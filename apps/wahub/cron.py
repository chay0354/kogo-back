"""
The slice of background work one cron call does (every five minutes):

  1. a summary for the contacts marked for one, once their conversation has
     been quiet for five minutes — so a customer typing five short messages is
     summarised once, not five times;
  2. the match against the registrations, for contacts never checked or last
     checked more than twelve hours ago.

A call stops when its time is spent and the next one carries on: every contact
is finished and saved on its own, so a call that is cut loses nothing.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from apps.wahub import analysis, matching
from apps.wahub.models import ANALYSIS_AI, Contact

logger = logging.getLogger(__name__)

BUDGET_SECONDS = 20
# The summaries may call Claude; the matching must still get its turn.
ANALYSIS_SHARE = 0.6
QUIET_BEFORE_ANALYSIS = timedelta(minutes=5)
RECHECK_AFTER = timedelta(hours=12)
BATCH = 50


def _to_analyze(now):
    return Contact.objects.filter(needs_analysis=True).filter(
        Q(last_message_at__lte=now - QUIET_BEFORE_ANALYSIS) | Q(last_message_at__isnull=True)
    )


def _to_match(now):
    return Contact.objects.filter(Q(kogo_checked_at__isnull=True) | Q(kogo_checked_at__lte=now - RECHECK_AFTER))


def tick(*, budget_seconds: float = BUDGET_SECONDS) -> dict:
    started = time.monotonic()

    def left() -> float:
        return budget_seconds - (time.monotonic() - started)

    now = timezone.now()
    counts = {'analyzed': 0, 'analyzed_ai': 0, 'analyzed_rules': 0, 'matched': 0, 'outcomes_changed': 0}

    places = None
    analysis_until = budget_seconds * (1 - ANALYSIS_SHARE)
    for contact in _to_analyze(now).order_by(F('last_message_at').asc(nulls_first=True), 'id')[:BATCH]:
        if left() <= analysis_until:
            break
        if places is None:
            places = analysis.load_places()
        try:
            # Never wait for Claude longer than the time this call still has.
            known = analysis.analyze_contact(contact, places=places, timeout=max(2.0, left() - analysis_until))
        except Exception:
            logger.exception('wahub cron: summary of contact %s failed', contact.pk)
            # Left marked, it would be the first one picked again every five minutes.
            Contact.objects.filter(pk=contact.pk).update(needs_analysis=False)
            continue
        counts['analyzed'] += 1
        counts['analyzed_ai' if known.source == ANALYSIS_AI else 'analyzed_rules'] += 1

    for contact in _to_match(now).order_by(F('kogo_checked_at').asc(nulls_first=True), 'id')[:BATCH * 4]:
        if left() <= 0:
            break
        try:
            changed = matching.recheck_contact(contact)
        except Exception:
            logger.exception('wahub cron: matching of contact %s failed', contact.pk)
            Contact.objects.filter(pk=contact.pk).update(kogo_checked_at=timezone.now())
            continue
        counts['matched'] += 1
        counts['outcomes_changed'] += int(changed)

    now = timezone.now()
    counts['pending_analysis'] = _to_analyze(now).count()
    counts['pending_matching'] = _to_match(now).count()
    counts['seconds'] = round(time.monotonic() - started, 2)
    return counts
