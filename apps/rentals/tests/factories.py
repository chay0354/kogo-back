"""Shared setup for the rentals tests: users by role, branches, studios, rentals, merchants."""
from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model

from apps.core.models import Branch, City, Room, UserProfile
from apps.customers.models import BusinessCustomer
from apps.scheduling.models import ScheduleEvent

User = get_user_model()


def make_user(username, role, branches=()):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    if branches:
        profile.assigned_branches.set(branches)
    return User.objects.get(pk=user.pk)


def make_branch(name):
    city, _ = City.objects.get_or_create(name='תל אביב')
    return Branch.objects.create(name=name, city=city)


def make_studio(branch, name='סטודיו 1'):
    return Room.objects.create(branch=branch, name=name)


def make_rental(
    branch,
    *,
    renter_name='שוכר',
    renter_id_number='',
    price='100',
    days=(0,),
    event_type='weekly',
    studio=None,
    is_active=True,
    contract=(date(2026, 9, 1), date(2027, 8, 31)),
    **extra,
):
    """A studio rental saved straight to the table, as the calendar would hold it."""
    return ScheduleEvent.objects.create(
        name=f'שכירות {renter_name}',
        event_date=date(2026, 9, 6),
        start_time=time(10, 0),
        end_time=time(11, 0),
        event_type=event_type,
        branch=branch,
        studio=studio,
        is_studio_rental=True,
        renter_name=renter_name,
        renter_id_number=renter_id_number,
        price_per_session=Decimal(price),
        weekly_repeat_days=list(days),
        contract_start_date=contract[0],
        contract_end_date=contract[1],
        is_active=is_active,
        **extra,
    )


def make_customer(first_name='דנה', last_name='לוי', **fields):
    return BusinessCustomer.objects.create(first_name=first_name, last_name=last_name, **fields)
