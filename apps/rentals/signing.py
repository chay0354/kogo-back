"""Signing a rental contract through a short link: the office sends it, the tenant reads it and signs.

    issue_signing_link(contract)         a new link for the tenancy's current open contract → sent
    cancel_signing_link(contract)        withdraw the link → draft
    resolve_sign_token(token)            the contract a link points at, or None
    link_state(contract)                 what the link shows now: open, signed, expired or cancelled
    public_payload(contract, state)      the signing page, from the contract's frozen terms
    mark_viewed(contract)                the first open of a sent link
    clean_signing_input(data)            the tenant's name, ID and signature, checked
    sign_contract(contract, token, ...)  record the signature and the signed copy, in one transaction
    after_signing(contract)              what the page does next

The link is <frontend>/s/<sign_token>, built the way card links are built, and
lives 14 days from sign_token_created_at. The page shows the contract from its
frozen terms only — never from the live tenancy — because that is what the
tenant signs, and the signature is bound to their fingerprint (terms_sha256).

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

# The same lifetime as a card link: long enough for a tenant to get round to
# it, short enough that an old message does not open a contract for ever.
LINK_LIFETIME = timedelta(days=14)
# After signing, the link keeps showing the signed contract for a while, then
# closes: the page carries the tenant's ID, phone and email, and a forwarded
# message should not open them for ever. The office keeps the signed copy.
SIGNED_LINK_LIFETIME = timedelta(days=30)
# A token is short_token(): 10 letters and digits. Anything else is not looked up at all.
_TOKEN_PATTERN = re.compile(r'[A-Za-z0-9]{6,32}')

STATE_OPEN = 'open'
STATE_SIGNED = 'signed'
STATE_EXPIRED = 'expired'
STATE_CANCELLED = 'cancelled'

# What the tenant reads.
NOT_FOUND = 'הקישור לא נמצא. בדקו שהועתק במלואו, או בקשו מהמשרד קישור חדש'
EXPIRED = 'פג תוקף הקישור — בקשו מהמשרד קישור חדש'
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


def link_expires_at(contract):
    if contract.sign_token_created_at is None:
        return None
    return contract.sign_token_created_at + LINK_LIFETIME


def link_is_live(contract, now=None) -> bool:
    """Out with the tenant and not expired. Whether the agreement changed since is not checked here."""
    expires_at = link_expires_at(contract)
    return (
        contract.status in RentalContract.LINKED_STATUSES
        and bool(contract.sign_token)
        and expires_at is not None
        and (now or timezone.now()) < expires_at
    )


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
    expired   — 14 days have passed since the link was made (410).
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
    if contract.status not in RentalContract.LINKED_STATUSES or not contract.sign_token:
        return LinkState(STATE_CANCELLED, WITHDRAWN, 410)
    if not link_is_live(contract, now):
        return LinkState(STATE_EXPIRED, EXPIRED, 410)
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
        'expires_at': _iso(link_expires_at(contract)) if state.state == STATE_OPEN else None,
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


def after_signing(contract) -> dict:
    """
    What the signing page does once the contract is signed, merged into its answer.

    Phase 4 (the tenant's card and standing order) continues from here: it will
    answer {'next': 'card', ...} for a tenancy that still needs one. Until
    then the page is done.
    """
    return {'next': 'done'}
