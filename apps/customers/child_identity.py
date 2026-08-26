"""One real child should appear once in CRM lists and widget signup."""
from django.db.models import Case, Count, F, IntegerField, OuterRef, Q, Subquery, Value, When

from apps.customers.models import Child

# Lower rank wins when the same child was created twice (widget retry, etc.).
CHILD_STATUS_RANK = {
    'active': 0,
    'payment_problem': 1,
    'trial_signed': 2,
    'trial_completed': 3,
    'not_paid': 4,
    'pending': 5,
    'inactive': 6,
    'ghost': 7,
}


def child_status_rank_annotation():
    whens = [When(status=status, then=Value(rank)) for status, rank in CHILD_STATUS_RANK.items()]
    return Case(*whens, default=Value(99), output_field=IntegerField())


def normalize_id_number(value):
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def _winner_id_subquery(match_q):
    return (
        Child.objects.filter(match_q)
        .annotate(
            _status_rank=child_status_rank_annotation(),
            _enrollment_count=Count('lesson_enrollments'),
        )
        .order_by('_status_rank', '-_enrollment_count', '-created_at')
        .values('id')[:1]
    )


def exclude_weaker_duplicate_children(queryset):
    """
    Keep one child per name on a family.

    Widget retries often leave a leftover pending card beside the real
    active/trial card. The kept row is the strongest status (active over
    pending, and so on), then the one with more enrollments, then newest.
    """
    name_winner = _winner_id_subquery(Q(
        family_id=OuterRef('family_id'),
        first_name__iexact=OuterRef('first_name'),
        last_name__iexact=OuterRef('last_name'),
    ))
    return queryset.annotate(
        _name_winner_id=Subquery(name_winner),
    ).filter(id=F('_name_winner_id'))



def find_existing_child_on_family(family, *, first_name, last_name, id_number=''):
    """Reuse a child card on this family instead of creating another pending one."""
    children = list(family.children.exclude(status='ghost'))
    submitted_id = normalize_id_number(id_number)
    if submitted_id:
        id_matches = [
            child for child in children
            if normalize_id_number(child.id_number) == submitted_id
        ]
        if id_matches:
            id_matches.sort(key=lambda child: (
                CHILD_STATUS_RANK.get(child.status, 99),
                -child.created_at.timestamp() if child.created_at else 0,
            ))
            return id_matches[0]

    first = (first_name or '').strip().lower()
    last = (last_name or '').strip().lower()
    name_matches = [
        child for child in children
        if child.first_name.strip().lower() == first
        and child.last_name.strip().lower() == last
    ]
    if not name_matches:
        return None
    name_matches.sort(key=lambda child: (
        CHILD_STATUS_RANK.get(child.status, 99),
        -child.created_at.timestamp() if child.created_at else 0,
    ))
    return name_matches[0]
