"""
Card links for existing customers.

CRM (managers): create, list per child, send on WhatsApp, cancel, regenerate.
Public (the parent, no auth, throttled): preview by token, submit a card.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

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
from apps.customers.models import Child
from apps.core.payment_service import child_has_standing_order_for_lessons, lessons_covered_by_selection
from apps.payment_links.models import CardLink, money


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


class CardLinkListCreateView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        child_id = request.query_params.get('child_id')
        if not child_id:
            return Response({'error': 'נדרש child_id'}, status=status.HTTP_400_BAD_REQUEST)
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
