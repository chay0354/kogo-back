"""
What a registration would cost — worked out from facts, for someone who has no
card in Kogo to run it on.

The registration form prices a signup by running it for a real child
(`PaymentService.initiate_subscription_payment`; the form's own quote rehearses
that inside a transaction it rolls back). A parent asking on WhatsApp "how much
if I sign up now" has no child here, so nothing can be run for them. This puts
the same figures together from what is known — is a sibling registered, is this
the child's second class, was a trial paid for — out of the very pieces the
registration is made of:

    the monthly price      payment_service.lesson_price_for_index / bundle_monthly_price
    the discounts          DiscountService.evaluate_discounts_for_facts (one combination for both)
    the first month        payment_service.first_month_charge
    the yearly fee         payment_service.registration_fee_amount
    a paid trial           trial_credit.capped_credit — or quote_trial_credit, for a child who is here

Nothing is written, nothing is locked, nobody is charged.
`apps/core/tests/test_registration_quote.py` holds this against the
registration itself, figure by figure.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable, Optional

from apps.core.payment_service import (
    bundle_monthly_price,
    first_month_charge,
    lesson_price_for_index,
    registration_fee_amount,
)
from apps.customers.discount_service import DiscountService
from apps.customers.trial_credit import capped_credit, money

ZERO = Decimal('0.00')


@dataclass(frozen=True)
class Facts:
    """What the registration would have looked up about the child, told instead."""
    # Another child of the family is on a team (the "ילד שני" discount).
    sibling_registered: bool = False
    # This child already pays for another class: the second-class price or
    # discount applies, and the yearly fee — once per child — is behind them.
    another_class: bool = False
    # What was paid for a trial lesson in this branch that can still be
    # credited (inside the window, not used yet). None: no such trial.
    paid_trial: Optional[Decimal] = None


def quote(
    lesson,
    *,
    today: date,
    facts: Facts = Facts(),
    bundle=None,
    price_option=None,
    credit_for: Optional[Callable[[Decimal], Decimal]] = None,
) -> dict:
    """
    The figures of a registration to `lesson` made on `today`.

    `bundle` prices a combined track (the lesson is then the member whose
    weekday the first month is counted by, as the form does), `price_option`
    an extra catalogue price of the lesson. `credit_for(first_charge)` answers
    with a real child's trial credit; without it the credit is `facts.paid_trial`
    capped the same way.

    Raises ValueError when the lesson has no price, as the registration does.
    """
    course = lesson.course
    course_index = 2 if facts.another_class else 1
    if price_option is not None:
        base_price, used_tier = price_option.monthly_price, True
    elif bundle is not None:
        base_price, used_tier = bundle_monthly_price(bundle), True
    else:
        base_price, used_tier = lesson_price_for_index(lesson, course_index)
    if not base_price:
        raise ValueError('Lesson/Course price not configured')

    # A per-lesson tier or a track price is already the second-class price:
    # the generic "additional lesson" discount is not taken on top of it.
    discounts = DiscountService().evaluate_discounts_for_facts(
        payment_date=today,
        base_price=base_price,
        sibling_registered=facts.sibling_registered,
        another_class=facts.another_class and not used_tier,
    )
    monthly = discounts.final_price
    first_month = first_month_charge(full_monthly_amount=monthly, lesson=lesson, today=today)

    fee = ZERO if facts.another_class else registration_fee_amount(course)
    before_credit = first_month['amount'] + fee
    if credit_for is not None:
        credit = money(credit_for(before_credit))
    elif facts.paid_trial:
        credit = capped_credit(facts.paid_trial, before_credit)
    else:
        credit = ZERO

    return {
        'course_index': course_index,
        'base_amount': discounts.base_price,
        'discount_amount': discounts.total_discount_amount,
        'discounts_applied': [
            {'name': row.name, 'type': row.discount_type, 'value': row.value, 'reason': row.reason}
            for row in discounts.applicable_discounts
        ],
        'monthly_amount': monthly,
        'prorated_amount': first_month['amount'],
        'prorate_factor': first_month['prorate_factor'],
        'prorate_lessons_remaining': first_month['lessons_remaining'],
        'total_lessons_this_month': first_month['total_lessons'],
        'deferred_charge_date': first_month['deferred_charge_date'],
        'next_billing_date': first_month['next_billing_date'],
        'registration_fee': fee,
        'trial_credit_amount': credit,
        'final_amount': before_credit - credit,
    }
