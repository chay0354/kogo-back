"""
Card links for existing customers.

CRM (managers): create, list per child, send on WhatsApp, cancel, regenerate.
Public (the parent, no auth, throttled): preview by token, submit a card.

The list has two forms, and they are not the same answer to the same question.
With `child_id` it is the per-child list the send dialog reads, and its shape is
frozen. Without it, it is the office's own screen — every card link and every
standing-order card-update link, newest first, across all children, because the
owner's question is "I sent a link, what happened with it?" and he does not know
which child to open to find out.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.card_validation import CardValidationError, validate_card_details
from apps.core.frontend_url import public_frontend_url
from apps.core.models import Branch, Business, BusinessCategory
from apps.core.permissions import IsManager
from apps.courses.models import Lesson, LessonBundle
from apps.customers.card_link import (
    PROCESSING_STALE_AFTER,
    CardLinkError,
    apply_card_link,
    card_link_options,
    card_link_public_url,
    preview_payload,
    quote_standing_order,
    resolve_card_link_token,
    send_card_link_whatsapp,
    unit_label,
    unit_lessons,
)
from apps.customers.card_update import (
    MODE_CARD_ONLY,
    MODE_RENEW,
    card_update_public_url_for_token,
    month_label,
)
from apps.customers.models import Child
from apps.core.payment_service import child_has_standing_order_for_lessons, lessons_covered_by_selection
from apps.payment_links.models import CardLink, CardUpdateLink, money


def _serialize(link: CardLink, request=None) -> dict:
    lesson = link.lesson
    return {
        'id': str(link.id),
        'kind': link.kind,
        'status': link.status,
        'child_id': str(link.child_id),
        'lesson_id': str(link.lesson_id) if link.lesson_id else None,
        'bundle_id': str(link.bundle_id) if link.bundle_id else None,
        'lesson_label': unit_label(lesson=lesson, bundle=link.bundle) if lesson else '',
        'include_registration_fee': link.include_registration_fee,
        'amount': str(money(link.amount)) if link.amount is not None else None,
        'description': link.description,
        'branch_id': str(link.branch_id) if link.branch_id else None,
        'business_id': str(link.business_id) if link.business_id else None,
        'business_category_id': str(link.business_category_id) if link.business_category_id else None,
        'public_url': (
            card_link_public_url(link, public_frontend_url(request))
            if link.status in (CardLink.STATUS_PENDING, CardLink.STATUS_PROCESSING) else ''
        ),
        'attempts': link.attempts,
        'last_error': link.last_error,
        'review_reason': link.review_reason,
        'payment_id': str(link.payment_id) if link.payment_id else None,
        'recurring_payment_id': str(link.recurring_payment_id) if link.recurring_payment_id else None,
        'sent_at': link.sent_at.isoformat() if link.sent_at else None,
        'sent_result': link.sent_result or {},
        'completed_at': link.completed_at.isoformat() if link.completed_at else None,
        'created_at': link.created_at.isoformat(),
    }


KIND_CARD_UPDATE = 'card_update'
KIND_LABELS = {
    CardLink.KIND_STANDING_ORDER: 'הוראת קבע',
    CardLink.KIND_ONE_TIME: 'חיוב חד-פעמי',
    KIND_CARD_UPDATE: 'עדכון אשראי',
}
MODE_LABELS = {MODE_RENEW: 'חידוש הוראת קבע', MODE_CARD_ONLY: 'שינוי פרטי אשראי'}

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _int_param(raw, default: int, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _person(child) -> tuple[str, str]:
    """(child name, family name) — blank rather than missing."""
    if child is None:
        return '', ''
    family = child.family if child.family_id else None
    return child.full_name, (family.name if family else '')


def _family_branch(child):
    if child is None or not child.family_id:
        return None
    family = child.family
    return family.branch if family.branch_id else None


def _who(user) -> str:
    if user is None:
        return ''
    return (user.get_full_name() or '').strip() or user.get_username()


def _months_label_from_keys(keys) -> str:
    rows = []
    for key in keys or []:
        try:
            year, month = str(key).split('-')
            rows.append(month_label(date(int(year), int(month), 1)))
        except (ValueError, TypeError, IndexError):
            continue
    return ', '.join(rows)


def _overview_card_link(link: CardLink, request=None) -> dict:
    """One card link, said in the words the office reads the screen in."""
    child_name, family_name = _person(link.child)
    branch = link.branch if link.branch_id else _family_branch(link.child)
    if link.kind == CardLink.KIND_STANDING_ORDER:
        description = unit_label(lesson=link.lesson, bundle=link.bundle) if link.lesson_id else ''
    else:
        description = link.description
    return {
        'id': str(link.id),
        'source': 'card_link',
        'kind': link.kind,
        'kind_label': KIND_LABELS.get(link.kind, link.kind),
        'mode': '',
        'mode_label': '',
        'status': link.status,
        'status_label': link.get_status_display(),
        'child_id': str(link.child_id) if link.child_id else None,
        'child_name': child_name,
        'family_name': family_name,
        # branch_id/business_id are what the CRM's shared filter bar narrows on;
        # the names beside them are what the row shows.
        'branch_id': str(branch.id) if branch else None,
        'branch_name': branch.name if branch else '',
        'business_id': str(link.business_id) if link.business_id else None,
        'business_name': link.business.name if link.business_id else '',
        # A standing-order link has no fixed sum: the first charge is priced when
        # the parent pays, exactly as the widget prices it. Blank is the truth.
        'amount': str(money(link.amount)) if link.amount is not None else None,
        'description': description,
        'created_at': link.created_at.isoformat(),
        'created_by_name': _who(link.created_by),
        'sent_at': link.sent_at.isoformat() if link.sent_at else None,
        # A one-time charge is never sent on WhatsApp — the approved template
        # speaks of a standing order — so the office copies it, and 'copy' is
        # what happened to it even though we never learn when.
        'sent_via': (
            'whatsapp' if link.sent_at
            else ('copy' if link.kind == CardLink.KIND_ONE_TIME else '')
        ),
        # A card link records no page view; the column stays blank for it rather
        # than pretending a link nobody opened was never opened.
        'first_opened_at': None,
        'completed_at': link.completed_at.isoformat() if link.completed_at else None,
        'last_error': link.last_error,
        'public_url': (
            card_link_public_url(link, public_frontend_url(request))
            if link.status in (CardLink.STATUS_PENDING, CardLink.STATUS_PROCESSING) else ''
        ),
    }


def _sto_branch(recurring):
    """The branch a standing order bills for — its lesson's, as everywhere else."""
    initial = recurring.initial_payment if recurring is not None and recurring.initial_payment_id else None
    lesson = initial.lesson if initial is not None and initial.lesson_id else None
    if lesson is not None and lesson.course_id and lesson.course.branch_id:
        return lesson.course.branch
    if initial is not None and initial.branch_id:
        return initial.branch
    return None


def _overview_card_update(link: CardUpdateLink) -> dict:
    child_name, family_name = _person(link.child)
    branch = _sto_branch(link.recurring_payment) or _family_branch(link.child)
    months = _months_label_from_keys(link.months)
    mode_label = MODE_LABELS.get(link.mode, '')
    description = f'{mode_label} · {months}' if mode_label and months else (mode_label or months)
    # What the link actually took, when it took anything — a month collected by
    # the monthly run in between is dropped, so the sum asked for and the sum
    # charged are not always the same number.
    amount = link.charged_amount if link.charged_amount is not None else link.amount
    return {
        'id': str(link.id),
        'source': 'card_update',
        'kind': KIND_CARD_UPDATE,
        'kind_label': KIND_LABELS[KIND_CARD_UPDATE],
        'mode': link.mode,
        'mode_label': mode_label,
        'status': link.status,
        'status_label': link.get_status_display(),
        'child_id': str(link.child_id) if link.child_id else None,
        'child_name': child_name,
        'family_name': family_name,
        'branch_id': str(branch.id) if branch else None,
        'branch_name': branch.name if branch else '',
        # A standing order is always branch income; it carries no business tag.
        'business_id': None,
        'business_name': '',
        'amount': str(money(amount)) if amount is not None else None,
        'description': description,
        'created_at': link.created_at.isoformat(),
        'created_by_name': _who(link.created_by),
        'sent_at': link.sent_at.isoformat() if link.sent_at else None,
        'sent_via': link.channel,
        'first_opened_at': link.first_opened_at.isoformat() if link.first_opened_at else None,
        'completed_at': link.completed_at.isoformat() if link.completed_at else None,
        'last_error': link.last_error,
        'public_url': (
            card_update_public_url_for_token(link.token)
            if link.token and link.status not in (
                CardUpdateLink.STATUS_CHARGED, CardUpdateLink.STATUS_CARD_SAVED,
            ) else ''
        ),
    }


def _overview(request) -> dict:
    """
    Every link the office sent, newest first, whatever table it lives in.

    The two tables are paged together in Python rather than in SQL: a UNION over
    two different shapes buys nothing at these volumes, and the merge keeps each
    row serialized by the code that knows what it means.
    """
    limit = _int_param(request.query_params.get('limit'), DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    offset = _int_param(request.query_params.get('offset'), 0, 0, 100_000)
    kind = (request.query_params.get('kind') or '').strip()
    wanted_status = (request.query_params.get('status') or '').strip()
    query = (request.query_params.get('q') or '').strip()

    links = CardLink.objects.select_related(
        'child', 'child__family', 'child__family__branch', 'branch', 'business',
        'lesson', 'lesson__course', 'bundle', 'created_by',
    ).prefetch_related('bundle__lessons')
    updates = CardUpdateLink.objects.select_related(
        'child', 'child__family', 'child__family__branch', 'created_by',
        'recurring_payment', 'recurring_payment__initial_payment',
        'recurring_payment__initial_payment__lesson',
        'recurring_payment__initial_payment__lesson__course',
        'recurring_payment__initial_payment__lesson__course__branch',
        'recurring_payment__initial_payment__branch',
    )

    if kind in (CardLink.KIND_STANDING_ORDER, CardLink.KIND_ONE_TIME):
        links, updates = links.filter(kind=kind), updates.none()
    elif kind == KIND_CARD_UPDATE:
        links = links.none()
    if wanted_status:
        # The two tables do not share a status vocabulary, and they should not:
        # 'נדחה' on a card-update link and 'ממתין' on a card link are different
        # facts. A status only one table knows simply empties the other.
        links = links.filter(status=wanted_status)
        updates = updates.filter(status=wanted_status)
    if query:
        matches = (
            Q(child__first_name__icontains=query)
            | Q(child__last_name__icontains=query)
            | Q(child__family__name__icontains=query)
            | Q(child__family__phone__icontains=query)
        )
        links = links.filter(matches)
        updates = updates.filter(matches)

    window = limit + offset
    rows = [(row.created_at, _overview_card_link(row, request)) for row in links.order_by('-created_at')[:window]]
    rows += [(row.created_at, _overview_card_update(row)) for row in updates.order_by('-created_at')[:window]]
    rows.sort(key=lambda pair: pair[0], reverse=True)
    page = [payload for _, payload in rows[offset:offset + limit]]
    count = links.count() + updates.count()
    return {
        'results': page,
        'count': count,
        'limit': limit,
        'offset': offset,
        'has_more': offset + len(page) < count,
    }


class CardLinkListCreateView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        child_id = request.query_params.get('child_id')
        if not child_id:
            return Response(_overview(request))
        links = (
            CardLink.objects.filter(child_id=child_id)
            .select_related('lesson', 'lesson__course', 'bundle')
            .prefetch_related('bundle__lessons')
            .order_by('-created_at')[:50]
        )
        return Response([_serialize(row, request) for row in links])

    def post(self, request):
        data = request.data
        kind = (data.get('kind') or '').strip()
        if kind not in (CardLink.KIND_STANDING_ORDER, CardLink.KIND_ONE_TIME):
            return Response({'error': 'kind חייב להיות standing_order או one_time'}, status=status.HTTP_400_BAD_REQUEST)
        child = Child.objects.select_related('family').filter(id=data.get('child_id')).first()
        if child is None:
            return Response({'error': 'ילד לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        link = CardLink(kind=kind, child=child, created_by=request.user)
        if kind == CardLink.KIND_STANDING_ORDER:
            lesson = (
                Lesson.objects.select_related('course', 'course__branch').filter(id=data.get('lesson_id')).first()
                if data.get('lesson_id') else None
            )
            bundle = None
            if data.get('bundle_id'):
                bundle = LessonBundle.objects.prefetch_related('lessons').filter(id=data.get('bundle_id')).first()
                if bundle is None:
                    return Response({'error': 'המסלול לא נמצא'}, status=status.HTTP_400_BAD_REQUEST)
                members = unit_lessons(lesson=None, bundle=bundle)
                # The standing order hangs on the track's first day, as in the widget.
                if lesson is None or lesson not in members:
                    lesson = members[0] if members else None
            if lesson is None:
                return Response({'error': 'יש לבחור שיעור או מסלול להוראת הקבע'}, status=status.HTTP_400_BAD_REQUEST)
            covered = lessons_covered_by_selection(lesson=lesson, bundle=bundle)
            if child_has_standing_order_for_lessons(child, covered):
                return Response({'error': 'לילד כבר יש הוראת קבע פעילה לשיעור הזה'}, status=status.HTTP_400_BAD_REQUEST)
            link.lesson = lesson
            link.bundle = bundle
            link.include_registration_fee = bool(data.get('include_registration_fee', True))
            link.branch = lesson.course.branch
        else:
            try:
                amount = money(Decimal(str(data.get('amount') or '0')))
            except (InvalidOperation, ValueError):
                return Response({'error': 'סכום לא תקין'}, status=status.HTTP_400_BAD_REQUEST)
            if amount < Decimal('1.00') or amount > Decimal('50000'):
                return Response({'error': 'סכום לא תקין'}, status=status.HTTP_400_BAD_REQUEST)
            link.amount = amount
            link.description = (data.get('description') or '').strip()[:200]
            if not link.description:
                return Response({'error': 'יש להזין תיאור לחיוב'}, status=status.HTTP_400_BAD_REQUEST)
            branch = Branch.objects.filter(id=data.get('branch_id')).first() if data.get('branch_id') else None
            link.branch = branch or (child.family.branch if child.family_id and child.family.branch_id else None)
            business = Business.objects.filter(id=data.get('business_id')).first() if data.get('business_id') else None
            category = BusinessCategory.objects.filter(id=data.get('business_category_id')).first() if data.get('business_category_id') else None
            if category is not None and (business is None or category.business_id != business.id):
                return Response({'error': 'הקטגוריה אינה שייכת לעסק שנבחר'}, status=status.HTTP_400_BAD_REQUEST)
            link.business = business
            link.business_category = category
        link.save()
        link = CardLink.objects.select_related(
            'child', 'child__family', 'lesson', 'lesson__course', 'lesson__course__branch', 'bundle',
        ).get(id=link.id)

        payload = _serialize(link, request)
        if kind == CardLink.KIND_STANDING_ORDER:
            try:
                q = quote_standing_order(link)
                payload['quote'] = {
                    'first_charge': str(q['first_charge']), 'monthly_amount': str(q['monthly_amount']),
                    'registration_fee': str(q['registration_fee']), 'next_billing_date': q['next_billing_date'].isoformat(),
                }
            except CardLinkError as exc:
                payload['quote_error'] = str(exc)
        if data.get('send'):
            payload['whatsapp'] = send_card_link_whatsapp(link, public_frontend_url(request))
        return Response(payload, status=status.HTTP_201_CREATED)


class CardLinkActionView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request, link_id, action):
        link = CardLink.objects.select_related(
            'child', 'child__family', 'lesson', 'lesson__course', 'lesson__course__branch', 'bundle',
        ).filter(id=link_id).first()
        if link is None:
            return Response({'error': 'לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        if action == 'send':
            if link.status not in (CardLink.STATUS_PENDING,):
                return Response({'error': 'אפשר לשלוח רק קישור שממתין'}, status=status.HTTP_400_BAD_REQUEST)
            # WhatsApp carries one approved template, and it speaks of updating a card
            # on file. A one-time charge is copied from the popup and sent by hand,
            # so nobody tells a parent their standing order failed over a shirt.
            if link.kind == CardLink.KIND_ONE_TIME:
                return Response(
                    {'error': 'חיוב חד-פעמי נשלח בהעתקת הקישור, לא בוואטסאפ'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            result = send_card_link_whatsapp(link, public_frontend_url(request))
            link.refresh_from_db()
            return Response({**_serialize(link, request), 'whatsapp': result})
        in_flight = (
            link.status == CardLink.STATUS_PROCESSING
            and link.charge_started_at is not None
            and timezone.now() - link.charge_started_at < PROCESSING_STALE_AFTER
        )
        if action in ('cancel', 'regenerate') and in_flight:
            return Response({'error': 'חיוב בעיבוד כרגע — נסו שוב בעוד דקה'}, status=status.HTTP_409_CONFLICT)
        if action == 'cancel':
            if link.status in (CardLink.STATUS_COMPLETED, CardLink.STATUS_REVIEW):
                return Response({'error': 'קישור שמומש לא ניתן לביטול'}, status=status.HTTP_400_BAD_REQUEST)
            link.status = CardLink.STATUS_CANCELLED
            link.rotate_token()
            link.save(update_fields=['status', 'token', 'token_version', 'updated_at'])
            return Response(_serialize(link, request))
        if action == 'regenerate':
            if link.status in (CardLink.STATUS_COMPLETED, CardLink.STATUS_REVIEW):
                return Response({'error': 'קישור שמומש לא ניתן לחידוש'}, status=status.HTTP_400_BAD_REQUEST)
            # attempts is kept: it is part of every idempotency key this link ever used.
            link.status = CardLink.STATUS_PENDING
            link.rotate_token()
            link.last_error = ''
            # `created_at` is left alone now that nothing expires: it is when the
            # office first made this link, which is what the screen reports.
            link.save(update_fields=['status', 'token', 'token_version', 'last_error', 'updated_at'])
            return Response(_serialize(link, request))
        return Response({'error': 'פעולה לא מוכרת'}, status=status.HTTP_400_BAD_REQUEST)


class CardLinkOptionsView(APIView):
    """GET ?child_id= — what this child can be sent a standing-order link for, each priced."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        child = Child.objects.select_related('family').filter(id=request.query_params.get('child_id')).first()
        if child is None:
            return Response({'error': 'ילד לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'options': card_link_options(child)})


class CardLinkPreviewView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'card_link_view'

    def get(self, request, token: str):
        try:
            link, already_done = resolve_card_link_token(token)
        except CardLinkError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(preview_payload(link, already_done=already_done))


class CardLinkChargeView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'card_link_charge'

    def post(self, request, token: str):
        try:
            link, already_done = resolve_card_link_token(token)
        except CardLinkError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if already_done:
            return Response({'success': True, 'already_done': True})
        try:
            card = validate_card_details(request.data.get('card_details') or {})
        except CardValidationError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = apply_card_link(link, card)
        except CardLinkError as exc:
            if exc.already_done:
                return Response({'success': True, 'already_done': True})
            code = status.HTTP_409_CONFLICT if exc.processing else status.HTTP_400_BAD_REQUEST
            return Response({'success': False, 'error': str(exc), 'processing': exc.processing}, status=code)
        return Response(result)
