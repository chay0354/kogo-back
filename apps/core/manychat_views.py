"""ManyChat / WhatsApp test API (manager only)."""
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.manychat_service import ManyChatError, ManyChatService
from apps.core.permissions import IsManager
from apps.customers.models import Family, Parent
from apps.customers.models import Child
from apps.courses.models import Lesson
from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context


def _subscriber_display_name(sub: dict) -> str:
    first = (sub.get('first_name') or '').strip()
    last = (sub.get('last_name') or '').strip()
    name = (sub.get('name') or '').strip()
    if first or last:
        return f'{first} {last}'.strip()
    return name or f'#{sub.get("id", "?")}'


class WhatsAppViewSet(viewsets.ViewSet):
    """
    Proxy to ManyChat for testing WhatsApp from Kogo.
    ManyChat does not expose full inbox history via API — contacts come from Kogo DB + lookup.
    """
    permission_classes = [IsAuthenticated, IsManager]

    @action(detail=False, methods=['get'], url_path='status')
    def status(self, request):
        svc = ManyChatService()
        payload = {'configured': svc.is_configured}
        if svc.is_configured:
            try:
                info = svc.get_page_info()
                data = info.get('data') if isinstance(info.get('data'), dict) else info
                payload['page_name'] = data.get('name')
            except ManyChatError as exc:
                payload['configured'] = False
                payload['error'] = str(exc)
        return Response(payload)

    @action(detail=False, methods=['get'], url_path='contacts')
    def contacts(self, request):
        """Kogo families/parents with phones — used as contact list (ManyChat has no list-all API)."""
        q = (request.query_params.get('q') or '').strip()
        rows: list[dict] = []
        seen_phones: set[str] = set()

        families = Family.objects.select_related('branch').filter(~Q(phone=''))
        if q:
            families = families.filter(Q(name__icontains=q) | Q(phone__icontains=q))

        for fam in families.order_by('name')[:200]:
            phone = (fam.phone or '').strip()
            norm = ManyChatService.normalize_phone_e164(phone)
            if not norm or norm in seen_phones:
                continue
            seen_phones.add(norm)
            rows.append({
                'id': str(fam.id),
                'source': 'family',
                'name': fam.name,
                'phone': phone,
                'phone_e164': norm,
                'branch_name': fam.branch.name if fam.branch_id else None,
            })

        parents = Parent.objects.select_related('family', 'family__branch').filter(~Q(phone=''))
        if q:
            parents = parents.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(phone__icontains=q)
            )
        for p in parents.order_by('family__name', 'last_name')[:200]:
            phone = (p.phone or '').strip()
            norm = ManyChatService.normalize_phone_e164(phone)
            if not norm or norm in seen_phones:
                continue
            seen_phones.add(norm)
            rows.append({
                'id': str(p.id),
                'source': 'parent',
                'name': p.full_name,
                'phone': phone,
                'phone_e164': norm,
                'branch_name': p.family.branch.name if p.family.branch_id else None,
                'family_name': p.family.name,
            })

        return Response({'contacts': rows[:300]})

    @action(detail=False, methods=['get'], url_path='lookup')
    def lookup(self, request):
        phone = (request.query_params.get('phone') or '').strip()
        name = (request.query_params.get('name') or '').strip()
        if not phone and not name:
            return Response({'error': 'נדרש phone או name'}, status=status.HTTP_400_BAD_REQUEST)

        svc = ManyChatService()
        try:
            subscribers: list[dict] = []
            if phone:
                subscribers.extend(svc.find_by_phone(phone))
            if name and not subscribers:
                subscribers.extend(svc.find_by_name(name))
            return Response({
                'subscribers': subscribers,
                'count': len(subscribers),
            })
        except ManyChatError as exc:
            return Response({'error': str(exc), 'detail': exc.payload}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=False, methods=['post'], url_path='resolve')
    def resolve(self, request):
        """Find ManyChat subscriber by phone, or create if missing."""
        phone = (request.data.get('phone') or '').strip()
        name = (request.data.get('name') or '').strip()
        if not phone:
            return Response({'error': 'נדרש מספר טלפון'}, status=status.HTTP_400_BAD_REQUEST)

        svc = ManyChatService()
        try:
            result = svc.lookup_or_create(phone, name)
            sub = result.get('subscriber') or {}
            return Response({
                'subscriber_id': result.get('subscriber_id'),
                'created': result.get('created', False),
                'display_name': _subscriber_display_name(sub) if isinstance(sub, dict) else name,
                'subscriber': sub,
            })
        except ManyChatError as exc:
            return Response({'error': str(exc), 'detail': exc.payload}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=False, methods=['get'], url_path='subscriber')
    def subscriber_detail(self, request):
        subscriber_id = request.query_params.get('subscriber_id')
        if not subscriber_id:
            return Response({'error': 'subscriber_id נדרש'}, status=status.HTTP_400_BAD_REQUEST)
        svc = ManyChatService()
        try:
            sub = svc.get_subscriber(subscriber_id)
            return Response({'subscriber': sub})
        except ManyChatError as exc:
            return Response({'error': str(exc), 'detail': exc.payload}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=False, methods=['post'], url_path='send')
    def send(self, request):
        text = (request.data.get('text') or '').strip()
        subscriber_id = request.data.get('subscriber_id')
        phone = (request.data.get('phone') or '').strip()
        name = (request.data.get('name') or '').strip()

        if not text:
            return Response({'error': 'נדרש טקסט להודעה'}, status=status.HTTP_400_BAD_REQUEST)

        svc = ManyChatService()
        try:
            if not subscriber_id and phone:
                resolved = svc.lookup_or_create(phone, name)
                subscriber_id = resolved.get('subscriber_id')
            if not subscriber_id:
                return Response({'error': 'לא נמצא subscriber — נסה resolve קודם'}, status=status.HTTP_400_BAD_REQUEST)

            result = svc.send_whatsapp_text(subscriber_id, text)
            return Response({
                'success': True,
                'subscriber_id': subscriber_id,
                'manychat': result,
            })
        except ManyChatError as exc:
            return Response({'error': str(exc), 'detail': exc.payload}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=False, methods=['post'], url_path='bulk-send')
    def bulk_send(self, request):
        """
        Send the same custom text to multiple contacts.

        Body: { "text": "...", "contacts": [{"phone": "...", "name": "..."}, ...] }
        Optional: "dry_run": true — preview only, no ManyChat calls.
        """
        text = (request.data.get('text') or '').strip()
        raw_contacts = request.data.get('contacts') or []
        if not text:
            return Response({'error': 'נדרש טקסט להודעה'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(raw_contacts, list) or not raw_contacts:
            return Response({'error': 'נדרש רשימת אנשי קשר'}, status=status.HTTP_400_BAD_REQUEST)

        dry_run = bool(request.data.get('dry_run', False))

        svc = ManyChatService()
        if not svc.is_configured:
            return Response({'error': 'ManyChat לא מוגדר'}, status=status.HTTP_400_BAD_REQUEST)

        max_batch = 100
        contacts = raw_contacts[:max_batch]

        results: list[dict] = []
        sent = 0
        failed = 0
        skipped = 0

        for item in contacts:
            if not isinstance(item, dict):
                skipped += 1
                continue
            phone = (item.get('phone') or '').strip()
            name = (item.get('name') or '').strip()
            if not phone:
                skipped += 1
                results.append({'phone': '', 'name': name, 'status': 'skipped', 'reason': 'no_phone'})
                continue

            if dry_run:
                results.append({'phone': phone, 'name': name, 'status': 'preview'})
                continue

            try:
                resolved = svc.lookup_or_create(phone, name)
                subscriber_id = resolved.get('subscriber_id')
                if not subscriber_id:
                    failed += 1
                    results.append({
                        'phone': phone,
                        'name': name,
                        'status': 'failed',
                        'error': 'לא נמצא subscriber',
                    })
                    continue
                svc.send_whatsapp_text(subscriber_id, text)
                sent += 1
                results.append({
                    'phone': phone,
                    'name': name,
                    'status': 'sent',
                    'subscriber_id': subscriber_id,
                })
            except ManyChatError as exc:
                failed += 1
                results.append({
                    'phone': phone,
                    'name': name,
                    'status': 'failed',
                    'error': str(exc),
                })

        return Response({
            'dry_run': dry_run,
            'total': len(contacts),
            'sent': sent,
            'failed': failed,
            'skipped': skipped,
            'preview_count': sum(1 for r in results if r.get('status') == 'preview'),
            'results': results[:50],
        })

    @action(detail=False, methods=['post'], url_path='test-enrollment')
    def test_enrollment(self, request):
        """
        Send registration WhatsApp template without enrolling or charging.

        Body: { "child_id": "...", "lesson_id": "...", "kind": "subscription" | "trial" }
        """
        child_id = request.data.get('child_id')
        lesson_id = request.data.get('lesson_id')
        kind = (request.data.get('kind') or '').strip().lower()

        if not child_id or not lesson_id:
            return Response(
                {'error': 'נדרשים child_id ו-lesson_id'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        kind_map = {
            'subscription': ManyChatService.REGISTRATION_KIND_SUBSCRIPTION,
            'trial': ManyChatService.REGISTRATION_KIND_TRIAL,
        }
        if kind not in kind_map:
            return Response(
                {'error': 'kind חייב להיות subscription או trial'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        child = (
            Child.objects
            .select_related('family')
            .prefetch_related('family__parents')
            .filter(id=child_id)
            .first()
        )
        if not child:
            return Response({'error': 'ילד לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        lesson = (
            Lesson.objects
            .select_related('course', 'branch')
            .filter(id=lesson_id)
            .first()
        )
        if not lesson:
            return Response({'error': 'שיעור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        ctx = build_enrollment_whatsapp_context(child=child, lesson=lesson)
        if not ctx:
            return Response(
                {'error': 'לא נמצא טלפון הורה למשפחה'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lookup_names = ctx.pop('lookup_names', None)
        svc = ManyChatService()
        try:
            result = svc.notify_registration(
                kind=kind_map[kind],
                lookup_names=lookup_names,
                **ctx,
            )
        except ManyChatError as exc:
            return Response({'error': str(exc), 'detail': exc.payload}, status=status.HTTP_502_BAD_GATEWAY)

        if not result.get('sent'):
            return Response(result, status=status.HTTP_502_BAD_GATEWAY)

        return Response(result)
