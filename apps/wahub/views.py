"""
The API of "וואטסאפ ולידים" — docs/WAHUB-CONTRACT.md, endpoint for endpoint.

Managers only, except the two doors a machine uses: ManyChat's copy of each
message (a key in a header) and the cron (the same token as every other cron).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Max
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import ParseError, UnsupportedMediaType
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.manychat_service import ManyChatError, ManyChatService
from apps.core.permissions import IsManager
from apps.wahub import analysis, cron, demo, handoff, inbound, matching, queries, reviewer, sending, shadow, state
from apps.wahub.models import (
    EVENT_CREATED,
    EVENT_FOLLOWUP,
    EVENT_NEEDS_HUMAN,
    EVENT_NOTE,
    EVENT_TAGS,
    FOLLOWUP_LATER,
    MESSAGE_SOURCE_MANYCHAT,
    SENDER_BOT,
    SOURCE_DEMO,
    SOURCE_MANUAL,
    Contact,
    ContactEvent,
    Message,
    QuickReply,
    Tag,
)
from apps.wahub.phones import normalize_phone
from apps.wahub.serializers import (
    FOLLOWUP_LABELS,
    QuickReplySerializer,
    TagSerializer,
    contact_payload,
    event_payload,
    message_payload,
    sorted_tags,
)

logger = logging.getLogger(__name__)

INBOUND_KEY_NAME = 'WAHUB_INBOUND_KEY'
# The key is kept as its SHA-256, not as itself: the stored row is then of no
# use to anybody who reads the table (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md).
HASH_PREFIX = 'sha256$'

MESSAGES_PAGE = 50
EVENTS_PAGE = 50
UPDATES_LIMIT = 100
# A row saved a moment before its transaction commits carries an older
# touched_at than the cursor a screen took in between. Reading a little behind
# the cursor catches it; the screen drops what it already has.
UPDATES_OVERLAP = timedelta(seconds=2)


def _error(detail: str, code: str | None = None, http=status.HTTP_400_BAD_REQUEST, **extra) -> Response:
    body = {'detail': detail, **extra}
    if code:
        body['code'] = code
    return Response(body, status=http)


def _first_error(errors) -> str:
    """One Hebrew line out of a serializer's errors."""
    if isinstance(errors, dict):
        for value in errors.values():
            return _first_error(value)
    if isinstance(errors, (list, tuple)) and errors:
        return _first_error(errors[0])
    return str(errors)


class ContactPagination(PageNumberPagination):
    page_size = 40
    page_size_query_param = 'page_size'
    max_page_size = 100


def _cursor(moment) -> str:
    return moment.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _read_cursor(raw):
    text = (raw or '').strip().replace(' ', '+')
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if timezone.is_aware(moment) else moment.replace(tzinfo=dt_timezone.utc)


class ContactViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, IsManager]
    pagination_class = ContactPagination
    filter_backends = []
    queryset = Contact.objects.all()
    lookup_value_regex = r'\d+'

    def get_queryset(self):
        return queries.listed()

    def _payload(self, contact_id) -> dict:
        """The contact as it now is in the database, after whatever the request did."""
        return contact_payload(queries.listed().get(pk=contact_id))

    # --- lists and counts ---

    def list(self, request):
        page = self.paginate_queryset(queries.contact_list(request.query_params))
        now = timezone.now()
        today = timezone.localtime(now).date()
        return self.get_paginated_response([contact_payload(contact, now=now, today=today) for contact in page])

    def create(self, request):
        phone = normalize_phone(request.data.get('phone'))
        if not phone:
            return _error('מספר הטלפון אינו נייד ישראלי תקין.', code='invalid_phone')
        name = ' '.join(str(request.data.get('name') or '').split())[:200]
        note = str(request.data.get('note') or '').strip()
        # An invented contact the owner types messages for; nothing is ever sent to it.
        is_demo = request.data.get('is_demo') is True
        existing = Contact.objects.filter(phone=phone).values_list('id', flat=True).first()
        if existing:
            return Response({'code': 'exists', 'contact_id': existing}, status=status.HTTP_409_CONFLICT)
        try:
            with transaction.atomic():
                contact = Contact.objects.create(
                    phone=phone, name=name, source=SOURCE_DEMO if is_demo else SOURCE_MANUAL, is_demo=is_demo,
                )
                state.log_event(contact.id, EVENT_CREATED, 'נוסף כאיש קשר דמו' if is_demo else 'נוסף ידנית', actor=request.user)
                if note:
                    state.log_event(contact.id, EVENT_NOTE, note, actor=request.user)
        except IntegrityError:
            # Somebody added the same phone in the same moment.
            existing = Contact.objects.filter(phone=phone).values_list('id', flat=True).first()
            return Response({'code': 'exists', 'contact_id': existing}, status=status.HTTP_409_CONFLICT)
        try:
            # So the screen says at once whether this is somebody the system knows.
            matching.recheck_contact(contact)
        except Exception:
            logger.exception('wahub: matching a new contact %s failed', contact.id)
        return Response(self._payload(contact.id), status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def counts(self, request):
        return Response(queries.all_counts(request.query_params))

    @action(detail=False, methods=['get'])
    def updates(self, request):
        """
        The live update. The cursor is this server's clock at the start of the
        request; the next call gets every contact touched since, read a little
        behind the cursor. When more than a call's worth moved at once the
        cursor only advances over what was returned, and the next calls bring
        the rest.
        """
        now = timezone.now()
        since = _read_cursor(request.query_params.get('since'))
        contacts, cursor = [], now
        if since is not None:
            rows = list(
                queries.listed().filter(touched_at__gt=since - UPDATES_OVERLAP)
                .order_by('touched_at', 'id')[:UPDATES_LIMIT + 1]
            )
            if len(rows) > UPDATES_LIMIT:
                rows = rows[:UPDATES_LIMIT]
                last = rows[-1].touched_at
                # Rows saved in the very same instant are never split between two
                # calls: the cursor is a time, and could not tell them apart.
                have = [row.id for row in rows if row.touched_at == last]
                rows += list(queries.listed().filter(touched_at=last).exclude(id__in=have).order_by('id'))
                # The next call starts right after that instant.
                cursor = last + UPDATES_OVERLAP
            today = timezone.localtime(now).date()
            contacts = [contact_payload(contact, now=now, today=today) for contact in reversed(rows)]
        return Response({'cursor': _cursor(cursor), 'contacts': contacts, 'boxes': queries.box_counts()})

    # --- one conversation ---

    def retrieve(self, request, pk=None):
        contact = self.get_object()
        payload = contact_payload(contact)
        messages = list(Message.objects.filter(contact=contact).order_by('-id')[:MESSAGES_PAGE + 1])
        payload['has_older'] = len(messages) > MESSAGES_PAGE
        payload['messages'] = [message_payload(message) for message in reversed(messages[:MESSAGES_PAGE])]
        payload['events'] = [
            event_payload(event)
            for event in ContactEvent.objects.filter(contact=contact).select_related('actor').order_by('-id')[:EVENTS_PAGE]
        ]
        payload['kogo']['children'] = matching.children_of(contact)
        return Response(payload)

    def partial_update(self, request, pk=None):
        contact = self.get_object()
        if 'name' not in request.data:
            return _error('אין מה לעדכן.')
        name = ' '.join(str(request.data.get('name') or '').split())[:200]
        if name != contact.name:
            state.touch(contact.id, name=name)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        """`after` — the new ones, for the open conversation; `before` — a page of older ones. Oldest first."""
        contact = self.get_object()
        queryset = Message.objects.filter(contact=contact)

        def number(name, default=None):
            raw = (request.query_params.get(name) or '').strip()
            return int(raw) if raw.isdigit() else default

        after, before = number('after'), number('before')
        limit = max(1, min(number('limit', MESSAGES_PAGE), 200))
        if after is not None:
            rows = list(queryset.filter(id__gt=after).order_by('id')[:200])
            oldest = rows[0].id if rows else after + 1
        else:
            if before is not None:
                queryset = queryset.filter(id__lt=before)
            rows = list(queryset.order_by('-id')[:limit])
            rows.reverse()
            oldest = rows[0].id if rows else (before or 0)
        has_older = Message.objects.filter(contact=contact, id__lt=oldest).exists()
        return Response({'messages': [message_payload(message) for message in rows], 'has_older': has_older})

    @action(detail=True, methods=['post'])
    def read(self, request, pk=None):
        contact = self.get_object()
        if contact.unread_count:
            state.touch(contact.id, unread_count=0)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        contact = self.get_object()
        text = str(request.data.get('text') or '').strip()
        if not text:
            return _error('נדרש טקסט להודעה.')
        if len(text) > sending.MAX_TEXT_CHARS:
            return _error('ההודעה ארוכה מדי (עד 4,096 תווים).')
        try:
            message = sending.send_text(contact, text, request.user)
        except sending.WindowClosed:
            return _error(sending.WINDOW_CLOSED_DETAIL, code='window_closed', http=status.HTTP_409_CONFLICT)
        return Response({'message': message_payload(message), 'contact': self._payload(contact.id)})

    @action(detail=True, methods=['post'], url_path='send-flow')
    def send_flow(self, request, pk=None):
        contact = self.get_object()
        automation_id = str(request.data.get('automation_id') or '').strip()
        if not automation_id:
            return _error('נדרש automation_id.')
        name = ' '.join(str(request.data.get('automation_name') or '').split())[:200]
        name = name or ManyChatService.AUTOMATION_LABELS.get(automation_id, '')
        if contact.is_demo:
            # An invented contact: ManyChat is not asked for the flow or its name; sending.py refuses the send.
            message = sending.send_flow(contact, automation_id, name or automation_id, request.user)
            return Response({'message': message_payload(message), 'contact': self._payload(contact.id)})
        try:
            flow_ns = sending.flow_for(automation_id)
        except ManyChatError:
            flow_ns = ''
        if not flow_ns:
            return _error('לא נמצאה ב-ManyChat אוטומציה לתבנית הזו.')
        name = name or sending.flow_name(flow_ns)
        message = sending.send_flow(contact, flow_ns, name or automation_id, request.user)
        return Response({'message': message_payload(message), 'contact': self._payload(contact.id)})

    @action(detail=True, methods=['post'])
    def takeover(self, request, pk=None):
        contact = self.get_object()
        try:
            handoff.takeover(contact, request.user)
        except handoff.HandoffError as exc:
            return _error(str(exc), http=status.HTTP_502_BAD_GATEWAY)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        contact = self.get_object()
        try:
            handoff.release(contact, request.user)
        except handoff.HandoffError as exc:
            return _error(str(exc), http=status.HTTP_502_BAD_GATEWAY)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'], url_path='needs-human')
    def needs_human(self, request, pk=None):
        contact = self.get_object()
        wanted = request.data.get('needs_human')
        if not isinstance(wanted, bool):
            return _error('נדרש needs_human (true או false).')
        if wanted:
            reason = ' '.join(str(request.data.get('reason') or '').split())[:200] or 'סומן ידנית'
            state.touch(contact.id, needs_human=True, needs_human_reason=reason, needs_human_at=timezone.now())
            state.log_event(contact.id, EVENT_NEEDS_HUMAN, f'סומן שצריך נציג: {reason}', actor=request.user)
        elif contact.needs_human:
            state.touch(contact.id, needs_human=False, needs_human_reason='', needs_human_at=None)
            state.log_event(contact.id, EVENT_NEEDS_HUMAN, 'סומן שטופל', actor=request.user)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['patch'])
    def followup(self, request, pk=None):
        """The marks a person fills in. `due` lives only under "another time"."""
        contact = self.get_object()
        data = request.data
        if not any(key in data for key in ('status', 'due', 'note')):
            return _error('אין מה לעדכן.')

        new_status = contact.followup_status
        if 'status' in data:
            new_status = str(data.get('status') or '').strip()
            if new_status and new_status not in FOLLOWUP_LABELS:
                return _error('סימון מעקב לא מוכר.')

        due = contact.followup_due
        if 'due' in data:
            raw = data.get('due')
            if raw in (None, ''):
                due = None
            else:
                try:
                    due = date.fromisoformat(str(raw))
                except ValueError:
                    return _error('תאריך לא תקין (YYYY-MM-DD).')
        if new_status != FOLLOWUP_LATER:
            due = None

        note = contact.followup_note
        if 'note' in data:
            note = str(data.get('note') or '').strip()[:2000]

        described = []
        if new_status != contact.followup_status:
            described.append(FOLLOWUP_LABELS.get(new_status, 'בלי סימון'))
        if due != contact.followup_due and new_status == FOLLOWUP_LATER:
            described.append(f'לחזור ב-{due.day}.{due.month}.{due.year}' if due else 'בלי תאריך')
        if note != contact.followup_note:
            described.append(f'הערה: {note}' if note else 'ההערה נמחקה')
        if (new_status, due, note) != (contact.followup_status, contact.followup_due, contact.followup_note):
            state.touch(
                contact.id,
                followup_status=new_status, followup_due=due, followup_note=note,
                followup_by=request.user, followup_at=timezone.now(),
            )
            state.log_event(contact.id, EVENT_FOLLOWUP, ' · '.join(described), actor=request.user)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['put'])
    def tags(self, request, pk=None):
        contact = self.get_object()
        raw = request.data.get('tag_ids')
        if not isinstance(raw, list) or not all(isinstance(item, int) and not isinstance(item, bool) for item in raw):
            return _error('נדרש tag_ids (רשימת מזהים).')
        wanted = list(Tag.objects.filter(id__in=raw))
        if len(wanted) != len(set(raw)):
            return _error('אחת התגיות לא קיימת.')
        before = {tag.id: tag.name for tag in contact.tags.all()}
        after = {tag.id: tag.name for tag in wanted}
        if before.keys() != after.keys():
            contact.tags.set(wanted)
            state.touch(contact.id)
            added = [name for tag_id, name in after.items() if tag_id not in before]
            removed = [name for tag_id, name in before.items() if tag_id not in after]
            parts = ([f'נוספו: {", ".join(added)}'] if added else []) + ([f'הוסרו: {", ".join(removed)}'] if removed else [])
            state.log_event(contact.id, EVENT_TAGS, ' · '.join(parts), actor=request.user)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'])
    def recheck(self, request, pk=None):
        contact = self.get_object()
        matching.recheck_contact(contact)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        contact = self.get_object()
        analysis.analyze_contact(contact)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'], url_path='simulate-inbound')
    def simulate_inbound(self, request, pk=None):
        """
        Demo mode: a message typed by the manager goes through the very door
        ManyChat's copy uses (inbound.store_event) — needs_human, needs_analysis,
        needs_shadow and all. Only for a contact marked is_demo.
        """
        contact = self.get_object()
        if not contact.is_demo:
            return _error('אפשר להקליד הודעות רק לאיש קשר דמו.', code='not_demo')
        text = str(request.data.get('text') or '').strip()
        if not text:
            return _error('נדרש טקסט להודעה.')
        sender = str(request.data.get('sender') or 'customer').strip()
        if sender not in ('customer', 'bot'):
            return _error('sender: customer או bot.')
        event = inbound.EVENT_CUSTOMER_MESSAGE if sender == 'customer' else inbound.EVENT_BOT_REPLY
        result = inbound.store_event(event=event, phone=contact.phone, text=text, name=contact.name)
        payload = self._payload(contact.id)
        payload['stored'] = result.stored
        payload['stored_message_id'] = result.message_id
        return Response(payload)

    @action(detail=True, methods=['get'])
    def shadow(self, request, pk=None):
        """What the new bot would have answered, message by message, beside the old bot's answers. Newest first."""
        contact = self.get_object()
        rows = list(contact.shadow_replies.select_related('verdict_by').order_by('-id')[:100])
        ids = {item_id for reply in rows for item_id in (reply.knowledge_used or []) if isinstance(item_id, int)}
        from apps.wahub.models import KnowledgeItem
        items = {item.id: item for item in KnowledgeItem.objects.filter(id__in=ids)} if ids else {}
        return Response([shadow.reply_payload(reply, items=items) for reply in rows])


class _SettingsListViewSet(viewsets.ModelViewSet):
    """A short settings list: a plain array, and one Hebrew line for a refused save."""
    permission_classes = [IsAuthenticated, IsManager]
    pagination_class = None
    filter_backends = []
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def _save(self, serializer, http):
        if not serializer.is_valid():
            return _error(_first_error(serializer.errors))
        serializer.save()
        return Response(serializer.data, status=http)

    def create(self, request, *args, **kwargs):
        return self._save(self.get_serializer(data=request.data), status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        return self._save(
            self.get_serializer(self.get_object(), data=request.data, partial=True), status.HTTP_200_OK,
        )


class TagViewSet(_SettingsListViewSet):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer

    def list(self, request, *args, **kwargs):
        return Response(self.get_serializer(sorted_tags(self.get_queryset()), many=True).data)

    def _wake_contacts(self, tag):
        # Every open list shows the tag by name and colour.
        state.touch_many(Contact.objects.filter(tags=tag).values_list('id', flat=True))

    def partial_update(self, request, *args, **kwargs):
        response = super().partial_update(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            self._wake_contacts(self.get_object())
        return response

    def perform_destroy(self, instance):
        self._wake_contacts(instance)
        instance.delete()


class QuickReplyViewSet(_SettingsListViewSet):
    queryset = QuickReply.objects.all()
    serializer_class = QuickReplySerializer


# --- the "today" tab and the settings ---------------------------------------------------

class SummaryView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        return Response(queries.summary())


def _stored_inbound_key():
    from apps.core.models import IntegrationCredential

    return IntegrationCredential.objects.filter(key=INBOUND_KEY_NAME).first()


def _inbound_url(request) -> str:
    """
    The address to paste into ManyChat. The server's own public address when it
    is configured (CRM_API_BASE_URL already ends with /api/v1); on a developer's
    machine, the address this request came to.
    """
    base = (getattr(settings, 'CRM_API_BASE_URL', '') or '').strip().rstrip('/')
    if base and 'localhost' not in base and '127.0.0.1' not in base:
        return f'{base}/wahub/inbound/manychat/'
    return request.build_absolute_uri('/api/v1/wahub/inbound/manychat/')


class StatusView(APIView):
    """What is connected, so the screen can say what its numbers are worth."""
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        from apps.core.scoping import integration_credential

        now = timezone.now()
        stored = _stored_inbound_key()
        contacts = Contact.objects.aggregate(total=Count('id'), last_inbound=Max('last_inbound_at'))
        return Response({
            'inbound_configured': bool(integration_credential(INBOUND_KEY_NAME)),
            'inbound_key_set_at': stored.updated_at if stored else None,
            # Without the bot's replies nobody can tell who is still waiting.
            'bot_replies_seen': Message.objects.filter(
                sender=SENDER_BOT, source=MESSAGE_SOURCE_MANYCHAT, created_at__gte=now - timedelta(days=7),
            ).exists(),
            'ai_configured': analysis.ai_configured(),
            # The shadow bot drafts with Claude when there is a key; otherwise the stub answers.
            'shadow_configured': shadow.shadow_configured(),
            'shadow_model': shadow.model_name() if shadow.shadow_configured() else 'stub',
            'send_configured': ManyChatService().is_configured,
            'sending_enabled': sending.sending_enabled(),
            'simulate_send': sending.simulate_send(),
            'last_inbound_at': contacts['last_inbound'],
            'contacts_total': contacts['total'],
            'messages_last_24h': Message.objects.filter(sent_at__gte=now - timedelta(hours=24)).count(),
            'inbound_url': _inbound_url(request),
        })


class InboundKeyView(APIView):
    """A new key for ManyChat's copy of the messages. Shown once; the old one stops working."""
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request):
        from apps.core.models import IntegrationCredential

        key = secrets.token_urlsafe(36)
        IntegrationCredential.objects.update_or_create(
            key=INBOUND_KEY_NAME,
            defaults={'value': HASH_PREFIX + hashlib.sha256(key.encode()).hexdigest(), 'updated_by': request.user},
        )
        return Response({'key': key})


# --- the two doors a machine uses ---------------------------------------------------------

def inbound_key_matches(provided: str, expected: str) -> bool:
    """Constant-time. `expected` is the stored hash, or a plain value set by hand."""
    if not provided or not expected:
        return False
    if expected.startswith(HASH_PREFIX):
        provided = HASH_PREFIX + hashlib.sha256(provided.encode()).hexdigest()
    return hmac.compare_digest(provided.encode(), expected.encode())


class ManyChatInboundView(APIView):
    """
    POST inbound/manychat/ — ManyChat's copy of a customer's message or of a
    bot's reply. No user: the caller proves itself with the key in X-Wahub-Key.

    An authenticated call is always answered 200, stored or not, so ManyChat
    does not repeat a message Kogo decided not to keep. Nothing here calls
    another company or reads the registrations.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'wahub_inbound'

    def post(self, request):
        from apps.core.scoping import integration_credential

        expected = integration_credential(INBOUND_KEY_NAME)
        if not expected:
            return Response({'detail': 'inbound key is not configured'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not inbound_key_matches((request.headers.get('X-Wahub-Key') or '').strip(), expected):
            return Response({'detail': 'forbidden'}, status=status.HTTP_403_FORBIDDEN)
        try:
            data = request.data
        except (ParseError, UnsupportedMediaType):
            return Response({'ok': True, 'stored': False})
        try:
            result = inbound.handle_payload(data, request.query_params.get('event') or '')
        except Exception:
            # ManyChat would only send it again; the fault is ours to read in the log.
            logger.exception('wahub inbound: could not store an event')
            return Response({'ok': True, 'stored': False})
        return Response({'ok': True, 'stored': result.stored})


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_tick(request):
    """One slice of summaries and matching. Auth: the same token as the other crons."""
    from apps.customers.views import _cron_request_authorized

    if not _cron_request_authorized(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
    return Response({'ok': True, **cron.tick()})



# --- demo mode (docs/WAHUB-CONTRACT-STAGE2.md, ה) -----------------------------------------------------

class DemoScenariosView(APIView):
    """GET demo/scenarios/ — the ready-made failure conversations the owner can replay."""
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        return Response(demo.scenario_list())


class DemoScenarioView(APIView):
    """POST demo/scenario/ {scenario} — a demo contact with that conversation, summarised, matched, shadowed and reviewed at once."""
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request):
        key = str(request.data.get('scenario') or '').strip()
        if key not in demo.DEMO_SCENARIOS:
            return _error('תרחיש לא מוכר. GET demo/scenarios/ מחזיר את הרשימה.', code='unknown_scenario')
        contact = demo.create_scenario(key, request.user)
        payload = contact_payload(queries.listed().get(pk=contact.id))
        payload['shadow'] = [shadow.reply_payload(reply) for reply in contact.shadow_replies.order_by('-id')[:5]]
        payload['proposals'] = [reviewer.proposal_payload(row) for row in contact.proposals.order_by('-id')[:5]]
        return Response(payload, status=status.HTTP_201_CREATED)


class DemoContactsView(APIView):
    """DELETE demo/contacts/ — every demo contact with its messages, shadow replies, proposals and notes."""
    permission_classes = [IsAuthenticated, IsManager]

    def delete(self, request):
        return Response(demo.delete_demo_contacts())
