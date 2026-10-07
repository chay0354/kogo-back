"""Setting and changing where a business customer is filed.

The location is the business, the category in it and — under the category
סניפים — the branch. A document issued to the customer takes the business and
category the card has at that moment (documents.service._income_tags), and
keeps them.

Three cases, and the office is asked only in the last:

* The card has no location yet (a card imported from the previous software
  comes in clean): the location is set. Nothing is asked.
* The same location again: nothing happens.
* The card has a location and a different one is given: the office must say
  how far the change goes — `future` (the card only; documents already issued
  stay where they were) or `all` (the card and every document issued to the
  customer). Without that word the change is refused with what it would touch,
  so the screen can ask.

`all` rewrites the business, category and branch of documents that were
already issued — never their numbers, amounts, dates or PDFs. It leaves out the
documents another flow files on its own terms: a tenant's rent receipts follow
the rental agreement (RT), and Michal's follow her integration (MK). Every
change is recorded (BusinessCustomerLocationChange), a move of documents with
what each carried before.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q

from apps.customers.location_models import BusinessCustomerLocationChange

SCOPE_FUTURE = BusinessCustomerLocationChange.SCOPE_FUTURE
SCOPE_ALL = BusinessCustomerLocationChange.SCOPE_ALL
SCOPES = (SCOPE_FUTURE, SCOPE_ALL)

BRANCHES_CATEGORY = 'סניפים'


class LocationError(ValueError):
    """The location given cannot be set. The message is the office's, in Hebrew."""


class ScopeNeeded(Exception):
    """The card already has a location: the office must say how far the change goes."""

    def __init__(self, customer, documents: int, previous: dict, new: dict):
        super().__init__('scope needed')
        self.customer = customer
        self.documents = documents
        self.previous = previous
        self.new = new


@dataclass(frozen=True)
class Location:
    business: object = None
    category: object = None
    branch: object = None

    @property
    def ids(self) -> tuple:
        return (
            self.business.pk if self.business else None,
            self.category.pk if self.category else None,
            self.branch.pk if self.branch else None,
        )

    @property
    def label(self) -> str:
        parts = [self.business.name if self.business else '', self.category.name if self.category else '',
                 self.branch.name if self.branch else '']
        return ' · '.join(part for part in parts if part)

    def as_dict(self) -> dict:
        return {
            'business_id': str(self.business.pk) if self.business else None,
            'business_name': self.business.name if self.business else '',
            'business_category_id': str(self.category.pk) if self.category else None,
            'business_category_name': self.category.name if self.category else '',
            'branch_id': str(self.branch.pk) if self.branch else None,
            'branch_name': self.branch.name if self.branch else '',
            'label': self.label,
        }


def location_of(customer) -> Location:
    return Location(customer.business, customer.business_category, customer.branch)


def has_location(customer) -> bool:
    """A card is filed once it names a business or a branch; a clean card names neither."""
    return bool(customer.business_id or customer.branch_id)


def resolve_location(business_id, category_id, branch_id) -> Location:
    """The three ids as a location. Refuses what does not exist or does not belong together."""
    from apps.core.models import Branch, Business, BusinessCategory

    def find(model, pk, what):
        if pk in (None, ''):
            return None
        try:
            return model.objects.get(pk=pk)
        except (model.DoesNotExist, ValueError, TypeError):
            raise LocationError(f'{what} לא נמצא') from None
        except Exception as exc:  # noqa: BLE001 - a malformed UUID raises ValidationError
            raise LocationError(f'{what} לא תקין') from exc

    business = find(Business, business_id, 'העסק')
    category = find(BusinessCategory, category_id, 'הקטגוריה')
    branch = find(Branch, branch_id, 'הסניף')
    if business is None:
        raise LocationError('יש לבחור עסק')
    if category is None:
        raise LocationError('יש לבחור קטגוריה')
    if category.business_id != business.pk:
        raise LocationError('הקטגוריה אינה שייכת לעסק שנבחר')
    if branch is not None and category.name.strip() != BRANCHES_CATEGORY:
        raise LocationError(f'סניף נבחר רק תחת הקטגוריה {BRANCHES_CATEGORY}')
    return Location(business, category, branch)


def movable_documents(customer):
    """
    The customer's documents a change of location may move: the ones issued to
    them by hand. A rent receipt is filed by its rental agreement and one of
    Michal's by her integration, whatever the card says.
    """
    from apps.documents.models import FormalDocument
    from apps.documents.numbering import SERIES_MICHAL, SERIES_RENTAL

    return (
        FormalDocument.objects.filter(business_customer=customer)
        .exclude(Q(document_number__startswith=f'{SERIES_RENTAL}-') | Q(document_number__startswith=f'{SERIES_MICHAL}-'))
    )


def _actor_name(user) -> str:
    if user is None or not getattr(user, 'is_authenticated', False):
        return ''
    return (user.get_full_name() or user.get_username() or '')[:150]


def change_location(customer, location: Location, *, scope: str | None = None, user=None) -> dict:
    """
    File the customer under `location`. Returns what was done:
    {changed, scope, documents_changed, previous, location}.

    Raises ScopeNeeded when the card already has another location and `scope`
    does not say how far the change goes; LocationError for a scope that is
    not one.
    """
    from apps.customers.models import BusinessCustomer
    from apps.documents.models import FormalDocument

    if scope not in (None, '') and scope not in SCOPES:
        raise LocationError('היקף השינוי הוא future או all')

    with transaction.atomic():
        # Two saves of one card wait for each other: the second sees the first's location.
        customer = (
            BusinessCustomer.objects.select_for_update(of=('self',))
            .select_related('business', 'business_category', 'branch').get(pk=customer.pk)
        )
        previous = location_of(customer)
        if previous.ids == location.ids:
            return {'changed': False, 'scope': None, 'documents_changed': 0,
                    'previous': previous.as_dict(), 'location': location.as_dict()}

        first = not has_location(customer)
        if not first and not scope:
            raise ScopeNeeded(customer, movable_documents(customer).count(), previous.as_dict(), location.as_dict())
        applied = BusinessCustomerLocationChange.SCOPE_FIRST if first else scope

        moved = []
        if applied == SCOPE_ALL:
            rows = (
                movable_documents(customer).select_for_update(of=('self',))
                .values_list('pk', 'document_number', 'business_id', 'business_category_id', 'branch_id')
            )
            for pk, number, *before in rows:
                if tuple(before) == location.ids:
                    continue
                moved.append({
                    'id': str(pk),
                    'number': number,
                    'business_id': str(before[0]) if before[0] else None,
                    'business_category_id': str(before[1]) if before[1] else None,
                    'branch_id': str(before[2]) if before[2] else None,
                })
            # update(), not save(): only where each document is filed moves —
            # not its number, amounts, date or PDF, and not its updated_at.
            FormalDocument.objects.filter(pk__in=[row['id'] for row in moved]).update(
                business=location.business, business_category=location.category, branch=location.branch,
            )

        customer.business = location.business
        customer.business_type = location.business.name[:100]
        customer.business_category = location.category
        customer.category = location.category.name[:100]
        customer.branch = location.branch
        customer.save(update_fields=[
            'business', 'business_type', 'business_category', 'category', 'branch', 'updated_at',
        ])

        BusinessCustomerLocationChange.objects.create(
            customer=customer,
            scope=applied,
            previous_business_id=previous.ids[0], previous_category_id=previous.ids[1],
            previous_branch_id=previous.ids[2], previous_label=previous.label[:320],
            new_business_id=location.ids[0], new_category_id=location.ids[1],
            new_branch_id=location.ids[2], new_label=location.label[:320],
            documents=moved, documents_changed=len(moved),
            changed_by=user if _actor_name(user) else None, changed_by_name=_actor_name(user),
        )
    return {'changed': True, 'scope': applied, 'documents_changed': len(moved),
            'previous': previous.as_dict(), 'location': location.as_dict()}
