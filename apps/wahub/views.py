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
import time
import uuid

import requests
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
from apps.customers.models import Family
from apps.core.whatsapp_optout import OptOutListUnavailable
from apps.wahub import (
    analysis, cron, demo, future_leads, handoff, history, inbound, lead_stage, live_test, matching, media, office_clock,
    optout, queries, reviewer, sending, shadow, state, transcribe, waiting_list, waiting_notify, waiting_summary,
)
from apps.wahub.models import (
    DELIVERED_STATUSES,
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
    WaitingListEntry,
)
from apps.wahub.phones import normalize_phone
from apps.wahub.search import Search
from apps.wahub.serializers import (
    FOLLOWUP_LABELS,
    QuickReplySerializer,
    TagSerializer,
    contact_payload,
    event_payload,
    for_customer_payload,
    found_elsewhere,
    message_payload,
    sorted_tags,
    unregistered_lead_payload,
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
        """
        The list of a screen. With `search` (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.1) every row says what it
        was found by, and the first page carries `customers`: the families the search finds that have no
        conversation here. The screen sends `box=all` with a search, so that it crosses the boxes.
        """
        found = Search(request.query_params.get('search'))
        now = timezone.now()
        today = timezone.localtime(now).date()
        # The office's hours, laid out once for the whole request: the order of the list and every row read them.
        clock = office_clock.Clock(now)
        page = self.paginate_queryset(queries.contact_list(request.query_params, found=found, clock=clock))
        if not found:
            return self.get_paginated_response([contact_payload(contact, now=now, today=today, clock=clock) for contact in page])
        why = found.found_by(page)
        response = self.get_paginated_response([
            contact_payload(contact, now=now, today=today, found_by=why.get(contact.pk), clock=clock) for contact in page
        ])
        if self.paginator.page.number == 1:
            response.data['customers'], response.data['customers_more'] = found.customers()
        return response

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
        # The lead stage follows whatever a person does to its facts (a new contact, a mark, an answer,
        # a recheck), so the screen that asked gets it back at once. It never fails the request.
        lead_stage.refresh_quietly(contact.id)
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

        A conversation crosses the three office hours of waiting for a person by
        the clock alone, with nobody touching it — so every call also says who
        is past them now (`overdue`, the longest wait first) and how many
        (`boxes.overdue`); docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.2.5.
        """
        now = timezone.now()
        clock = office_clock.Clock(now)
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
            contacts = [contact_payload(contact, now=now, today=today, clock=clock) for contact in reversed(rows)]
        boxes = queries.box_counts(clock=clock)
        return Response({
            'cursor': _cursor(cursor), 'contacts': contacts, 'boxes': boxes,
            # Asked for only when the count says there is somebody.
            'overdue': queries.overdue_now(clock) if boxes.get('overdue') else [],
        })

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
        # What a person wrote about the customer while talking to them — shown inside the conversation and on the card.
        payload['notes'] = future_leads.notes(contact)
        # What the old bot and the office wrote in ManyChat before the switch, once copied (history.py) — the
        # screen puts each line in its place among the messages. [] and no query before a copy.
        payload['history_lines'] = history.lines_for(contact)
        # For somebody on a waiting list: what they wanted and where, and what can be offered instead now
        # (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.3.2). None — and nothing read — for everybody else.
        payload['waiting_summary'] = waiting_summary.of(contact)
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
        if message.status in DELIVERED_STATUSES:
            # The office answered: from here on the conversation is a person's, and the bot keeps quiet in it.
            handoff.office_answered(contact, request.user)
        lead_stage.refresh_quietly(contact.id)
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
            lead_stage.refresh_quietly(contact.id)
            return Response({'message': message_payload(message), 'contact': self._payload(contact.id)})
        try:
            flow_ns = sending.flow_for(automation_id)
        except ManyChatError:
            flow_ns = ''
        if not flow_ns:
            return _error('לא נמצאה ב-ManyChat אוטומציה לתבנית הזו.')
        name = name or sending.flow_name(flow_ns)
        message = sending.send_flow(contact, flow_ns, name or automation_id, request.user)
        if message.status in DELIVERED_STATUSES:
            handoff.office_answered(contact, request.user)
        lead_stage.refresh_quietly(contact.id)
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

    # --- ✓, the clock and the notes (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ג) ---

    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """
        POST contacts/{id}/close/ — "השיחה נסגרה" (✓): nobody is asked for any more,
        a future lead ends, the mark becomes "ענה", and the conversation goes
        back to the bot. 502 with nothing changed when a person holds it and
        ManyChat did not take it back.
        """
        contact = self.get_object()
        try:
            future_leads.close(contact, request.user)
        except handoff.HandoffError as exc:
            return _error(str(exc), http=status.HTTP_502_BAD_GATEWAY)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'], url_path='hand-to-bot')
    def hand_to_bot(self, request, pk=None):
        """
        POST contacts/{id}/hand-to-bot/ — the clock, "הבוט ימשיך לטפל": the
        conversation is read now as a lead and, when the bot then has a step
        with a time, handed back to it. 200 {contact, understood}: `understood.ok`
        false means nothing was changed, and `understood.text` says why.
        """
        contact = self.get_object()
        try:
            _contact, understood = future_leads.hand_to_bot(contact, request.user)
        except handoff.HandoffError as exc:
            return _error(str(exc), http=status.HTTP_502_BAD_GATEWAY)
        return Response({'contact': self._payload(contact.id), 'understood': understood})

    @action(detail=True, methods=['post'])
    def future(self, request, pk=None):
        """
        POST contacts/{id}/future/ {due, reason} — the clock, "ליד עתידי שלי". A
        day and a reason; the conversation is taken by a person so that no bot
        answers meanwhile. 200 {contact, mute: {muted, detail}} — the lead is
        kept also when the bot could not be muted, and `detail` says why.
        """
        contact = self.get_object()
        try:
            _contact, mute = future_leads.set_future(contact, request.data.get('due'), request.data.get('reason'), request.user)
        except future_leads.FutureLeadError as exc:
            return _error(str(exc))
        return Response({'contact': self._payload(contact.id), 'mute': mute})

    @action(detail=True, methods=['get', 'post'])
    def notes(self, request, pk=None):
        """
        GET contacts/{id}/notes/ → {notes}; POST {text} → 201 {note, contact}. A
        note is a line of the contact's journal: no message, sent to nobody.
        """
        contact = self.get_object()
        if request.method == 'GET':
            return Response({'notes': future_leads.notes(contact, future_leads.NOTES_LISTED)})
        try:
            event = future_leads.add_note(contact, request.data.get('text'), request.user)
        except future_leads.FutureLeadError as exc:
            return _error(str(exc))
        return Response(
            {'note': future_leads.note_payload(event), 'contact': self._payload(contact.id)}, status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['patch', 'delete'], url_path=r'notes/(?P<note_id>[0-9]+)')
    def note(self, request, pk=None, note_id=None):
        """
        PATCH contacts/{id}/notes/{note_id}/ {text} → 200 {note, contact};
        DELETE → 200 {deleted, contact} (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ז.5).
        404 for a note this contact does not have — also for a line of its
        journal that is no note. The journal keeps who and when, not the words.
        """
        contact = self.get_object()
        try:
            if request.method == 'DELETE':
                deleted = future_leads.delete_note(contact, note_id, request.user)
                return Response({'deleted': deleted, 'contact': self._payload(contact.id)})
            event = future_leads.edit_note(contact, note_id, request.data.get('text'), request.user)
        except future_leads.NoteNotFound:
            return _error('הפתק לא נמצא.', http=status.HTTP_404_NOT_FOUND)
        except future_leads.FutureLeadError as exc:
            return _error(str(exc))
        return Response({'note': future_leads.note_payload(event), 'contact': self._payload(contact.id)})

    # --- "הסר" (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.1.4) ---

    @action(detail=True, methods=['post'], url_path='opt-out')
    def opt_out(self, request, pk=None):
        """
        POST contacts/{id}/opt-out/ — a person marks that the customer asked not
        to get broadcast messages. The phone goes on the system's list; nothing
        is written in ManyChat and nothing is sent. 200 with the contact; one
        already marked comes back as it is. 503 when the list could not be
        written — nothing changed.
        """
        contact = self.get_object()
        try:
            optout.opt_out(contact, request.user)
        except OptOutListUnavailable as exc:
            return _error(exc.detail, code=exc.code, http=status.HTTP_503_SERVICE_UNAVAILABLE)
        lead_stage.refresh_quietly(contact.id)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'], url_path='opt-in')
    def opt_in(self, request, pk=None):
        """POST contacts/{id}/opt-in/ — "ביקש לחזור לתפוצה": off the list again. 200 with the contact; 503 as above."""
        contact = self.get_object()
        try:
            optout.opt_in(contact, request.user)
        except OptOutListUnavailable as exc:
            return _error(exc.detail, code=exc.code, http=status.HTTP_503_SERVICE_UNAVAILABLE)
        lead_stage.refresh_quietly(contact.id)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'], url_path='waiting-list')
    def waiting_list(self, request, pk=None):
        """
        POST contacts/{id}/waiting-list/ {lesson_id, child?} — the office puts the
        contact on the waiting list of a lesson that is full. 201 {entry, contact};
        409 when it is already there; 400 with `code` for a lesson that has room
        (has_room) or that is not one (no_lesson, bad_lesson).
        """
        contact = self.get_object()
        try:
            entry, created = waiting_list.join(
                contact, request.data.get('lesson_id'), child=str(request.data.get('child') or ''),
                by=waiting_list.WAITING_BY_PERSON, user=request.user,
            )
        except waiting_list.WaitingListError as exc:
            return _error(exc.detail, code=exc.code)
        entry = WaitingListEntry.objects.select_related(*waiting_list.ROW_RELATED).get(pk=entry.pk)
        body = {'entry': waiting_list.payloads([entry])[0], 'contact': self._payload(contact.id)}
        if not created:
            return Response(
                {'detail': 'הלקוח כבר ברשימת ההמתנה לשיעור הזה.', 'code': 'exists', **body}, status=status.HTTP_409_CONFLICT,
            )
        return Response(body, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='live-test')
    def live_test_switch(self, request, pk=None):
        """
        POST contacts/{id}/live-test/ {on: true|false} — the live test from the
        owner's phone (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב). On: the old bot is
        muted for this contact in ManyChat and the NEW bot answers it for real;
        off: the old bot answers again. `handled_by` is not touched. A demo
        contact is refused: nothing is ever sent to it.
        """
        contact = self.get_object()
        on = request.data.get('on')
        if not isinstance(on, bool):
            return _error('נדרש on (true או false).')
        if contact.is_demo:
            return _error(handoff.LIVE_TEST_DEMO, code='demo')
        try:
            (handoff.start_live_test if on else handoff.stop_live_test)(contact, request.user)
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
        lead_stage.refresh_quietly(contact.id)
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
            # The marks feed the lead stage: "נרשם" closes it, "בזמן אחר" with a day makes it wait.
            lead_stage.refresh_quietly(contact.id)
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
        lead_stage.refresh_quietly(contact.id)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        contact = self.get_object()
        analysis.analyze_contact(contact)
        lead_stage.refresh_quietly(contact.id)
        return Response(self._payload(contact.id))

    @action(detail=True, methods=['post'], url_path='simulate-inbound')
    def simulate_inbound(self, request, pk=None):
        """
        Demo mode: a message typed by the manager goes through the very door
        ManyChat's copy uses (inbound.store_event) — needs_human, needs_analysis,
        needs_shadow and all. Only for a contact marked is_demo.

        As multipart it also takes a recording or a picture (docs/WAHUB-CONTRACT-REAL-DEMO.md, א):
        `message_type` voice|image, `file`, and `text` as an optional caption.
        The file goes to the private bucket (apps/wahub/media.py) and the
        message carries its path.
        """
        contact = self.get_object()
        if not contact.is_demo:
            return _error('אפשר להקליד הודעות רק לאיש קשר דמו.', code='not_demo')
        message_type = str(request.data.get('message_type') or 'text').strip()
        if message_type not in ('text', 'voice', 'image'):
            return _error('message_type: text, voice או image.')
        text = str(request.data.get('text') or '').strip()
        sender = str(request.data.get('sender') or 'customer').strip()
        if sender not in ('customer', 'bot'):
            return _error('sender: customer או bot.')
        media_path = ''
        if message_type == 'text':
            if not text:
                return _error('נדרש טקסט להודעה.')
        else:
            if sender != 'customer':
                return _error('הקלטה או תמונה נשלחות רק בשם הלקוח.')
            upload = request.FILES.get('file')
            try:
                # Refused for its type or size before the storage is asked anything.
                media.check_upload(upload, message_type)
            except media.MediaError as exc:
                return _error(str(exc), code='bad_file')
            if not media.configured():
                return _error(media.NOT_CONFIGURED, code='storage_not_configured', http=status.HTTP_503_SERVICE_UNAVAILABLE)
            try:
                media_path = media.upload(contact.id, upload, message_type)
            except media.MediaError as exc:
                return _error(str(exc), code='storage_failed', http=status.HTTP_502_BAD_GATEWAY)
        event = inbound.EVENT_CUSTOMER_MESSAGE if sender == 'customer' else inbound.EVENT_BOT_REPLY
        result = inbound.store_event(
            event=event, phone=contact.phone, text=text, name=contact.name, message_type=message_type, media_url=media_path,
        )
        payload = self._payload(contact.id)
        payload['stored'] = result.stored
        payload['stored_message_id'] = result.message_id
        if result.message_id:
            payload['message'] = message_payload(Message.objects.get(pk=result.message_id))
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


class HistoryImportView(APIView):
    """
    GET history/import/[?again=1] → {switched_at, subscribers}: ManyChat's numbers of the conversations to bring.
    POST history/import/ {conversations: [{subscriber_id, messages: [{id, at, from, text, type}]}]} → the counts.
    The conversations from before the switch, as the office's own session in ManyChat shows them, a small batch a
    call (history.py). Lines already here are kept once; nothing is sent.
    """
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        try:
            return Response(history.still_to_copy(again=request.query_params.get('again') == '1'))
        except history.NoSwitch:
            return _error('הבוט החדש עוד לא עונה לכולם — אין עדיין "לפני המעבר" להעתיק.', code='no_switch')

    def post(self, request):
        conversations = request.data.get('conversations') if isinstance(request.data, dict) else None
        if not isinstance(conversations, list) or not conversations:
            return _error('אין שיחות להעתקה.', code='empty')
        if len(conversations) > history.MAX_CONVERSATIONS:
            return _error(f'עד {history.MAX_CONVERSATIONS} שיחות בכל פעם.', code='too_many')
        try:
            counts = history.import_conversations(conversations)
        except history.NoSwitch:
            return _error('הבוט החדש עוד לא עונה לכולם — אין עדיין "לפני המעבר" להעתיק.', code='no_switch')
        logger.info('wahub history import: %s', counts)
        return Response(counts)


def _stored_inbound_key():
    from apps.core.models import IntegrationCredential

    return IntegrationCredential.objects.filter(key=INBOUND_KEY_NAME).first()


API_PREFIX = '/api/v1'


def _own_url(request, path: str) -> str:
    """
    This server's address for one of its wahub endpoints. The public address
    when it is configured; on a developer's machine, the address this request
    came to. CRM_API_BASE_URL is written with or without the /api/v1 at its end
    (production has it without: the self-call of the live test went to
    /wahub/live-test/reply/ and got 404, 11.10.2026) — both are read.
    """
    base = (getattr(settings, 'CRM_API_BASE_URL', '') or '').strip().rstrip('/')
    if base and 'localhost' not in base and '127.0.0.1' not in base:
        if not base.endswith(API_PREFIX):
            base = f'{base}{API_PREFIX}'
        return f'{base}/wahub/{path}'
    return request.build_absolute_uri(f'/api/v1/wahub/{path}')


def _inbound_url(request) -> str:
    """The address to paste into ManyChat."""
    return _own_url(request, 'inbound/manychat/')


class StatusView(APIView):
    """What is connected, so the screen can say what its numbers are worth."""
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        from apps.core.scoping import integration_credential
        from apps.wahub import bot_mode, holiday_flows

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
            # Recordings: is there a key, and did OpenAI take the last one (no credit left is the usual "no").
            'transcribe': transcribe.health(),
            'send_configured': ManyChatService().is_configured,
            'sending_enabled': sending.sending_enabled(),
            'simulate_send': sending.simulate_send(),
            # The contacts the NEW bot answers for real while the switch is off (docs/WAHUB-CONTRACT-LIVE-TEST.md).
            'live_test_contacts': Contact.objects.filter(live_test=True).count(),
            # Whom the new bot answers for real — the test numbers or everybody — since when, what stopped it, and
            # what today's answers came to (docs/WAHUB-CONTRACT-GO-LIVE.md, ג.2).
            'bot': bot_mode.status(),
            # The holidays boards the bot sends (holiday_flows.py): which of their automations ManyChat has, by name.
            'holiday_flows': holiday_flows.status(now),
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
        # Before the message is kept: the moment the new bot began to answer everybody is set the first time
        # the mode "all" and sending are both seen on (bot_mode.live_since), so this very message is one it answers.
        live_since = _live_since_at_the_door()
        try:
            result = inbound.handle_payload(data, request.query_params.get('event') or '')
        except Exception:
            # ManyChat would only send it again; the fault is ours to read in the log.
            logger.exception('wahub inbound: could not store an event')
            return Response({'ok': True, 'stored': False})
        if result.live_test or _answered_by_the_mode(result, live_since):
            # At once, before anything else: somebody is waiting on a phone for the NEW bot's answer — the
            # owner under the live test (docs/WAHUB-CONTRACT-LIVE-TEST.md), or any customer once the mode is
            # "all" (docs/WAHUB-CONTRACT-GO-LIVE.md). The call does not wait and never raises.
            try:
                live_test.kick(result.contact_id, _own_url(request, live_test.REPLY_PATH))
            except Exception:
                logger.exception('wahub inbound: the live answer for contact %s could not be started', result.contact_id)
        elif result.media:
            # A recording or a picture: fetched and transcribed now, not at the next cron — the office
            # reads what the customer said within seconds. Not waited for; the cron does it anyway.
            _start_media(result.contact_id, _own_url(request, MEDIA_PREPARE_PATH))
        return Response({'ok': True, 'stored': result.stored})


def _live_since_at_the_door():
    """bot_mode.live_since, set if this is the first message that finds the mode live. Never raises: the message is kept anyway."""
    from apps.wahub import bot_mode

    try:
        return bot_mode.live_since(arm=True)
    except Exception:
        logger.exception('wahub inbound: the mode of the new bot could not be read')
        return None


def _answered_by_the_mode(result, live_since) -> bool:
    """
    Is this a customer's message the new bot answers because the owner chose
    "all": the mode is live, the message was kept just now, and its contact is
    neither a demo nor in a person's hands. The reply looks at all of it again
    (live_test.reply); this only spares the calls that would answer nothing.
    """
    if live_since is None or not result.stored or not result.message_id:
        return False
    from apps.wahub.models import DIRECTION_IN, HANDLED_HUMAN

    try:
        row = (
            Message.objects.filter(pk=result.message_id, direction=DIRECTION_IN)
            .values_list('contact__is_demo', 'contact__handled_by').first()
        )
    except Exception:
        # Not started from here: the cron's safety net starts it within minutes.
        logger.exception('wahub inbound: message %s could not be looked at for the live answer', result.message_id)
        return False
    return row is not None and not row[0] and row[1] != HANDLED_HUMAN


MEDIA_PREPARE_PATH = 'media/prepare/'


def _start_media(contact_id: int, url: str) -> None:
    """POST our own media/prepare/ and let go (the read timeout is the normal way out). Never raises."""
    try:
        token = live_test.cron_token()
        if not token:
            return
        response = requests.post(url, json={'contact': contact_id}, headers={'X-Cron-Token': token}, timeout=live_test.KICK_TIMEOUT)
        if response.status_code != 200:
            logger.warning('wahub inbound: preparing the file of contact %s answered %s', contact_id, response.status_code)
    except requests.ReadTimeout:
        pass
    except Exception as exc:
        logger.warning('wahub inbound: the file of contact %s was not prepared at once: %s', contact_id, type(exc).__name__)


class MediaPrepareView(APIView):
    """
    POST media/prepare/ {contact} — fetch the files of the customer's unanswered
    messages into the bucket and transcribe the recordings now
    (shadow.prepare_media). Called by this server on itself from the inbound
    door; auth is the cron token. Sends nothing and answers nobody: the shadow
    (or the live test) answers later and finds the words ready.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        from apps.customers.views import _cron_request_authorized

        if not _cron_request_authorized(request):
            return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        raw = request.data.get('contact') if isinstance(request.data, dict) else None
        if isinstance(raw, bool) or not str(raw or '').strip().isdigit():
            return _error('נדרש contact (מזהה איש הקשר).')
        contact = Contact.objects.filter(pk=int(raw)).first()
        if contact is None:
            return _error('איש הקשר לא נמצא.', http=status.HTTP_404_NOT_FOUND)
        seen = shadow.prepare_media(contact, deadline=time.monotonic() + MEDIA_PREPARE_SECONDS)
        if seen:
            # The screen learns that the messages changed (a transcript, a kept file).
            state.touch(contact.pk)
        return Response({'ok': True, 'prepared': len(seen)})


MEDIA_PREPARE_SECONDS = 50


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_tick(request):
    """One slice of summaries and matching. Auth: the same token as the other crons."""
    from apps.customers.views import _cron_request_authorized

    if not _cron_request_authorized(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
    # The address of the live answer: the cron starts again the answers that never started (live_test.answer_late).
    return Response({'ok': True, **cron.tick(reply_url=_own_url(request, live_test.REPLY_PATH))})


class LiveTestReplyView(APIView):
    """
    POST live-test/reply/ {contact} — the NEW bot answers the unanswered
    messages of a contact it answers (a live-test contact, or anybody once the
    mode is "all") NOW and sends the answer through ManyChat
    (apps/wahub/live_test.py). Called by this server on itself from the inbound
    door — and, with `late: true`, from the cron's safety net; auth is the cron
    token. Always 200 with `outcome` (sent, failed, busy, nothing, daily_limit,
    hourly_limit, too_old, human_handles, model_error, not_live_test); 404 for a
    contact that does not exist. A second call while one is answering is "busy":
    the first one answers what arrived meanwhile.

    {ping: true} answers {ok, pong} and does nothing else: the check, before
    the mode may become "all", that this server reaches this very door.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        from apps.customers.views import _cron_request_authorized

        if not _cron_request_authorized(request):
            return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        data = request.data if isinstance(request.data, dict) else {}
        if data.get('ping') is True:
            return Response({'ok': True, 'pong': True})
        raw = data.get('contact')
        if isinstance(raw, bool) or not str(raw or '').strip().isdigit():
            return _error('נדרש contact (מזהה איש הקשר).')
        result = live_test.reply(int(raw), late=data.get('late') is True)
        if result['outcome'] == live_test.OUTCOME_NOT_FOUND:
            return _error('איש הקשר לא נמצא.', http=status.HTTP_404_NOT_FOUND)
        return Response({'ok': True, **result})



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
        lead_stage.refresh_quietly(contact.id)
        payload = contact_payload(queries.listed().get(pk=contact.id))
        payload['shadow'] = [shadow.reply_payload(reply) for reply in contact.shadow_replies.order_by('-id')[:5]]
        payload['proposals'] = [reviewer.proposal_payload(row) for row in contact.proposals.order_by('-id')[:5]]
        return Response(payload, status=status.HTTP_201_CREATED)


class DemoContactsView(APIView):
    """DELETE demo/contacts/ — every demo contact with its messages, shadow replies, proposals and notes."""
    permission_classes = [IsAuthenticated, IsManager]

    def delete(self, request):
        return Response(demo.delete_demo_contacts())


# --- a real conversation with a demo contact (docs/WAHUB-CONTRACT-REAL-DEMO.md) ---------------------------

# The shadow bot's answer inside one request, with the transcription and the tool rounds it needs.
REPLY_SECONDS = 50


def _flag(value, default: bool) -> bool | None:
    """A JSON boolean, or its text from a form; None when it is neither."""
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ('true', '1', 'yes'):
        return True
    if text in ('false', '0', 'no'):
        return False
    return None


class DemoReplyView(APIView):
    """
    POST demo/contacts/{id}/reply/ {post: true|false} — the new bot answers the
    demo contact's unanswered messages NOW: the very code the cron runs
    (shadow.propose, one answer per burst), kept as a ShadowReply. With `post`
    the answer is also written into the conversation as the new bot's message
    (sending.keep_new_bot_reply: sender "bot", new_bot, status "simulated").
    Nothing is sent to anybody. A failure of Claude or of the transcription is
    200 with `shadow.error`, and nothing is kept, so the next call tries again.
    """
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request, pk):
        contact = Contact.objects.filter(pk=pk).first()
        if contact is None:
            return _error('איש הקשר לא נמצא.', http=status.HTTP_404_NOT_FOUND)
        if not contact.is_demo:
            return _error('הבוט החדש עונה בשיחה רק לאיש קשר דמו.', code='not_demo')
        post = _flag(request.data.get('post'), True)
        if post is None:
            return _error('post: true או false.')

        def payload_of(message):
            return {
                'message': message_payload(message) if message is not None else None,
                'contact': contact_payload(queries.listed().get(pk=contact.id)),
            }

        try:
            # The very computation the live test runs for the owner's phone (live_test.py).
            reply = shadow.answer_now(contact, deadline=time.monotonic() + REPLY_SECONDS)
        except shadow.ShadowModelError as exc:
            logger.warning('wahub demo reply for contact %s: %s', contact.pk, exc)
            return Response({'shadow': {'error': str(exc)}, **payload_of(None)})
        if reply is None:
            return Response({'shadow': None, **payload_of(None)})
        message = sending.keep_new_bot_reply(contact, reply.text) if post and reply.text else None
        if message is not None:
            lead_stage.refresh_quietly(contact.id)
        return Response({'shadow': shadow.reply_payload(reply), **payload_of(message)})


class MessageMediaView(APIView):
    """GET messages/{id}/media/ → {url, expires_in}: a short-lived signed address of a message's recording or picture."""
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request, pk):
        message = Message.objects.filter(pk=pk).only('id', 'media_url').first()
        if message is None or not message.media_url:
            return _error('להודעה הזאת אין קובץ.', code='no_media', http=status.HTTP_404_NOT_FOUND)
        if media.is_external(message.media_url):
            # ManyChat's own address of a customer's file, not fetched into the bucket yet (or the
            # fetch failed): it is not ours to sign, and it is opened where it is.
            return Response({'url': message.media_url, 'expires_in': None})
        try:
            url = media.signed_url(message.media_url)
        except media.MediaNotConfigured as exc:
            return _error(str(exc), code='storage_not_configured', http=status.HTTP_503_SERVICE_UNAVAILABLE)
        except media.MediaError as exc:
            return _error(str(exc), code='storage_failed', http=status.HTTP_502_BAD_GATEWAY)
        return Response({'url': url, 'expires_in': media.SIGNED_URL_SECONDS})


# --- stage 3: who asked and did not register; the WhatsApp block of a customer's card -----------------
# (docs/WAHUB-CONTRACT-STAGE3.md, א and ב). Nothing here sends a message.

class UnregisteredLeadsView(APIView):
    """GET leads/unregistered/?days=30&hot=0|1 — hot first, then whoever has waited longest. No paging: the list is short."""
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        now = timezone.now()
        today = timezone.localtime(now).date()
        days = queries.unregistered_days(request.query_params.get('days'))
        rows = list(queries.unregistered_leads(days, now=now))
        leads = [unregistered_lead_payload(row, today=today) for row in rows]
        if queries.flag(request.query_params.get('hot')):
            leads = [lead for lead in leads if lead['hot']]
        # The counts describe the whole window whatever the filter: the card's numbers do not move under it.
        return Response({'days': days, 'counts': queries.unregistered_counts(rows, today), 'leads': leads})


class LeadBoardView(APIView):
    """
    GET leads/board/ — the lead stages in their three groups (דורש טיפול ·
    בתהליך · סגור), each stage with how many contacts are in it and how long the
    longest has been there (docs/WAHUB-CONTRACT-LEAD-STAGES.md). A stage with
    nobody stays on the list. The people behind a line: contacts/?lead_stage=<key>.
    Reads only; `pending_scan` is how many contacts the cron has still to look at.
    """
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        return Response(lead_stage.board())


class WaitingListView(APIView):
    """
    GET waiting-list/?status=active|waiting|freed|closed|all&lesson=<uuid> — who
    waits for which lesson (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ד.4): the rows
    whose seat came free first, then whoever has waited longest. Reads only.
    """
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        found = Search(request.query_params.get('search') or request.query_params.get('q'))
        try:
            answer = waiting_list.listed(request.query_params, found=found)
        except waiting_list.WaitingListError as exc:
            return _error(exc.detail, code=exc.code)
        if found:
            # So that a number the system knows never reads "not found": who has it in the conversations
            # without being on this list, and the customers with no conversation at all (the same contract, ח.1.4).
            on_the_list = {row['contact_id'] for row in answer['results']}
            answer['contacts'] = found_elsewhere(found, Contact.objects.exclude(id__in=on_the_list))
            answer['customers'], answer['customers_more'] = found.customers()
        return Response(answer)


class WaitingListLessonsView(APIView):
    """
    GET waiting-list/lessons/?q=&branch=<uuid>&full=1&contact=<id> — the lessons a
    person chooses from when putting somebody on a waiting list (the same
    contract, ז.2), each with its seats as the form counts them. Reads only.
    """
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        try:
            return Response(waiting_list.lessons_to_choose(request.query_params))
        except waiting_list.WaitingListError as exc:
            return _error(exc.detail, code=exc.code)


class WaitingListNotifyView(APIView):
    """
    Telling one row's customer that a seat came free (the same contract, ז.4) —
    only ever on a person's press. GET: what the press would do now (the words,
    how they would leave, what stops them); reads only. POST {again?}: the press.
    200 {entry, contact, notify} — `notify.sent` says whether a message left,
    and why not when it did not; 409 {detail, code, entry, contact} for a row
    that is not one to tell about, or whose customer was already told of this
    seat and the press did not say `again`. Every door of sending.py stands.
    """
    permission_classes = [IsAuthenticated, IsManager]

    @staticmethod
    def _contact(entry):
        return contact_payload(queries.listed().get(pk=entry.contact_id))

    def get(self, request, pk):
        try:
            return Response(waiting_notify.preview(pk))
        except WaitingListEntry.DoesNotExist:
            return _error('הרשומה לא נמצאה.', http=status.HTTP_404_NOT_FOUND)

    def post(self, request, pk):
        again = request.data.get('again') in (True, 'true', '1', 1)
        try:
            result = waiting_notify.tell(pk, request.user, again=again)
        except WaitingListEntry.DoesNotExist:
            return _error('הרשומה לא נמצאה.', http=status.HTTP_404_NOT_FOUND)
        except waiting_notify.Refused as exc:
            entry = waiting_list.payloads([exc.entry])[0]
            return _error(
                exc.detail, code=exc.code, http=status.HTTP_409_CONFLICT, entry=entry, contact=self._contact(exc.entry),
                already_notified_at=entry['notified_at'], already_notified_by_name=entry['notified_by_name'],
            )
        notify = result['notify']
        notify['message'] = message_payload(notify['message']) if notify['message'] is not None else None
        return Response({
            'entry': waiting_list.payloads([result['entry']])[0],
            'contact': self._contact(result['entry']),
            'notify': notify,
        })


class WaitingListEntryView(APIView):
    """
    POST waiting-list/{id}/handled/ — "טופל"; POST waiting-list/{id}/remove/ —
    "להסיר מהרשימה". 200 {entry, contact}; a row already off the list comes back
    as it is. Nothing is sent to anybody.
    """
    permission_classes = [IsAuthenticated, IsManager]
    act = None

    def post(self, request, pk):
        try:
            entry = type(self).act(pk, request.user)
        except WaitingListEntry.DoesNotExist:
            return _error('הרשומה לא נמצאה.', http=status.HTTP_404_NOT_FOUND)
        return Response({
            'entry': waiting_list.payloads([entry])[0],
            'contact': contact_payload(queries.listed().get(pk=entry.contact_id)),
        })


class WaitingListHandledView(WaitingListEntryView):
    act = staticmethod(waiting_list.mark_handled)


class WaitingListRemoveView(WaitingListEntryView):
    act = staticmethod(waiting_list.remove)


def _family_for(raw):
    """(family, None) for a card the system has; (None, the refusal) otherwise."""
    text = str(raw or '').strip()
    if not text:
        return None, _error('נדרש family (מזהה המשפחה).')
    try:
        family_id = uuid.UUID(text)
    except ValueError:
        return None, _error('מזהה המשפחה אינו תקין.')
    family = Family.objects.filter(pk=family_id).only('id', 'phone').first()
    if family is None:
        return None, _error('המשפחה לא נמצאה.', http=status.HTTP_404_NOT_FOUND)
    return family, None


class ForCustomerView(APIView):
    """GET for-customer/?family=<uuid> — every WhatsApp contact behind the family's phones. Reads only."""
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        family, refused = _family_for(request.query_params.get('family'))
        if refused is not None:
            return refused
        phones, contacts = matching.contacts_of_family(family)
        return Response(for_customer_payload(family, phones, contacts))


class ForCustomerRecheckView(APIView):
    """
    POST for-customer/recheck/ {family} — the match against the registrations
    again, for each contact found; then the same answer as GET. The only
    write here is the kogo_* fields (matching.recheck_contact).
    """
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request):
        family, refused = _family_for(request.data.get('family'))
        if refused is not None:
            return refused
        phones, contacts = matching.contacts_of_family(family)
        for contact in contacts:
            matching.recheck_contact(contact)
            lead_stage.refresh_quietly(contact.id)
        return Response(for_customer_payload(family, phones, contacts))
