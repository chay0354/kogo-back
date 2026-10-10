"""
The API of "הבוט" — the knowledge, the shadow bot and the review
(docs/WAHUB-CONTRACT-STAGE2.md, endpoint for endpoint). Managers only.

Nothing here sends a message. The only thing that changes the knowledge is a
manager's own save, restore, or approval of a proposal.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManager
from apps.wahub import knowledge, knowledge_import, reviewer, shadow
from apps.wahub.models import (
    PROPOSAL_PENDING,
    PROPOSAL_STATUS_CHOICES,
    VERDICT_BAD,
    VERDICT_CHOICES,
    Contact,
    KnowledgeHistory,
    KnowledgeItem,
    KnowledgeProposal,
    ServiceNote,
    ShadowReply,
    TrialQuestion,
)

logger = logging.getLogger(__name__)

TRY_DEADLINE_SECONDS = 50
VERDICTS = dict(VERDICT_CHOICES)
PROPOSAL_STATUSES = dict(PROPOSAL_STATUS_CHOICES)


def _error(detail: str, http=status.HTTP_400_BAD_REQUEST, **extra) -> Response:
    return Response({'detail': detail, **extra}, status=http)


class _ManagerViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, IsManager]
    pagination_class = None
    filter_backends = []
    lookup_value_regex = r'\d+'


# --- knowledge ---------------------------------------------------------------------------------------------------

class KnowledgeViewSet(_ManagerViewSet):
    queryset = KnowledgeItem.objects.all()

    def list(self, request):
        return Response([knowledge.item_payload(item) for item in knowledge.listed(request.query_params)])

    def retrieve(self, request, pk=None):
        return Response(knowledge.item_payload(self.get_object()))

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            item = knowledge.create_item(dict(data), request.user, note=str(data.get('note') or 'נוצר מהמסך'))
        except knowledge.KnowledgeError as exc:
            return _error(str(exc))
        return Response(knowledge.item_payload(item), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        item = self.get_object()
        data = request.data if isinstance(request.data, dict) else {}
        if not data:
            return _error('אין מה לעדכן.')
        try:
            item = knowledge.update_item(item, dict(data), request.user, note=str(data.get('note') or 'עודכן מהמסך'))
        except knowledge.KnowledgeError as exc:
            return _error(str(exc))
        return Response(knowledge.item_payload(item))

    def destroy(self, request, pk=None):
        """A soft delete: the item is kept, inactive, with its history."""
        item = self.get_object()
        if item.is_active:
            knowledge.soft_delete(item, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        item = self.get_object()
        rows = KnowledgeHistory.objects.filter(item=item).select_related('changed_by').order_by('-version')
        return Response([knowledge.history_payload(row) for row in rows])

    @action(detail=True, methods=['post'])
    def restore(self, request, pk=None):
        item = self.get_object()
        raw = request.data.get('version')
        if not isinstance(raw, int) or isinstance(raw, bool):
            return _error('נדרש version (מספר).')
        try:
            item = knowledge.restore(item, raw, request.user)
        except knowledge.KnowledgeError as exc:
            return _error(str(exc))
        return Response(knowledge.item_payload(item))

    @action(detail=False, methods=['get'], url_path='from-kogo')
    def from_kogo(self, request):
        return Response(knowledge.from_kogo())

    @action(detail=False, methods=['get'], url_path='office-hours/now')
    def office_hours_now(self, request):
        return Response(knowledge.office_hours_now())

    @action(detail=False, methods=['post'], url_path='import')
    def import_old_bot(self, request):
        """
        Files the old bot's knowledge (knowledge_seed, 137 records) from the
        screen — production has no shell. Once: a record that already exists is
        left alone, so the owner's edits survive a second click. Sends nothing.
        """
        data = request.data if isinstance(request.data, dict) else {}
        dry_run = bool(data.get('dry_run'))
        result = knowledge_import.run(user=request.user, dry_run=dry_run)
        return Response({
            'dry_run': dry_run,
            'created_total': result['created_total'],
            'created': {kind: count for kind, count in sorted(result['created'].items())},
            'skipped': result['skipped'],
            'inactive': result['inactive'],
            'tags': result['tags'],
            'quick_replies': result['quick_replies'],
            'total': KnowledgeItem.objects.count(),
        })


# --- the shadow bot --------------------------------------------------------------------------------------------------

def _moment(raw):
    """An ISO datetime from the screen; Israel's clock when no zone is given. None when unreadable."""
    if raw in (None, ''):
        return None
    moment = parse_datetime(str(raw)) if not isinstance(raw, datetime) else raw
    if moment is None:
        return None
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


class ShadowViewSet(_ManagerViewSet):
    queryset = ShadowReply.objects.all()

    @action(detail=True, methods=['post'])
    def verdict(self, request, pk=None):
        """👍 / 👎 by the owner. 👎 with a note also becomes a proposal to fix the knowledge."""
        reply = self.get_object()
        verdict = str(request.data.get('verdict') or '').strip()
        if verdict not in VERDICTS:
            return _error('נדרש verdict: good או bad.')
        note = str(request.data.get('note') or '').strip()[:2000]
        reply.verdict, reply.verdict_note, reply.verdict_by, reply.verdict_at = verdict, note, request.user, timezone.now()
        reply.save(update_fields=['verdict', 'verdict_note', 'verdict_by', 'verdict_at'])
        proposal = None
        if verdict == VERDICT_BAD and note:
            try:
                proposal = reviewer.propose_from_verdict(reply, note, request.user)
            except Exception:
                logger.exception('wahub: proposal from verdict on shadow %s failed', reply.pk)
        payload = shadow.reply_payload(reply)
        payload['proposal_id'] = proposal.id if proposal else None
        return Response(payload)

    @action(detail=False, methods=['post'], url_path='try')
    def try_question(self, request):
        """"נסה שאלה": the same draft for a typed question, now or at a pretend time."""
        question = str(request.data.get('question') or '').strip()
        if not question:
            return _error('נדרשת שאלה.')
        if len(question) > 4096:
            return _error('השאלה ארוכה מדי.')
        contact = None
        raw_contact = request.data.get('contact_id')
        if raw_contact not in (None, ''):
            contact = Contact.objects.filter(pk=raw_contact).first() if str(raw_contact).isdigit() else None
            if contact is None:
                return _error('איש הקשר לא נמצא.')
        pretend_now = _moment(request.data.get('pretend_now'))
        if request.data.get('pretend_now') and pretend_now is None:
            return _error('pretend_now לא קריא (ISO 8601).')
        last_outbound = str(request.data.get('last_outbound') or '').strip()[:4096] or None
        result = shadow.answer(
            question, contact=contact, pretend_now=pretend_now, last_outbound=last_outbound,
            deadline=time.monotonic() + TRY_DEADLINE_SECONDS,
        )
        payload = shadow.draft_payload(result)
        payload.update({'question': question, 'pretend_now': pretend_now.isoformat() if pretend_now else None, 'trial_question_id': None})
        if request.data.get('save') is True:
            saved = TrialQuestion.objects.create(
                question=question, contact=contact, pretend_now=pretend_now, last_outbound=last_outbound or '',
                answer=payload, created_by=request.user,
            )
            payload['trial_question_id'] = saved.id
        return Response(payload)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        return Response(shadow.summary())

    @action(detail=False, methods=['get'])
    def bad(self, request):
        """"לתקן": the replies the owner marked bad, newest first."""
        rows = ShadowReply.objects.filter(verdict=VERDICT_BAD).select_related('contact', 'after_message', 'verdict_by').order_by('-verdict_at', '-id')[:200]
        return Response([shadow.reply_payload(reply, with_contact=True) for reply in rows])

    @action(detail=False, methods=['get'])
    def recent(self, request):
        """The latest proposals across every conversation, for the "תשובות בצל" list."""
        rows = ShadowReply.objects.select_related('contact', 'after_message', 'verdict_by').order_by('-id')[:100]
        return Response([shadow.reply_payload(reply, with_contact=True) for reply in rows])


# --- the review ------------------------------------------------------------------------------------------------------

class ReviewProposalViewSet(_ManagerViewSet):
    queryset = KnowledgeProposal.objects.select_related('contact', 'decided_by')

    def list(self, request):
        rows = self.get_queryset()
        wanted = (request.query_params.get('status') or '').strip()
        if wanted:
            statuses = [part for part in wanted.split(',') if part in PROPOSAL_STATUSES]
            rows = rows.filter(status__in=statuses) if statuses else rows.none()
        source = (request.query_params.get('source') or '').strip()
        if source:
            rows = rows.filter(source=source)
        return Response([reviewer.proposal_payload(row) for row in rows.order_by('-id')[:300]])

    def retrieve(self, request, pk=None):
        return Response(reviewer.proposal_payload(self.get_object()))

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        proposal = self.get_object()
        try:
            item = reviewer.approve(proposal, request.user, str(request.data.get('note') or ''))
        except reviewer.ReviewError as exc:
            return _error(str(exc), http=status.HTTP_409_CONFLICT if 'כבר' in str(exc) else status.HTTP_400_BAD_REQUEST)
        proposal.refresh_from_db()
        return Response({'proposal': reviewer.proposal_payload(proposal), 'item': knowledge.item_payload(item)})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        proposal = self.get_object()
        note = str(request.data.get('note') or '').strip()
        try:
            reviewer.reject(proposal, request.user, note)
        except reviewer.ReviewError as exc:
            return _error(str(exc), http=status.HTTP_409_CONFLICT)
        proposal.refresh_from_db()
        return Response(reviewer.proposal_payload(proposal))


class ReviewNotesView(APIView):
    """POST review/notes/ — "הבוט טעה כאן": a line from the office that becomes a proposal."""
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request):
        text = ' '.join(str(request.data.get('text') or '').split())[:2000]
        if not text:
            return _error('נדרש טקסט להערה.')
        contact = message = None
        raw_contact = request.data.get('contact_id')
        if raw_contact not in (None, ''):
            contact = Contact.objects.filter(pk=raw_contact).first() if str(raw_contact).isdigit() else None
            if contact is None:
                return _error('איש הקשר לא נמצא.')
        raw_message = request.data.get('message_id')
        if raw_message not in (None, ''):
            message = None
            if str(raw_message).isdigit():
                from apps.wahub.models import Message
                message = Message.objects.filter(pk=raw_message).first()
            if message is None:
                return _error('ההודעה לא נמצאה.')
            if contact is None:
                contact = message.contact
        note = ServiceNote.objects.create(text=text, contact=contact, message=message, created_by=request.user)
        proposal = None
        try:
            proposal = reviewer.propose_from_note(note)
        except Exception:
            logger.exception('wahub: proposal from service note %s failed', note.pk)
        if proposal is not None:
            note.proposal = proposal
            note.save(update_fields=['proposal'])
        body = {'note_id': note.id, 'proposal_id': proposal.id if proposal else None}
        if proposal is None:
            body['detail'] = 'לא הבנתי, פרט: כתבו מה הבוט עשה ומה היה צריך לעשות.'
        return Response(body, status=status.HTTP_201_CREATED)

    def get(self, request):
        rows = ServiceNote.objects.select_related('contact', 'created_by', 'proposal').order_by('-id')[:200]
        from apps.wahub import state
        return Response([{
            'id': row.id, 'text': row.text, 'contact_id': row.contact_id, 'message_id': row.message_id,
            'proposal_id': row.proposal_id, 'proposal_status': row.proposal.status if row.proposal_id and row.proposal else None,
            'by_name': state.user_display_name(row.created_by) if row.created_by_id else None,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        } for row in rows])


class ReviewSummaryView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        return Response(reviewer.summary())
