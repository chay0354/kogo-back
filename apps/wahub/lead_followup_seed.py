"""
The words the bot comes back to a lead with (docs/WAHUB-CONTRACT-LEADS-V2.md, ג and ח.10–11).

Drafts, after docs/WAHUB-CUSTOMER-TYPES-2026-10-12.md §6: one to three lines,
one question, one emoji at most, plural address until anybody knows better,
and the child's name only where it is known. The owner settles the final
wording — which is why each is filed as a "נוסח" in the bot's knowledge
(`run`, called from the same deploy command that files the old bot's texts):
he edits them on the "הבוט" screen, and lead_followups.py reads them from
there. Until the filing has run, and for a kind nobody has filed, the draft
below is what is used.

A phrasing's body is the first time the bot comes back; `variants` are the
second and the third. What a phrasing may use:

    {חוג}     the kind of class asked about      missing → "החוג" ("לחוג", "בחוג")
    {סניף}    the branch                          missing → dropped
    {ילד}     the child's name                    missing → dropped
    {גיל}     the age from which a class opens
    {קישור}   the registration link

    [ … ]     a stretch that is dropped whole when something in it is missing:
              "איך היה[ ל{ילד}] בשיעור הניסיון?"

These phrasings are never read out to the bot that answers customers
(knowledge.relevant_items leaves them out): they are what the system sends by
itself, not what an answer may be built from.
"""
from __future__ import annotations

from apps.wahub import knowledge
from apps.wahub.models import (
    KIND_PHRASING,
    NUDGE_DATE_ARRIVED,
    NUDGE_DORMANT,
    NUDGE_LINK_REMINDER,
    NUDGE_OPENING,
    NUDGE_TRIAL_FEEDBACK,
    NUDGE_TRIAL_NO_SHOW,
    NUDGE_WILL_CHECK,
    SCOPE_BUSINESS,
    WHEN_INTERNAL,
    KnowledgeItem,
)

SEED_TAG = 'חזרה ללידים 12.10.2026'
VARIABLES_NOTE = 'משתנים: {חוג} {סניף} {ילד} {גיל} {קישור}. קטע בסוגריים מרובעים נשמט כשחסר בו פרט.'
LAST_TIME = "היי, פעם אחרונה שאני מזכירה 🙂 אם זה כבר לא רלוונטי — כתבו 'לא' ולא אשלח יותר. ואם כן, אני כאן."

# (kind, variant, title, when it is sent, the first time, [the second, the third], filed active)
PHRASINGS = [
    (
        NUDGE_LINK_REMINDER, '', 'חזרה לליד: קיבל קישור ועוד לא נרשם',
        'יום, שלושה ימים ושבוע אחרי שקיבל קישור להרשמה ולא נרשם',
        'היי, כאן קוגומלו 🙂 שלחתי קישור ל{חוג}[ ב{סניף}]. הצלחתם להירשם, או שמשהו נתקע באתר?',
        [
            'היי, רוצים שאשלח שוב את הקישור לשיעור ניסיון[ ב{חוג}]? אפשר להתחיל משיעור אחד[ ולראות אם {ילד} נהנה].',
            LAST_TIME,
        ],
        True,
    ),
    (
        NUDGE_LINK_REMINDER, 'signup', 'חזרה לליד: התחיל הרשמה ולא סיים',
        'יום אחרי שמילא פרטים באתר ולא שילם (הפניות הבאות — כמו "קיבל קישור")',
        'היי, כאן קוגומלו 🙂 ראיתי שהתחלתם הרשמה[ ל{חוג}] ולא סיימתם. משהו נתקע באתר? אשמח לעזור.',
        [],
        True,
    ),
    (
        NUDGE_LINK_REMINDER, 'commitment', 'חזרה לליד: חושש מהתחייבות',
        'יום אחרי ששאל מה קורה אם החוג לא יתאים (הפניות הבאות — כמו "קיבל קישור")',
        'היי, לגבי מה ששאלתם — אפשר להתחיל משיעור ניסיון אחד ולראות אם זה מתאים. רוצים שאשלח קישור?',
        [],
        True,
    ),
    (
        NUDGE_WILL_CHECK, '', 'חזרה לליד: אמר שיבדוק ויחזור',
        'פעם אחת, יום אחרי שאמר שיבדוק או ישאל מישהו; בלי תשובה — עובר למשרד',
        'היי, הספקתם לחשוב על {חוג}[ ל{ילד}]? אם נשארה שאלה פתוחה — אני כאן.',
        [],
        True,
    ),
    (
        NUDGE_TRIAL_FEEDBACK, '', 'חזרה לליד: אחרי שיעור ניסיון',
        'יום, שלושה ימים ושבוע אחרי שיעור ניסיון שלא הסתיים בהרשמה. בפנייה הראשונה שואלים רק איך היה',
        'היי, איך היה[ ל{ילד}] בשיעור הניסיון? 🙂',
        [
            'אם היה טוב — ההרשמה כאן: {קישור}\nואם משהו לא התאים (היום, הקבוצה, הגיל) — ספרו לי ואבדוק אפשרות אחרת.',
            'היי, רק בודקת אם {חוג} עדיין רלוונטי[ ל{ילד}]. אם נוח יותר שנדבר בטלפון — כתבו מתי.',
        ],
        True,
    ),
    (
        NUDGE_TRIAL_NO_SHOW, '', 'חזרה לליד: נרשם לניסיון ולא הגיע',
        'יום ושלושה ימים אחרי שיעור ניסיון שלא הגיעו אליו',
        'היי, לא הספקתם להגיע לשיעור הניסיון — קורה 🙂 רוצים מועד אחר?',
        ['היי, יש שיעורי ניסיון[ ב{חוג}] גם במועדים אחרים. רוצים שאבדוק מה מתאים לכם?'],
        True,
    ),
    (
        NUDGE_DATE_ARRIVED, '', 'חזרה לליד: הגיע המועד שהלקוח נקב בו',
        'ביום שהלקוח אמר ("אחרי החגים", "בעוד חצי שנה"), ושוב אחרי שלושה ימים ואחרי שבוע',
        'היי, דיברנו בעבר על {חוג}[ ל{ילד}]. עכשיו זמן טוב יותר? אפשר לקבוע שיעור ניסיון.',
        ['היי, רוצים שאשלח קישור לשיעור ניסיון[ ב{חוג}]? אפשר להתחיל משיעור אחד.', LAST_TIME],
        True,
    ),
    (
        NUDGE_DATE_ARRIVED, 'age', 'חזרה לליד: הילד הגיע לגיל שממנו יש חוג',
        'ביום שבו מלאו לילד 3, ושוב אחרי שבוע',
        'היי, לפני כמה זמן שאלתם על חוג[ ל{ילד}]. אצלנו מתחילים מגיל {גיל} — אז עכשיו אפשר 🎈 רוצים שאבדוק קבוצה[ ב{סניף}]?',
        ['היי, רק מזכירה שכבר אפשר להתחיל[ ב{חוג}]. רוצים שאבדוק קבוצה מתאימה?'],
        True,
    ),
    (
        NUDGE_DATE_ARRIVED, 'not_ready', 'חזרה לליד: הילד לא רצה — שנה אחרי',
        'פעם אחת, ב-1 בספטמבר שאחרי שההורה אמר שהילד לא רצה',
        'היי, בשנה שעברה הייתם אצלנו בשיעור ניסיון[ ב{חוג}]. ילדים משתנים הרבה בשנה 🙂 רוצים לנסות שוב?',
        [],
        True,
    ),
    (
        NUDGE_OPENING, '', 'חזרה לליד: פנה ולא כתב מה הוא מחפש',
        'פעם אחת, כשעתיים אחרי הודעה שיש בה רק הנוסח של המודעה או "היי"',
        'היי 🙂 ראיתי שהתעניינתם בחוגים שלנו. לאיזה גיל, ובאיזו עיר?',
        [],
        True,
    ),
    (
        NUDGE_DORMANT, '', 'חזרה לליד רדום: נפתחה הרשמה',
        'פעם אחת, ביום שייקבע (פתיחת הרשמה או עונה). טיוטה — לא פעילה עד שבעל המערכת מאשר נוסח ותאריך',
        'היי, נפתחה ההרשמה לחוגים של קוגומלו 🙂 רוצים לשמוע מה חדש?',
        [],
        False,
    ),
]

KEY_PREFIX = knowledge.LEAD_PHRASING_PREFIX


def phrasing_key(kind: str, variant: str = '') -> str:
    return f'{KEY_PREFIX}{kind}' + (f'_{variant}' if variant else '')


def _source_note(key: str) -> str:
    return f'{SEED_TAG} · {key}'


def records() -> list[dict]:
    """The drafts as knowledge records; `source_note` is what makes the filing happen once."""
    return [
        {
            'kind': KIND_PHRASING, 'key': phrasing_key(kind, variant), 'title': title, 'body': body,
            'is_active': active, 'when_to_say': WHEN_INTERNAL, 'scope_level': SCOPE_BUSINESS,
            'source_note': _source_note(phrasing_key(kind, variant)),
            'data': {'variants': list(later), 'verbatim': True, 'when': f'{when}. {VARIABLES_NOTE}'},
        }
        for kind, variant, title, when, body, later, active in PHRASINGS
    ]


DEFAULTS = {
    phrasing_key(kind, variant): {'texts': [body, *later], 'active': active, 'title': title, 'when': when}
    for kind, variant, title, when, body, later, active in PHRASINGS
}


def run(*, user=None, dry_run: bool = False) -> dict:
    """
    File the drafts in the bot's knowledge. A record that is already there —
    by its `source_note`, whatever the owner has since made of it — is left
    alone, and so is a key somebody already wrote a live phrasing for.
    """
    existing = set(
        KnowledgeItem.objects.filter(source_note__startswith=SEED_TAG).values_list('source_note', flat=True)
    )
    taken = set(
        KnowledgeItem.objects.filter(kind=KIND_PHRASING, key__startswith=KEY_PREFIX, is_active=True)
        .values_list('key', flat=True)
    )
    result = {'created': 0, 'skipped': 0}
    for record in records():
        if record['source_note'] in existing or (record['is_active'] and record['key'] in taken):
            result['skipped'] += 1
            continue
        result['created'] += 1
        if not dry_run:
            knowledge.create_item(dict(record), user, note='נוסחי החזרה ללידים')
    return result
