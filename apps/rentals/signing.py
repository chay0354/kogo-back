"""Signing a rental contract through a short link: the office sends it, the tenant reads it and signs.

    issue_signing_link(contract)         a new link for the tenancy's current open contract → sent
    cancel_signing_link(contract)        withdraw the link → draft
    resolve_sign_token(token)            the contract a link points at, or None
    link_state(contract)                 what the link shows now: open, signed, expired or cancelled
    public_payload(contract, state)      the signing page, from the contract's frozen terms
    mark_viewed(contract)                the first open of a sent link
    clean_signing_input(data)            the tenant's name, ID and signature, checked
    sign_contract(contract, token, ...)  record the signature and the signed copy, in one transaction
    after_signing(contract)              what the page does next, once it is signed
    signed_next_step(contract)           ... and again, every time the signed page is opened

The link is <frontend>/s/<sign_token>, built the way card links are built, and
does not expire: it closes when the contract is signed, when the office
withdraws it, or when a new version retires it. The page shows the contract
from its frozen terms only — never from the live tenancy — because that is what
the tenant signs, and the signature is bound to their fingerprint (terms_sha256).

Signing re-checks everything under the same locks, in the same order, as
issuing a version (tenancy first, then the contract): a signature can never
land on a contract that a new version voided a moment before, nor a void on
one being signed. Nothing here charges or sends WhatsApp; the one side effect
outside the database, the signed copy by email, runs after the commit and
never fails the signing.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import timedelta
from typing import NamedTuple

from django.db import transaction
from django.db.models import Prefetch, prefetch_related_objects
from django.urls import reverse
from django.utils import timezone
from PIL import Image as PILImage

from apps.core.card_validation import israeli_id_valid
from apps.core.frontend_url import public_frontend_url
from apps.payment_links.models import short_token
from apps.rentals.contracts import HEAVY_COLUMNS, ContractError, contract_is_stale, current_contract
from apps.rentals.models import RentalContract, Tenancy, sha256_hex
from apps.rentals.signed_email import send_signed_contract_email
from apps.scheduling.models import ScheduleEvent
from apps.scheduling.rental_agreement.generator import SignedBy, generate_tenancy_contract_pdf, row_when
from apps.scheduling.rental_agreement.terms import terms_sha256
from apps.scheduling.rental_agreement.text import contract_html, contract_paragraphs
from apps.signatures.capture import client_ip, decode_signature_png
from apps.signatures.models import Signature

logger = logging.getLogger(__name__)

# A link to sign has no lifetime. A tenant who gets round to it a month later
# still opens their contract; the office cancels or re-sends when it wants the
# old link dead. Nothing is at stake in an unsigned link that time protects:
# it shows the version the office chose to send, and signing it is what the
# office asked for whenever it happens.
#
# The 30 days below are the opposite case, and are kept deliberately: once the
# contract is signed there is nothing left for the tenant to do on the page,
# and what stays on it is their ID, phone and e-mail. A message forwarded on
# should not open those for ever, so the signed link — and only the signed one
# — closes. The office keeps the signed copy and re-sends it by hand.
SIGNED_LINK_LIFETIME = timedelta(days=30)
# A token is short_token(): 10 letters and digits. Anything else is not looked up at all.
_TOKEN_PATTERN = re.compile(r'[A-Za-z0-9]{6,32}')

STATE_OPEN = 'open'
STATE_SIGNED = 'signed'
STATE_EXPIRED = 'expired'
STATE_CANCELLED = 'cancelled'

# What the tenant reads.
NOT_FOUND = 'הקישור לא נמצא. בדקו שהועתק במלואו, או בקשו מהמשרד קישור חדש'
UPDATED = 'החוזה עודכן — בקשו מהמשרד קישור חדש'
WITHDRAWN = 'הקישור בוטל — בקשו מהמשרד קישור חדש'
ALREADY_SIGNED = 'החוזה כבר נחתם'
SIGNED_CLOSED = 'החוזה נחתם. לעותק נוסף — פנו למשרד'
BROKEN = 'לא ניתן לחתום על החוזה כרגע. פנו למשרד'

# What the office reads.
STALE = 'ההסכם השתנה אחרי שהחוזה הופק — הפיקו גרסה חדשה'

# The signature pad is a few hundred pixels a side even at 3× density. A PNG
# far bigger than that is not a signature, and decoding it is not free.
_MAX_SIGNATURE_SIDE_PX = 4000


class SigningError(ValueError):
    """A signing the tenant cannot complete. The message is shown to them as is."""

    def __init__(self, message: str, status_code: int = 400, state: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.state = state

    def payload(self) -> dict:
        body = {'error': self.message}
        if self.state:
            body['state'] = self.state
        return body


class LinkState(NamedTuple):
    state: str          # open, signed, expired or cancelled
    message: str        # why it is not open, in Hebrew; '' when open
    status_code: int    # the answer a signing attempt gets in this state


# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------

def new_sign_token() -> str:
    while True:
        token = short_token()
        if not RentalContract.objects.filter(sign_token=token).exists():
            return token


def link_is_live(contract) -> bool:
    """
    Out with the tenant: sent or viewed, with a token that still resolves.

    Time does not close it. Whether the agreement changed since is not checked
    here either — only the signing refuses a stale contract.
    """
    return contract.status in RentalContract.LINKED_STATUSES and bool(contract.sign_token)


def signing_url(contract, request=None) -> str:
    """`<frontend>/s/<token>`: CRM_FRONTEND_URL, else the trusted origin of the office's request."""
    return f'{public_frontend_url(request).rstrip("/")}/s/{contract.sign_token}'


def public_pdf_path(token: str) -> str:
    return reverse('rental-sign-pdf', args=[token])


def _tenancy_with_slots(tenancy_id, *, lock: bool = False):
    """The tenancy as contract_is_stale reads it. Locked alone, without joins, as issue_contract locks it."""
    queryset = Tenancy.objects.select_for_update() if lock else Tenancy.objects.all()
    tenancy = queryset.get(pk=tenancy_id)
    prefetch_related_objects(
        [tenancy], Prefetch('slots', queryset=ScheduleEvent.objects.select_related('studio')),
    )
    return tenancy


def issue_signing_link(contract) -> RentalContract:
    """
    A new link for the contract: the first one, or a replacement that retires the one before.

    Only the tenancy's current contract, while it is open and still says what
    the tenancy says. The contract becomes 'sent'; the 14 days start again and
    the new link has not been opened yet.
    """
    with transaction.atomic():
        tenancy = _tenancy_with_slots(contract.tenancy_id, lock=True)
        locked = RentalContract.objects.select_for_update().defer(*HEAVY_COLUMNS).get(pk=contract.pk)
        if locked.status == RentalContract.STATUS_SIGNED:
            raise ContractError('החוזה כבר נחתם, ולכן אין לו קישור חדש')
        if locked.status == RentalContract.STATUS_VOID:
            raise ContractError('החוזה בוטל. יש להפיק גרסה חדשה ולשלוח אותה')
        current = current_contract(tenancy)
        if current is None or current.pk != locked.pk:
            raise ContractError('זו אינה הגרסה העדכנית של החוזה. יש לשלוח את הגרסה העדכנית')
        if contract_is_stale(tenancy, locked):
            raise ContractError(STALE)
        now = timezone.now()
        locked.sign_token = new_sign_token()
        locked.sign_token_created_at = now
        locked.status = RentalContract.STATUS_SENT
        locked.sent_at = now
        locked.viewed_at = None
        locked.save(update_fields=['sign_token', 'sign_token_created_at', 'status', 'sent_at', 'viewed_at'])
    return locked


def cancel_signing_link(contract) -> RentalContract:
    """Withdraw the link: the token is cleared, so the URL stops resolving, and the contract is a draft again."""
    with transaction.atomic():
        locked = RentalContract.objects.select_for_update().defer(*HEAVY_COLUMNS).get(pk=contract.pk)
        if locked.status == RentalContract.STATUS_SIGNED:
            raise ContractError('אי אפשר לבטל את הקישור של חוזה חתום')
        if locked.status == RentalContract.STATUS_VOID:
            raise ContractError('החוזה בוטל, ואין לו קישור לביטול')
        if not locked.sign_token:
            raise ContractError('לחוזה הזה אין קישור פעיל לביטול')
        locked.sign_token = None
        locked.sign_token_created_at = None
        locked.status = RentalContract.STATUS_DRAFT
        # sent_at and viewed_at describe the link that was out. With none out,
        # a draft reads as never sent.
        locked.sent_at = None
        locked.viewed_at = None
        locked.save(update_fields=['sign_token', 'sign_token_created_at', 'status', 'sent_at', 'viewed_at'])
    return locked


def resolve_sign_token(token):
    """The contract the link points at, with its signature (without the heavy columns), or None."""
    raw = (token or '').strip()
    if not _TOKEN_PATTERN.fullmatch(raw):
        return None
    return (
        RentalContract.objects.select_related('signature')
        .defer('pdf', 'signed_pdf', 'signature__signature_png', 'signature__document_html')
        .filter(sign_token=raw)
        .first()
    )


def link_state(contract, *, now=None) -> LinkState:
    """
    What the link shows now.

    signed    — the contract is signed; the link keeps showing it, and its signed copy.
    cancelled — the contract is out of play: a newer version replaced it
                ("updated") or the office voided it ("withdrawn") — 409 to a
                signing attempt — or its link was withdrawn (410).
    expired   — only the 30-day close of a link that was already signed (410).
                An unsigned link is never closed by time.
    open      — the page shows it and the tenant may try to sign.

    Whether the tenancy changed since the contract was issued (is_stale) is
    not part of the state: the page keeps showing the frozen terms, and only
    the signing refuses a stale contract (sign_contract, 409).
    """
    if contract.status == RentalContract.STATUS_SIGNED:
        if contract.signed_at and (now or timezone.now()) - contract.signed_at > SIGNED_LINK_LIFETIME:
            return LinkState(STATE_EXPIRED, SIGNED_CLOSED, 410)
        return LinkState(STATE_SIGNED, ALREADY_SIGNED, 409)
    # "Not the current one": a newer version exists. It voided this one when it
    # was issued, so this is the void case too, told apart only by its message.
    replaced = RentalContract.objects.filter(tenancy_id=contract.tenancy_id, version__gt=contract.version).exists()
    if replaced or contract.status == RentalContract.STATUS_VOID:
        return LinkState(STATE_CANCELLED, UPDATED if replaced else WITHDRAWN, 409)
    if not link_is_live(contract):
        return LinkState(STATE_CANCELLED, WITHDRAWN, 410)
    return LinkState(STATE_OPEN, '', 200)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if value else None


def _public_slot(row: dict, terms: dict) -> dict:
    """A row of the contract's payment table, as the page lists it."""
    return {
        'kind': row['kind'],
        'weekday': row['weekday'],
        'date': row['date'],
        'day_label': row_when(row),
        'start_time': row['start_time'],
        'end_time': row['end_time'],
        'branch_name': (terms.get('branch') or {}).get('name'),
        'studio': row.get('studio'),
        'rate': row['rate'],
        'sum': row['sum'],
    }


def public_payload(contract, state: LinkState) -> dict:
    """
    The signing page, from the contract's frozen terms — never from the live tenancy.

    A link that is expired or cancelled shows no contract at all: only its
    state and why, and the version it was for.
    """
    if state.state not in (STATE_OPEN, STATE_SIGNED):
        return {'state': state.state, 'message': state.message, 'version': contract.version}
    terms = contract.terms
    tenant = terms.get('tenant') or {}
    studio = terms.get('studio') or {}
    signature = contract.signature if contract.signature_id else None
    return {
        'state': state.state,
        'version': contract.version,
        'tenant': {key: tenant.get(key) or '' for key in ('name', 'id_number', 'company_number', 'phone', 'email')},
        'branch_name': (terms.get('branch') or {}).get('name'),
        'studio': {key: studio.get(key) or '' for key in ('name', 'company_number', 'phone', 'email')},
        'slots': [_public_slot(row, terms) for row in terms.get('rows') or []],
        'monthly_amount': terms['monthly_amount'],
        'vat_rate': terms['vat_rate'],
        'vat_amount': terms['vat_amount'],
        'monthly_total': terms['monthly_total'],
        'billing_day': terms.get('billing_day'),
        'start_date': terms.get('start_date'),
        'end_date': terms.get('end_date'),
        'document': contract_paragraphs(terms),
        'signed_at': _iso(contract.signed_at),
        'signer_name': signature.signer_name if signature else '',
        # A link to sign no longer expires, so the page has no date to print.
        # The field stays, always null, so nothing downstream has to change.
        'expires_at': None,
        'pdf_url': public_pdf_path(contract.sign_token),
    }


def mark_viewed(contract) -> None:
    """The first open of a sent link: 'viewed', and when. A conditional update, so it happens once and never crosses a signing."""
    now = timezone.now()
    opened = RentalContract.objects.filter(
        pk=contract.pk, status=RentalContract.STATUS_SENT, sign_token=contract.sign_token,
    ).update(status=RentalContract.STATUS_VIEWED, viewed_at=now)
    if opened:
        contract.status = RentalContract.STATUS_VIEWED
        contract.viewed_at = now


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

class SigningInput(NamedTuple):
    signer_name: str
    signer_id_number: str
    png: bytes


def normalize_signer_id(value) -> str | None:
    """
    An Israeli ID number with a valid check digit (left-padded to 9 digits, as
    they are often typed without their leading zeros), or a 9-digit company
    number (every Israeli corporation's starts with 5). Spaces and dashes are
    ignored. None when it is neither.
    """
    digits = re.sub(r'[\s\-]', '', str(value or ''))
    if not digits.isdigit() or not digits.isascii():
        return None
    if 5 <= len(digits) <= 9 and israeli_id_valid(digits.zfill(9)):
        return digits.zfill(9)
    if len(digits) == 9 and digits.startswith('5'):
        return digits
    return None


def _signature_image_problem(png: bytes) -> str:
    """'' for an image with something drawn on it; else what is wrong with it, in Hebrew."""
    try:
        with PILImage.open(io.BytesIO(png)) as image:
            width, height = image.size
            if not (0 < width <= _MAX_SIGNATURE_SIDE_PX and 0 < height <= _MAX_SIGNATURE_SIDE_PX):
                return 'החתימה לא התקבלה. חתמו שוב בתוך המסגרת'
            image.load()
            # One colour throughout — transparent, or a white canvas — is a pad nobody drew on.
            if all(low == high for low, high in image.convert('RGBA').getextrema()):
                return 'החתימה ריקה. חתמו באצבע בתוך המסגרת'
    except Exception:
        return 'החתימה לא התקבלה. חתמו שוב בתוך המסגרת'
    return ''


def clean_signing_input(data) -> SigningInput:
    """The tenant's submission, checked. Raises a SigningError (400) with the first thing to fix."""
    if not isinstance(data, dict):
        raise SigningError('הבקשה אינה תקינה')
    # JSON true only: the page's one checkbox accepts the contract and the
    # computerized documents together, and nothing else stands for it.
    if data.get('accept') is not True:
        raise SigningError('יש לאשר את תנאי החוזה ואת קבלת המסמכים הממוחשבים לפני החתימה')
    name = ' '.join(str(data.get('signer_name') or '').split())
    if len(name) < 2:
        raise SigningError('יש להזין את השם המלא של החותם')
    if len(name) > 200:
        raise SigningError('השם ארוך מדי')
    id_number = normalize_signer_id(data.get('signer_id_number'))
    if id_number is None:
        raise SigningError('מספר תעודת הזהות או מספר החברה אינו תקין')
    png, _problem = decode_signature_png(data.get('signature'))
    if png is None:
        raise SigningError('החתימה לא התקבלה. חתמו שוב בתוך המסגרת')
    problem = _signature_image_problem(png)
    if problem:
        raise SigningError(problem)
    return SigningInput(signer_name=name, signer_id_number=id_number, png=png)


def sign_contract(contract, token: str, signing: SigningInput, request) -> RentalContract:
    """
    Sign the contract the token opened, with what the tenant submitted (clean_signing_input).

    One transaction, the tenancy and then the contract locked, everything
    checked again under the locks: the same token, open, not expired, still the
    current version, still what the tenancy says. Then the Signature row, the
    contract signed with its signed copy, and the tenancy 'signed' if it had
    not got that far. The signed copy is emailed after the commit.
    """
    with transaction.atomic():
        tenancy = _tenancy_with_slots(contract.tenancy_id, lock=True)
        locked = RentalContract.objects.select_for_update().get(pk=contract.pk)
        if locked.sign_token != token:
            # Rotated or withdrawn between opening the page and signing.
            raise SigningError(WITHDRAWN, 410, STATE_CANCELLED)
        state = link_state(locked)
        if state.state != STATE_OPEN:
            raise SigningError(state.message, state.status_code, state.state)
        if contract_is_stale(tenancy, locked):
            # The agreement changed after this version was issued: what the
            # tenant read is no longer what the office agrees to.
            raise SigningError(UPDATED, 409)
        terms = locked.terms
        if terms_sha256(terms) != locked.terms_sha256:
            # The signature would bind to a fingerprint the stored terms no longer have.
            logger.error(
                'Rental contract %s (tenancy %s, version %s): the stored terms no longer match their '
                'SHA-256 %s; refusing to sign',
                locked.pk, locked.tenancy_id, locked.version, locked.terms_sha256,
            )
            raise SigningError(BROKEN, 500)

        now = timezone.now()
        document = contract_html(terms)
        tenant_terms = terms.get('tenant') or {}
        signature = Signature.objects.create(
            kind=Signature.KIND_RENTAL_CONTRACT,
            signed_at=now,
            signer_name=signing.signer_name,
            signer_id_number=signing.signer_id_number,
            signer_phone=(tenant_terms.get('phone') or '')[:20],
            signer_email=(tenant_terms.get('email') or '')[:254],
            business_customer_id=tenancy.tenant_id,
            branch_id=tenancy.branch_id,
            document_title=f'חוזה שכירות — גרסה {locked.version}',
            document_html=document,
            document_sha256=hashlib.sha256(document.encode('utf-8')).hexdigest(),
            consents={'terms': True, 'computerized_documents': True},
            signature_png=signing.png,
            signature_sha256=hashlib.sha256(signing.png).hexdigest(),
            ip_address=client_ip(request),
            user_agent=str((getattr(request, 'META', {}) or {}).get('HTTP_USER_AGENT') or '')[:500],
            source=Signature.SOURCE_SIGNING_LINK,
            refs={
                'contract_id': str(locked.pk),
                'tenancy_id': str(tenancy.pk),
                'version': locked.version,
                'terms_sha256': locked.terms_sha256,
                'pdf_sha256': locked.pdf_sha256,
            },
        )
        signed_pdf = generate_tenancy_contract_pdf(
            terms,
            version=locked.version,
            signed=SignedBy(
                png=signing.png,
                name=signing.signer_name,
                id_number=signing.signer_id_number,
                signed_at=now,
                signature_id=str(signature.pk),
                terms_sha256=locked.terms_sha256,
            ),
        )
        locked.status = RentalContract.STATUS_SIGNED
        locked.signed_at = now
        locked.signature = signature
        locked.signed_pdf = signed_pdf
        locked.signed_pdf_sha256 = sha256_hex(signed_pdf)
        locked.save(update_fields=['status', 'signed_at', 'signature', 'signed_pdf', 'signed_pdf_sha256'])

        if tenancy.status in (Tenancy.STATUS_DRAFT, Tenancy.STATUS_SENT):
            tenancy.status = Tenancy.STATUS_SIGNED
            tenancy.save(update_fields=['status', 'updated_at'])

        contract_id = locked.pk
        transaction.on_commit(lambda: send_signed_contract_email(contract_id))
    logger.info(
        'Rental contract %s (tenancy %s, version %s) signed: signature %s',
        locked.pk, locked.tenancy_id, locked.version, signature.pk,
    )
    return locked


def after_signing(contract, request=None) -> dict:
    """
    What the signing page does once the contract is signed, merged into its answer.

    With rental billing on (apps/rental_billing), a tenancy that still needs a
    card goes on to its card page: its standing order is opened from the
    signing (source 'signing'; the open one is reused when there is one), a
    new card link is issued, and the answer is
    {'next': 'card', 'card_url': '<frontend>/rc/<token>'}. An order that
    already holds a card (active, paused) needs no card step.

    Otherwise — billing off, the rentals business missing, or anything here
    failing — the page is done: {'next': 'done'}.

    The signature is already committed when this runs: sign_contract's
    transaction closed before it returned, and the view calls this after it.
    Nothing here can undo the signing, so a failure is logged and answered
    with 'done', never raised.
    """
    return _next_step(contract, request, _card_link_after_signing)


def signed_next_step(contract, request=None) -> dict:
    """
    The same answer, for a signed contract's page opened again — the GET of /sign/{token}/.

    A tenant who closed the page after signing and came back on the same link
    is taken on to their card page while there is still a card to enter:
    {'next': 'card', 'card_url': ...} for a standing order the signing already
    opened that is still waiting for one, and {'next': 'done'} otherwise —
    billing off, a card on file, no order, or anything here failing.

    It never raises and never signs anything: the signed contract must keep
    opening whatever billing does.
    """
    return _next_step(contract, request, _card_link_for_signed)


def _next_step(contract, request, card_link) -> dict:
    """Where the tenant goes from a signed contract, the one way for both answers. Never raises."""
    from apps.rental_billing.billing import billing_enabled, missing_business_message, rental_business
    from apps.rental_billing.links import public_url

    if not billing_enabled():
        return {'next': 'done'}
    try:
        if rental_business() is None:
            logger.warning(
                'Rental contract %s signed; no card step: %s', contract.pk, missing_business_message(),
            )
            return {'next': 'done'}
        # One transaction (a savepoint when called inside one): whatever the
        # step writes — an order and its link on the signing, a replacement
        # link on a page opened again — lands together or not at all.
        with transaction.atomic():
            link = card_link(contract)
        if link is None:
            return {'next': 'done'}
        return {'next': 'card', 'card_url': public_url(link, public_frontend_url(request))}
    except Exception:
        logger.exception(
            'Rental contract %s signed; its card step could not be opened (the signature stands)', contract.pk,
        )
        return {'next': 'done'}


def _card_link_after_signing(contract):
    """The tenancy's open standing order (opened now if it has none) and a new card link, or None when it needs no card."""
    from apps.rental_billing import links, orders
    from apps.rental_billing.models import TenantStandingOrder

    order = TenantStandingOrder.objects.filter(
        tenancy_id=contract.tenancy_id, status__in=TenantStandingOrder.OPEN_STATUSES,
    ).first()
    if order is None:
        tenancy = Tenancy.objects.get(pk=contract.tenancy_id)
        order = orders.open_standing_order(tenancy, source=TenantStandingOrder.SOURCE_SIGNING)
    if order.status not in links.LINKABLE_STATUSES:
        return None
    return links.rotate_card_link(order)


def _card_link_for_signed(contract):
    """
    The live card link of a signed tenancy's standing order, or None when it needs no card.

    Unlike _card_link_after_signing this opens nothing and rotates nothing, and
    that is what makes it safe on a page the tenant may open again and again:

    * the signing token is the tenant's own key and resolves to one contract,
      and the order is looked up by that contract's tenancy_id alone — the
      tenant's own tenancy, never another;
    * ensure_card_link hands back the link that is already out; a fresh one is
      issued only when there is none left live — none was ever made, or the one
      there was has been used or cancelled. So a URL already sent by WhatsApp is
      never rotated out from under the tenant by a page load, and reloading the
      page twice gives the same address;
    * no standing order is opened here. An order the office ended stays ended,
      and reading a signed contract never puts a tenancy into billing;
    * the order row is locked first and its links second — the order
      rotate_card_link locks them in — so the two can never deadlock;
    * the page this link opens is itself behind RENTAL_BILLING_ENABLED, and
      nothing on this path reaches Tranzila. The GET keeps its own throttle
      (rental_sign_view), so a flood of reloads is refused before it gets here.

    "Still needs a card" is pending_card or failed: the two statuses a card is
    taken for everywhere else (links.LINKABLE_STATUSES, card.CARD_STATUSES).
    An order holding a card the gateway accepted is active (or paused by the
    office); a failed one's stored card is precisely the one that was declined,
    and it only becomes active again once a charge goes through
    (billing.record_result), so it is never a usable card.
    """
    from apps.rental_billing import links
    from apps.rental_billing.models import TenantStandingOrder

    order = (
        TenantStandingOrder.objects.select_for_update()
        .filter(tenancy_id=contract.tenancy_id, status__in=TenantStandingOrder.OPEN_STATUSES)
        .first()
    )
    if order is None or order.status not in links.LINKABLE_STATUSES:
        return None
    return links.ensure_card_link(order)
