"""The tenant's signing page — /api/v1/rentals/sign/{token}/. No login; the token is the key.

    GET   sign/{token}/      the contract as the page shows it, and the link's state
    POST  sign/{token}/      {signer_name, signer_id_number, signature, accept: true}
    GET   sign/{token}/pdf/  the contract PDF; the signed copy once it is signed

Throttled with scopes of their own, the way card links are: reading (and the
PDF) on rental_sign_view, signing on rental_sign_submit. An unknown token is a
404 whatever it looks like, so the answers tell nothing about other links.
"""
from __future__ import annotations

import logging

from django.http import HttpResponse
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.rentals.models import RentalContract
from apps.rentals.signing import (
    NOT_FOUND,
    STATE_OPEN,
    STATE_SIGNED,
    SigningError,
    after_signing,
    clean_signing_input,
    link_state,
    mark_viewed,
    public_payload,
    public_pdf_path,
    resolve_sign_token,
    sign_contract,
)

logger = logging.getLogger(__name__)


def _not_found() -> Response:
    return Response({'error': NOT_FOUND}, status=status.HTTP_404_NOT_FOUND)


def _opened_by_staff(request) -> bool:
    """
    Whether the page was opened by someone logged in to the CRM.

    The office opens a link to check it before sending; that is not the tenant
    reading their contract, so it must not mark the contract "viewed". These
    views authenticate nobody, so the token is read here and only for this.
    """
    try:
        found = TokenAuthentication().authenticate(request)
    except AuthenticationFailed:
        return False
    return bool(found and found[0] and found[0].is_active)


class _PublicSigningView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'rental_sign_view'


class SigningPageView(_PublicSigningView):
    @property
    def throttle_scope(self):
        # One URL, two rates: reading is cheap, signing renders a PDF and writes evidence.
        return 'rental_sign_submit' if self.request.method == 'POST' else 'rental_sign_view'

    def get(self, request, token: str):
        contract = resolve_sign_token(token)
        if contract is None:
            return _not_found()
        state = link_state(contract)
        if state.state == STATE_OPEN and not _opened_by_staff(request):
            mark_viewed(contract)
        return Response(public_payload(contract, state))

    def post(self, request, token: str):
        contract = resolve_sign_token(token)
        if contract is None:
            return _not_found()
        # The state first: a tenant on a dead link is told so, whatever they typed.
        state = link_state(contract)
        if state.state != STATE_OPEN:
            return Response({'error': state.message, 'state': state.state}, status=state.status_code)
        try:
            signing = clean_signing_input(request.data)
            signed = sign_contract(contract, token, signing, request)
        except SigningError as exc:
            return Response(exc.payload(), status=exc.status_code)
        return Response({
            'state': STATE_SIGNED,
            'signed_at': signed.signed_at.isoformat(),
            'pdf_url': public_pdf_path(token),
            **after_signing(signed),
        })


class SigningPdfView(_PublicSigningView):
    def get(self, request, token: str):
        contract = resolve_sign_token(token)
        if contract is None:
            return _not_found()
        state = link_state(contract)
        if state.state not in (STATE_OPEN, STATE_SIGNED):
            return Response({'error': state.message, 'state': state.state}, status=status.HTTP_410_GONE)
        stored = RentalContract.objects.get(pk=contract.pk)
        if state.state == STATE_SIGNED:
            intact, data, filename = (
                stored.signed_pdf_is_intact(), stored.signed_pdf, f'rental-contract-v{stored.version}-signed.pdf',
            )
        else:
            intact, data, filename = stored.pdf_is_intact(), stored.pdf, f'rental-contract-v{stored.version}.pdf'
        if not intact:
            logger.error(
                'Rental contract %s (version %s): the %s PDF does not match its SHA-256; refusing to serve it',
                stored.pk, stored.version, state.state,
            )
            return Response(
                {'error': 'קובץ החוזה אינו תקין ולכן לא הורד. פנו למשרד'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        response = HttpResponse(bytes(data), content_type='application/pdf')
        # Inline: on a phone the contract opens in the browser's viewer.
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        # The same URL serves the unsigned contract and then the signed copy: a
        # phone must never show the tenant a cached unsigned one after signing.
        response['Cache-Control'] = 'no-store'
        return response
