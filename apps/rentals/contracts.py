"""Rental contracts — a tenancy's contract, issued as a numbered document that never changes.

    build_terms(tenancy)               what a contract for the tenancy says, as of now
    issue_contract(tenancy, user)      freeze those terms and their PDF as the next version
    void_contract(contract, reason)    take a contract that was not signed out of play
    current_contract(tenancy)          the newest contract that is not void
    contract_is_stale(tenancy, c)      the tenancy no longer says what the contract says

The tenancy is where the agreement is edited. A contract copies what the
tenancy says at the moment it is issued (RentalContract.terms), together with
the PDF drawn from that copy, and neither changes afterwards. Editing the
tenancy leaves every issued contract as it was, and marks the current one
stale until the next version is issued.

Issuing is one transaction under a lock on the tenancy row: two people issuing
at once get versions N and N+1, never two Ns, and the earlier draft, sent or
viewed contracts are voided by the same commit that creates the new one.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Max, Prefetch, prefetch_related_objects
from django.utils import timezone

from apps.rentals.models import RentalContract, Tenancy
from apps.scheduling.models import ScheduleEvent
from apps.scheduling.rental_agreement import terms as contract_terms
from apps.scheduling.rental_agreement.generator import generate_tenancy_contract_pdf

VOID_REASON_MAX_LENGTH = 500

# Where the tenancies list keeps each tenancy's contracts that are not void, newest first.
LIVE_CONTRACTS_ATTR = 'live_contracts'

# A contract's large columns: the two PDFs and the terms. Lists and status
# changes leave them in the database; only a download or a signing reads one.
HEAVY_COLUMNS = ('pdf', 'terms', 'signed_pdf')


class ContractError(ValueError):
    """A contract that cannot be issued or voided. The message is shown to the office as is."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _slot_order(item) -> tuple:
    slot, rows = item
    return (min(contract_terms.row_order(row) for row in rows), contract_terms.clean_text(slot.name))


def build_terms(tenancy) -> dict:
    """
    Everything a contract for this tenancy says, as of now (the keys are in terms.py).

    The amount is the tenancy's agreed monthly_amount, never recomputed from the
    slots: the office may agree on a sum other than rate × 4 per weekday, and a
    contract states what was agreed. Every active slot is listed; an inactive
    one rents nothing any more.

    The same tenancy always gives the same terms, whatever order the database
    returns its slots in, so a contract's terms_sha256 can be compared with the
    tenancy's at any time (contract_is_stale). Reads tenancy.slots.all(), so a
    prefetch of the slots (with their studios) is used when there is one.
    """
    slots = [slot for slot in tenancy.slots.all() if slot.is_active]
    per_slot = sorted(((slot, contract_terms.slot_rows(slot)) for slot in slots), key=_slot_order)
    names = []
    for slot, _rows in per_slot:
        name = contract_terms.clean_text(slot.name)
        if name and name not in names:
            names.append(name)
    tenant = tenancy.tenant
    return {
        'template_version': contract_terms.TEMPLATE_VERSION,
        'studio': contract_terms.studio_details(),
        'tenant': {
            'name': contract_terms.clean_text(tenant.full_name),
            'company_number': contract_terms.clean_text(tenant.company_number),
            'id_number': contract_terms.clean_text(tenant.id_number),
            'phone': contract_terms.clean_text(tenant.phone),
            'email': contract_terms.clean_text(tenant.email),
            'address': contract_terms.clean_text(tenant.address),
        },
        'branch': {'name': contract_terms.clean_text(tenancy.branch.name)} if tenancy.branch_id else None,
        'activity': ', '.join(names),
        'rows': contract_terms.sort_rows(row for _slot, rows in per_slot for row in rows),
        'period': contract_terms.PERIOD_MONTHLY,
        **contract_terms.amounts(tenancy.monthly_amount),
        'billing_day': tenancy.billing_day,
        'start_date': contract_terms.iso_date(tenancy.start_date),
        'end_date': contract_terms.iso_date(tenancy.end_date),
    }


def _refuse_unless_issuable(tenancy) -> None:
    """Raise a ContractError for the first reason no contract can be issued for this tenancy now."""
    if tenancy.contracts.filter(status=RentalContract.STATUS_SIGNED).exists():
        raise ContractError('יש כבר חוזה חתום להסכם הזה')
    if not any(slot.is_active for slot in tenancy.slots.all()):
        raise ContractError('אין בהסכם שכירויות פעילות. יש לשייך לפחות שכירות אחת לפני הפקת חוזה')
    if not tenancy.start_date or not tenancy.end_date:
        raise ContractError('יש להזין להסכם תאריך התחלה ותאריך סיום לפני הפקת חוזה')
    if not tenancy.monthly_amount:
        raise ContractError('הסכום החודשי בהסכם הוא 0. יש להזין את הסכום המוסכם לפני הפקת חוזה')


def issue_contract(tenancy, user) -> RentalContract:
    """
    Issue the tenancy's next contract: freeze its terms, draw its PDF, fingerprint both.

    Refused with a ContractError when the tenancy already has a signed
    contract, has no active slot, lacks a start or an end date, or has an
    amount of 0. The earlier draft, sent and viewed contracts are voided as
    replaced by this version; void and signed ones stay as they are.
    """
    with transaction.atomic():
        # Locked alone, without joins: Postgres refuses FOR UPDATE on the
        # nullable side of an outer join, and branch is nullable.
        locked = Tenancy.objects.select_for_update().get(pk=tenancy.pk)
        prefetch_related_objects(
            [locked], Prefetch('slots', queryset=ScheduleEvent.objects.select_related('studio')),
        )
        _refuse_unless_issuable(locked)
        terms = build_terms(locked)
        version = (locked.contracts.aggregate(latest=Max('version'))['latest'] or 0) + 1
        pdf = generate_tenancy_contract_pdf(terms, version=version)
        locked.contracts.filter(status__in=RentalContract.OPEN_STATUSES).update(
            status=RentalContract.STATUS_VOID,
            voided_at=timezone.now(),
            void_reason=f'הוחלף בגרסה {version}',
        )
        return RentalContract.objects.create(
            tenancy=locked,
            version=version,
            terms=terms,
            pdf=pdf,
            created_by=user if getattr(user, 'is_authenticated', False) else None,
        )


def void_contract(contract, reason='') -> RentalContract:
    """
    Take a draft, sent or viewed contract out of play, with the office's reason.

    A signed contract is never voided: it is the record of what was agreed. A
    void one stays as it is. The row is locked first, so a void cannot cross a
    signature (phase 3) landing at the same moment.
    """
    if reason is None:
        reason = ''
    if not isinstance(reason, str):
        raise ContractError('סיבת הביטול חייבת להיות טקסט')
    reason = reason.strip()
    if len(reason) > VOID_REASON_MAX_LENGTH:
        raise ContractError(f'סיבת הביטול ארוכה מדי (עד {VOID_REASON_MAX_LENGTH} תווים)')
    with transaction.atomic():
        locked = RentalContract.objects.select_for_update().defer(*HEAVY_COLUMNS).get(pk=contract.pk)
        if locked.status == RentalContract.STATUS_SIGNED:
            raise ContractError('אי אפשר לבטל חוזה חתום')
        if locked.status == RentalContract.STATUS_VOID:
            raise ContractError('החוזה כבר בוטל')
        locked.status = RentalContract.STATUS_VOID
        locked.voided_at = timezone.now()
        locked.void_reason = reason
        locked.save(update_fields=['status', 'voided_at', 'void_reason'])
    return locked


def live_contracts_prefetch() -> Prefetch:
    """Each tenancy's contracts that are not void, newest first, without the heavy columns."""
    return Prefetch(
        'contracts',
        queryset=RentalContract.objects.exclude(status=RentalContract.STATUS_VOID)
        # The signer's name shows on the tenants row; the image and the text as
        # signed stay in the database until someone opens the signature.
        .select_related('signature')
        .defer(*HEAVY_COLUMNS, 'signature__signature_png', 'signature__document_html')
        .order_by('-version'),
        to_attr=LIVE_CONTRACTS_ATTR,
    )


def current_contract(tenancy):
    """The tenancy's newest contract that is not void, or None. Uses live_contracts_prefetch when it ran."""
    live = getattr(tenancy, LIVE_CONTRACTS_ATTR, None)
    if live is not None:
        return live[0] if live else None
    return (
        tenancy.contracts.exclude(status=RentalContract.STATUS_VOID)
        .defer(*HEAVY_COLUMNS)
        .order_by('-version')
        .first()
    )


def contract_is_stale(tenancy, contract) -> bool:
    """The agreement changed after the contract was issued: the tenancy's terms now fingerprint differently."""
    return contract_terms.terms_sha256(build_terms(tenancy)) != contract.terms_sha256
