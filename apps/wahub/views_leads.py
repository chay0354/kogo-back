"""
The API of the leads screen, version 2 — docs/WAHUB-CONTRACT-LEADS-V2.md, ד,
endpoint for endpoint — and of the "חזרה אוטומטית ללידים" card of the settings
(the same contract, י). Managers only, like the rest of the section. The lists
and the payloads are apps/wahub/leads.py and apps/wahub/lead_automation.py;
nothing here sends a message.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManager
from apps.wahub import lead_automation, leads
from apps.wahub.models import Contact


def _refused(detail: str, http=status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({'detail': detail}, status=http)


NOT_FOUND = 'איש הקשר לא נמצא.'


class _LeadView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def answer(self, act) -> Response:
        """Run one action of a person and answer with the lead as it now is."""
        try:
            contact = act()
        except Contact.DoesNotExist:
            return _refused(NOT_FOUND, status.HTTP_404_NOT_FOUND)
        except leads.LeadError as exc:
            return _refused(str(exc))
        return Response(leads.details_payload(contact))


class LeadListView(_LeadView):
    """
    GET leads/?owner=office|bot|closed&stage=&branch=&q=&cursor= →
    {counts: {office, bot, closed}, stages: [{key, label, count}], results: [...], next_cursor}.
    `counts` are of every lead, whatever the filter; `stages` are the stages
    somebody on this list is in, for the one filter of the screen.
    """

    def get(self, request):
        try:
            return Response(leads.listed(request.query_params))
        except leads.LeadError as exc:
            return _refused(str(exc))


class LeadDetailView(_LeadView):
    """GET leads/{contact_id}/ — the row, and `details`: why, the child, the trial, the follow-ups, a short timeline."""

    def get(self, request, pk):
        contact = Contact.objects.filter(pk=pk).first()
        if contact is None:
            return _refused(NOT_FOUND, status.HTTP_404_NOT_FOUND)
        return Response(leads.details_payload(contact))


class LeadCloseView(_LeadView):
    """POST leads/{contact_id}/close/ {reason: "deleted" | "not_interested", note?}."""

    def post(self, request, pk):
        return self.answer(lambda: leads.close(pk, str(request.data.get('reason') or '').strip(), request.data.get('note'), request.user))


class LeadReopenView(_LeadView):
    """POST leads/{contact_id}/reopen/ — a closed lead back into the lists."""

    def post(self, request, pk):
        return self.answer(lambda: leads.reopen(pk, request.user))


class LeadSnoozeView(_LeadView):
    """POST leads/{contact_id}/snooze/ {until: "YYYY-MM-DD", note?} — waits until that day."""

    def post(self, request, pk):
        return self.answer(lambda: leads.snooze(pk, request.data.get('until'), request.data.get('note'), request.user))


class LeadOwnerView(_LeadView):
    """POST leads/{contact_id}/owner/ {owner: "office" | "bot"} — "קח אליי" / "החזר לבוט"."""

    def post(self, request, pk):
        return self.answer(lambda: leads.set_owner(pk, str(request.data.get('owner') or '').strip(), request.user))


class LeadTemplatesView(_LeadView):
    """
    GET leads/templates/ — every text the bot may come back to a lead with, the
    five of stage 1 first: {template_key, kind_label, when_label, text, stage,
    mapped, target, target_name} (and kind, variant, step, label, when, days,
    active, required, suggested_name, connected_by, connected_at). `target` is
    the ManyChat automation connected to it — the flow "שלח תבנית" sends — or
    null; without one, a follow-up that is due after the 24-hour window stays due.
    """

    def get(self, request):
        return Response(lead_automation.templates())


class LeadTemplateView(_LeadView):
    """
    PUT leads/templates/{template_key}/ {target} — connect the text to a
    ManyChat automation (its id from `options/`, or its name as ManyChat spells
    it); {target: null} takes the connection off. Answers with the row as it now
    is. 400 with `detail` when ManyChat has no such automation, 502 when
    ManyChat could not be asked — nothing is kept either way. 404 for a key
    that is no template.
    """

    def put(self, request, template_key):
        data = request.data if isinstance(request.data, dict) else {}
        if 'target' not in data:
            return _refused('נדרש target: האוטומציה ב-ManyChat, או null כדי לנתק.')
        try:
            row = lead_automation.connect(
                template_key, data.get('target'), request.user, target_name=str(data.get('target_name') or ''),
            )
        except LookupError:
            return _refused('אין חזרה כזאת.', status.HTTP_404_NOT_FOUND)
        except lead_automation.TemplateError as exc:
            return _refused(str(exc), exc.status)
        return Response(row)


class LeadTemplateOptionsView(_LeadView):
    """
    GET leads/templates/options/ — the automations ManyChat has, to connect a
    text to: {options: [{id, name}]}. {options: null, reason} when ManyChat
    cannot be asked right now, and the screen takes the name typed in.
    """

    def get(self, request):
        return Response(lead_automation.options())


class LeadAutomationView(_LeadView):
    """
    GET leads/automation/ — "does it all run by itself, and if not, what is
    missing": {ready, lines: [classify, schedule, sending, templates], today,
    upcoming, banner}. Every text is ready to show.
    """

    def get(self, request):
        return Response(lead_automation.status())
