"""
The two switches of the NEW bot — docs/WAHUB-CONTRACT-GO-LIVE.md, ג.1 — for the
settings screen: whom it answers for real (the test numbers, or everybody), and
whether its follow-ups to leads may leave. Managers only, like every setting.
What the switches do is apps/wahub/bot_mode.py; nothing here sends a message,
and neither switch opens anything while the owner's general switch
(WAHUB_SENDING_ENABLED) is off.

All four calls answer with the same body (bot_mode.status with the log), so a
screen that changed one switch reads both from the answer.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManager
from apps.wahub import bot_mode, live_test


def _state() -> dict:
    return bot_mode.status(with_log=True)


def _refused(exc: bot_mode.Refused) -> Response:
    body = {'detail': exc.detail, 'code': exc.code}
    if exc.problems:
        # Refused because the server cannot answer customers: the screen shows what is missing beside what stands.
        body = {**_state(), **body, 'problems': exc.problems}
    return Response(body, status=exc.status)


class BotModeView(APIView):
    """
    GET settings/bot-mode/ — whom the new bot answers, whether that is live and
    since when, what stopped it, today's numbers, the follow-ups' switch, the log.

    POST {mode: "test" | "all", confirm?} — "all" needs `confirm: "all"`, and is
    refused (409 `not_ready`, with `problems`) while this server cannot answer:
    no Claude key, no ManyChat, or it does not reach its own reply door. With
    sending off "all" is kept and nothing leaves — `warning` says so. 200 with
    the same body as GET.
    """
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        return Response(_state())

    def post(self, request):
        from apps.wahub.views import _own_url

        data = request.data if isinstance(request.data, dict) else {}
        try:
            bot_mode.set_mode(
                data.get('mode'), request.user, confirm=data.get('confirm'),
                self_call=lambda: live_test.ping(_own_url(request, live_test.REPLY_PATH)),
            )
        except bot_mode.Refused as exc:
            return _refused(exc)
        return Response(_state())


class BotFollowupsView(APIView):
    """
    GET settings/followups/ — the same body as settings/bot-mode/.

    POST {on: true | false, confirm?} — turning them on needs `confirm: "on"`.
    From then on a follow-up leaves only when it falls due after the switch
    became live; what was waiting is let go, never sent late.
    """
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        return Response(_state())

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            bot_mode.set_followups(data.get('on'), request.user, confirm=data.get('confirm'))
        except bot_mode.Refused as exc:
            return _refused(exc)
        return Response(_state())
