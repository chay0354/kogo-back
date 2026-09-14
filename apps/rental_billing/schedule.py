"""Billing dates for a tenant's standing order. Pure: no database, no clock.

A standing order is charged once a month, on its billing day (1–28, so every
month has it). A charge belongs to a billing month, its period, written as the
first of that month; the charge made on 10 October is October's.
"""
from __future__ import annotations

from datetime import date


def first_of_month(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, months: int) -> date:
    """The first of the month `months` after `day`'s month."""
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def billing_date(month: date, billing_day: int) -> date:
    """The billing day in `month`'s month."""
    return date(month.year, month.month, billing_day)


def billing_date_after(period: date, billing_day: int) -> date:
    """The billing day of the month after `period`: when the next month is charged."""
    return billing_date(add_months(period, 1), billing_day)


def first_billing_on_or_after(day: date, billing_day: int) -> date:
    """The first billing day that is `day` or later."""
    candidate = billing_date(day, billing_day)
    return candidate if candidate >= day else billing_date(add_months(day, 1), billing_day)


def month_label(period: date) -> str:
    """'אוקטובר 2026'."""
    from apps.documents.period_report import HEBREW_MONTHS

    return f'{HEBREW_MONTHS[period.month - 1]} {period.year}'
