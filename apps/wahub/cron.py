"""
The slice of background work one cron call does (every five minutes):

  1. a summary for the contacts marked for one, once their conversation has
     been quiet for five minutes — so a customer typing five short messages is
     summarised once, not five times;
  1b. the same summary, a few contacts a call, for the conversations of the last
     months that it has never read as a lead (`lead_ai` still empty): the
     one-time pass of leads version 2, spread over the cron instead of run at
     the deploy (docs/WAHUB-CONTRACT-LEADS-V2.md, ו). By the keyword rules
     unless WAHUB_LEAD_BACKFILL_AI says the model may be asked — a call to
     Claude for every old conversation is money, and that is the owner's to decide;
  1c. the walk-ins: a child who came to a lesson without being registered, whose
     phone the instructor wrote down, is made a contact matched against the
     registrations — opened when the phone never wrote to us — and gets its lead
     stage at once (apps/wahub/walk_ins.py). Nothing is sent from here: the bot's
     first message to them is a follow-up like any other (2c);
  2. the match against the registrations, for contacts never checked or last
     checked more than twelve hours ago;
  2a. the waiting lists: every row is held against the registration form's own
     count of its lesson's seats; a seat that came free moves the lead to the
     office's list (apps/wahub/waiting_list.py). Nothing is sent to anybody;
  2b. the lead stage of every contact something happened to, or whose time has
     come — rules over the row, no model (apps/wahub/lead_stage.py);
  2c. the bot's own follow-ups to leads whose moment has come
     (apps/wahub/lead_followups.py) — the one step here that may send a message,
     and only where sending.sending_enabled says one may leave for that contact;
     everywhere else the follow-up stays "due" and nothing leaves;
  3. the shadow bot's answer to every customer message it has not answered yet,
     twenty seconds after the last one of a burst (apps/wahub/shadow.py);
  4. the reviewer's sweep over the last day's conversations (apps/wahub/reviewer.py);
  4c. "מה הבוט לא ידע": the shadow answers in which the new bot had no answer
     are filed under topics for the owner to teach (apps/wahub/unknowns.py) —
     one question to Claude a day, the quick model, and only with time left;
     with no key, by their words. It changes nothing the bot knows;
  4d. "אין כרגע מה להציע": once a day every such lead is held against the
     classes open today, by the bot's own search (it only reads); one that a
     class with a free seat now fits goes to the office (apps/wahub/lead_openings.py).
     Only with time left, and never longer than its own short slice;
  5. a look at the live test: a message of a live-test contact that nobody
     answered is marked on the contact (apps/wahub/live_test.py);
  5b. the safety net of the live answers: a message of a contact the new bot
     answers that is a few minutes old and that no call has answered is
     answered now, by the very call the door makes (live_test.answer_late) —
     the one step besides 2c that may lead to a message, and under the same
     claim as every live answer, so nobody is answered twice;
  6. the holidays boards: which of their automations ManyChat has, by name —
     asked once in ten minutes and kept, because the bot's tool only reads what
     is kept (apps/wahub/holiday_flows.py). A read of ManyChat's list; nothing is sent;
  6b. the follow-ups' templates: the same for the automations the bot's follow-ups to
     leads leave through (apps/wahub/lead_templates.py) — so that the leads list, which
     never asks ManyChat itself, shows a template within ten minutes of its being made
     there. A read of ManyChat's list; nothing is sent.

Nothing in 1b, 1c, 2a, 2b, 3, 4, 4d or 5 sends a message; only 4 may change the knowledge, under the owner's standing approval (reviewer.py): the stage is
a few fields on the contact, the shadow keeps proposals, the reviewer keeps
proposals-to-approve, the look only marks.

A call stops when its time is spent and the next one carries on: every contact
is finished and saved on its own, so a call that is cut loses nothing.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from apps.wahub import (
    analysis, holiday_flows, lead_followups, lead_openings, lead_stage, lead_templates, live_test, matching, reviewer, shadow,
    unknowns, waiting_list, walk_ins,
)
from apps.wahub.models import ANALYSIS_AI, CUSTOMER_OUTCOMES, SHADOW_MODEL_STUB, Contact

logger = logging.getLogger(__name__)

BUDGET_SECONDS = 20
# The summaries may call Claude; the matching must still get its turn.
ANALYSIS_SHARE = 0.6
# After the summary and the matching: the shadow bot, then the reviewer's sweep.
# Each starts only when this much of the budget is still left, and the matching
# stops early to leave the shadow its share.
MATCHING_FLOOR = 0.35
SHADOW_FLOOR = 0.12
# The lead stage comes right after the matching, whose answers it reads. It is
# rules over rows already in hand, so a short slice is plenty — and the matching
# always leaves it one, also on a call with no shadow reply waiting. When one is
# waiting, the stages take only what is left above the time a shadow answer needs.
LEAD_STAGE_FLOOR = 0.1
SHADOW_ANSWER_SECONDS = 3.0
SHADOW_BATCH = 10
# The one-time pass of leads version 2 over the older conversations: this many a call, and only
# conversations whose customer wrote within this long. A registered customer is no lead, and is
# left out. WAHUB_LEAD_BACKFILL_PER_TICK=0 stops it; WAHUB_LEAD_BACKFILL_AI=1 lets it ask Claude
# (off unless set: the rules cost nothing, and a conversation is read by Claude anyway the next
# time its customer writes).
LEAD_BACKFILL_PER_TICK = 3
LEAD_BACKFILL_WITHIN = timedelta(days=90)
# Conversations read again once their past was copied from ManyChat — after everything else the summary has to read.
HISTORY_READ_PER_TICK = 3
QUIET_BEFORE_ANALYSIS = timedelta(minutes=5)
# ManyChat is asked for its automations only with this much of the call still left (the asking waits up to eight seconds).
HOLIDAY_FLOWS_FLOOR = 9
RECHECK_AFTER = timedelta(hours=12)
BATCH = 50


def _to_analyze(now):
    return Contact.objects.filter(needs_analysis=True).filter(
        Q(last_message_at__lte=now - QUIET_BEFORE_ANALYSIS) | Q(last_message_at__isnull=True)
    )


def _to_understand(now):
    """Conversations the summary has never read as a lead: not waiting for a summary anyway, recent, not a customer's."""
    return (
        Contact.objects.filter(lead_ai={}, needs_analysis=False, last_inbound_at__gte=now - LEAD_BACKFILL_WITHIN)
        .exclude(kogo_outcome__in=CUSTOMER_OUTCOMES)
    )


def _backfill_per_tick() -> int:
    try:
        return max(0, int(getattr(settings, 'WAHUB_LEAD_BACKFILL_PER_TICK', LEAD_BACKFILL_PER_TICK)))
    except (TypeError, ValueError):
        return LEAD_BACKFILL_PER_TICK


def _history_to_read(now):
    """
    Conversations whose past was copied from ManyChat (history.py) and not read since: recent, matched with Kogo
    first, not a customer's — read again by the summary with the old bot's and the office's lines in them.
    """
    return (
        Contact.objects.filter(
            history_copied_at__isnull=False, history_read_at__isnull=True, is_demo=False,
            kogo_checked_at__isnull=False, last_inbound_at__gte=now - LEAD_BACKFILL_WITHIN,
        )
        .exclude(kogo_outcome__in=CUSTOMER_OUTCOMES)
    )


def _history_per_tick() -> int:
    try:
        return max(0, int(getattr(settings, 'WAHUB_HISTORY_READ_PER_TICK', HISTORY_READ_PER_TICK)))
    except (TypeError, ValueError):
        return HISTORY_READ_PER_TICK


def _to_match(now):
    return Contact.objects.filter(Q(kogo_checked_at__isnull=True) | Q(kogo_checked_at__lte=now - RECHECK_AFTER))


def tick(*, budget_seconds: float = BUDGET_SECONDS, reply_url: str = '') -> dict:
    started = time.monotonic()

    def left() -> float:
        return budget_seconds - (time.monotonic() - started)

    now = timezone.now()
    counts = {
        'analyzed': 0, 'analyzed_ai': 0, 'analyzed_rules': 0, 'matched': 0, 'outcomes_changed': 0,
        'shadow_proposed': 0, 'shadow_ai': 0, 'shadow_stub': 0, 'review_proposals': 0,
        'lead_checked': 0, 'lead_changed': 0, 'lead_understood': 0, 'history_read': 0,
        'followups_sent': 0, 'followups_due': 0, 'followups_skipped': 0, 'followups_failed': 0,
        'waiting_freed': 0, 'waiting_closed': 0, 'waiting_checked': 0,
        'unknowns_sorted': 0, 'unknown_topics_opened': 0, 'openings_checked': 0, 'openings_found': 0,
        'walk_ins_seen': 0, 'walk_in_contacts_opened': 0, 'walk_in_contacts_matched': 0,
    }

    places = course_types = external_places = None
    analysis_until = budget_seconds * (1 - ANALYSIS_SHARE)
    for contact in _to_analyze(now).order_by(F('last_message_at').asc(nulls_first=True), 'id')[:BATCH]:
        if left() <= analysis_until:
            break
        if places is None:
            # Read once for the whole call: the branches, the external ones, the kinds of class.
            places, course_types = analysis.load_places(), analysis.load_course_types()
            external_places = analysis.load_external_places()
        try:
            # Never wait for Claude longer than the time this call still has.
            known = analysis.analyze_contact(
                contact, places=places, course_types=course_types, external_places=external_places,
                timeout=max(2.0, left() - analysis_until),
            )
        except Exception:
            logger.exception('wahub cron: summary of contact %s failed', contact.pk)
            # Left marked, it would be the first one picked again every five minutes.
            Contact.objects.filter(pk=contact.pk).update(needs_analysis=False)
            continue
        counts['analyzed'] += 1
        counts['analyzed_ai' if known.source == ANALYSIS_AI else 'analyzed_rules'] += 1

    # 1b. the older conversations, read as leads a few at a time — newest first
    for contact in _to_understand(now).order_by('-last_inbound_at', 'id')[:_backfill_per_tick()]:
        if left() <= analysis_until:
            break
        if places is None:
            places, course_types = analysis.load_places(), analysis.load_course_types()
            external_places = analysis.load_external_places()
        try:
            analysis.analyze_contact(
                contact, places=places, course_types=course_types, external_places=external_places,
                timeout=max(2.0, left() - analysis_until),
                use_ai=bool(getattr(settings, 'WAHUB_LEAD_BACKFILL_AI', False)),
            )
        except Exception:
            logger.exception('wahub cron: contact %s was not read as a lead', contact.pk)
            # Left empty, it would be picked again on every call: marked as read, with nothing found.
            Contact.objects.filter(pk=contact.pk, lead_ai={}).update(lead_ai={'v': 0})
            continue
        counts['lead_understood'] += 1

    # 1c. the walk-ins with a phone — before the matching and the stages, which then read them like any lead
    if left() > 0:
        try:
            counts.update(walk_ins.find(now=now, deadline=time.monotonic() + min(walk_ins.CHECK_SECONDS, left())))
        except Exception:
            logger.exception('wahub cron: the walk-ins were not brought in')

    # 1d. a conversation whose past was copied from ManyChat, read again with it (history.py) — the latest first
    for contact in _history_to_read(now).order_by('-last_inbound_at', 'id')[:_history_per_tick()]:
        if left() <= analysis_until:
            break
        if places is None:
            places, course_types = analysis.load_places(), analysis.load_course_types()
            external_places = analysis.load_external_places()
        try:
            analysis.analyze_contact(
                contact, places=places, course_types=course_types, external_places=external_places,
                timeout=max(2.0, left() - analysis_until),
            )
        except Exception:
            logger.exception('wahub cron: contact %s was not read again with its past', contact.pk)
        # Read, or failed: either way not picked again on every call.
        Contact.objects.filter(pk=contact.pk).update(history_read_at=timezone.now())
        counts['history_read'] += 1

    shadow_waiting = shadow.pending(now).exists()
    matching_until = budget_seconds * (MATCHING_FLOOR if shadow_waiting else LEAD_STAGE_FLOOR)
    for contact in _to_match(now).order_by(F('kogo_checked_at').asc(nulls_first=True), 'id')[:BATCH * 4]:
        if left() <= matching_until:
            break
        try:
            changed = matching.recheck_contact(contact)
        except Exception:
            logger.exception('wahub cron: matching of contact %s failed', contact.pk)
            Contact.objects.filter(pk=contact.pk).update(kogo_checked_at=timezone.now())
            continue
        counts['matched'] += 1
        counts['outcomes_changed'] += int(changed)

    shadow_until = budget_seconds * SHADOW_FLOOR
    # 2a. the waiting lists — before the stages, which read what this finds (a seat that came free)
    waiting_seconds = min(
        waiting_list.CHECK_SECONDS, left() - (shadow_until + SHADOW_ANSWER_SECONDS if shadow_waiting else 0),
    )
    if waiting_seconds > 0:
        try:
            counts.update(waiting_list.check(budget_seconds=waiting_seconds))
        except Exception:
            logger.exception('wahub cron: the waiting lists were not looked at')

    # 2b. the lead stage — after the summary and the matching, so it reads today's facts
    lead_seconds = min(
        lead_stage.SCAN_SECONDS, left() - (shadow_until + SHADOW_ANSWER_SECONDS if shadow_waiting else 0),
    )
    if lead_seconds > 0:
        try:
            counts.update(lead_stage.scan(budget_seconds=lead_seconds))
        except Exception:
            logger.exception('wahub cron: the lead stages were not refreshed')

    # 2c. the bot's own follow-ups to leads — after the stages, which say whether each is still wanted
    follow_seconds = min(
        lead_followups.RUN_SECONDS, left() - (shadow_until + SHADOW_ANSWER_SECONDS if shadow_waiting else 0),
    )
    if follow_seconds > 0:
        try:
            counts.update(lead_followups.run(budget_seconds=follow_seconds))
        except Exception:
            logger.exception('wahub cron: the follow-ups to leads were not run')

    # 3. the shadow bot — one answer per quiet burst, never sent
    for contact in shadow.pending(now).order_by(F('last_inbound_at').asc(nulls_first=True), 'id')[:SHADOW_BATCH]:
        if left() <= shadow_until:
            break
        try:
            reply = shadow.propose(contact, now=timezone.now(), deadline=time.monotonic() + max(3.0, left() - shadow_until))
        except Exception:
            logger.exception('wahub cron: shadow reply for contact %s failed', contact.pk)
            Contact.objects.filter(pk=contact.pk).update(needs_shadow=False)
            continue
        if reply is not None:
            counts['shadow_proposed'] += 1
            counts['shadow_stub' if reply.model == SHADOW_MODEL_STUB else 'shadow_ai'] += 1

    # 4. the reviewer's sweep — proposals that wait for a click
    if left() > 0:
        try:
            review = reviewer.scan(now=timezone.now(), deadline=time.monotonic() + left())
            counts['review_proposals'] = review['proposals']
        except Exception:
            logger.exception('wahub cron: the reviewer sweep failed')

    # 4b. recordings that were not transcribed are tried again (nothing is answered from here)
    if left() > 10:
        try:
            counts['recordings_heard_again'] = shadow.hear_again(now=timezone.now(), deadline=time.monotonic() + left())
        except Exception:
            logger.exception('wahub cron: the second try at the recordings failed')

    # 4c. "מה הבוט לא ידע": the answers in which the new bot did not know are filed under topics — Claude is
    # asked once a day, and only with time to spare; with no key they are gathered by their words (unknowns.py)
    try:
        counts.update(unknowns.sort_new(now=timezone.now(), deadline=time.monotonic() + left()))
    except Exception:
        logger.exception('wahub cron: what the bot did not know was not filed under topics')

    # 4d. the leads with nothing to offer, once a day each: has a class that fits them opened? (lead_openings.py — a read
    #     of the classes; a lead it fits goes to the office, and nothing is sent to anybody)
    if left() > 0:
        try:
            counts.update(lead_openings.check(
                now=timezone.now(), deadline=time.monotonic() + min(lead_openings.CHECK_SECONDS, left()),
            ))
        except Exception:
            logger.exception('wahub cron: the leads with nothing to offer were not looked at')

    # 5. the live test: a message nobody answered is said on the contact (nothing is sent from here)
    try:
        counts['live_test_unanswered'] = live_test.flag_unanswered()
    except Exception:
        logger.exception('wahub cron: the look at the live test failed')

    # 5b. the safety net: a young message whose answer never started is started again (the same call, the same claim)
    try:
        counts.update(live_test.answer_late(reply_url))
    except Exception:
        logger.exception('wahub cron: the late live answers were not started')

    # 6. the holidays boards: what ManyChat has under their names (a read, kept for the bot's tool; asked when stale)
    if left() > HOLIDAY_FLOWS_FLOOR:
        try:
            counts['holiday_flows_found'] = len(holiday_flows.found())
        except Exception:
            logger.exception('wahub cron: the holidays boards were not looked up in ManyChat')

    # 6b. the follow-ups' templates: what ManyChat has under their names (a read, kept for the engine and the leads list;
    #     asked when the last answer is ten minutes old, and only when some follow-up is still to be found by name)
    if left() > HOLIDAY_FLOWS_FLOOR:
        try:
            counts['lead_templates_connected'] = len(lead_templates.connections(timezone.now()))
        except Exception:
            logger.exception('wahub cron: the templates of the follow-ups were not looked up in ManyChat')

    now = timezone.now()
    counts['pending_analysis'] = _to_analyze(now).count()
    counts['pending_matching'] = _to_match(now).count()
    counts['pending_shadow'] = shadow.pending(now).count()
    counts['pending_lead_stage'] = lead_stage.pending_count(now)
    counts['pending_lead_understanding'] = _to_understand(now).count() if _backfill_per_tick() else 0
    counts['seconds'] = round(time.monotonic() - started, 2)
    return counts
