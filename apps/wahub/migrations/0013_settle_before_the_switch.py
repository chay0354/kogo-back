# The switch from the old bot, 14.10.2026 12:32 (Israel). The owner, an hour after it: "המערכת לא מסונכרנת עם
# מה שהיה… יש לי מלא שיחות פתוחות ובאלגן"; and then: "תמזג… שיש את כל הלידים שנשמרו לעתיד… שלא רוצה לפספס".
#
# The old bot answered every customer's message, and its answers were never copied to Kogo; the office read
# and answered in ManyChat's own inbox. So two marks of the conversations screen said nothing true about any
# conversation whose last message came before the switch: "מחכים לתשובה" (waiting_since — 227 in production)
# and "לא נקראו" (unread_count — 210). Those two, and only those, are settled here, for contacts that are not
# a demo or the live test and whose last customer message came before the first moment the new bot answered
# everybody (bot_mode: `first_since`, or `since` while it was not kept yet).
#
# Nothing else is touched: a request for a person (needs_human) stays open — the office decides on each; the
# office's future leads, the dates a conversation named, the waiting lists, the leads' stages and the bot's
# follow-ups are as they were. With no switch on record (a fresh database, tests) nothing happens at all.
# One-way by nature: what was cleared was never true, and there is nothing to restore.

from datetime import datetime

from django.db import migrations
from django.db.models import Q


def _switch(WahubSetting):
    row = WahubSetting.objects.filter(key='bot_mode').first()
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    raw = value.get('first_since') or value.get('since')
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def settle(apps, schema_editor):
    WahubSetting = apps.get_model('wahub', 'WahubSetting')
    Contact = apps.get_model('wahub', 'Contact')
    switch = _switch(WahubSetting)
    if switch is None:
        return
    before = Contact.objects.filter(is_demo=False, live_test=False).filter(
        Q(last_inbound_at__lt=switch) | Q(last_inbound_at__isnull=True),
    )
    waiting = before.filter(waiting_since__isnull=False, waiting_since__lt=switch).update(waiting_since=None)
    unread = before.filter(unread_count__gt=0).update(unread_count=0)
    print(f'\n  wahub 0013: before the switch at {switch.isoformat()} — "מחכים לתשובה" cleared on {waiting}, "לא נקראו" on {unread}')


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0012_contact_lead_opened'),
    ]

    operations = [
        migrations.RunPython(settle, migrations.RunPython.noop),
    ]
