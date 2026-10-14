"""
The words the bot comes back to a lead with (docs/WAHUB-CONTRACT-LEADS-V2.md, ג and ח.10–11).

The owner's word on them (12.10.2026): "הכי לא פורמלי, זורם, והמטרה זה רק
לתזכר את הלקוחות ולדייק שלא ישכחו — כאילו בן אדם שואל, לא בוט." So: no
"כאן קוגומלו", no comma after "היי" (a comma and formal punctuation make a
message formal and unreal), one or two short sentences the way a person writes
on WhatsApp, one emoji at most, plural address until anybody knows better, no
sales talk, and a question mark only where a real question ends. The child's
name only where it is known.

Each is filed as a "נוסח" in the bot's knowledge (`run`, called from the same
deploy command that files the old bot's texts): the owner edits them on the
"הבוט" screen, and lead_followups.py reads them from there. Until the filing
has run, and for a kind nobody has filed, the draft below is what is used.

These are the words that leave inside WhatsApp's 24 hours, and what the
conversation shows. After the 24 hours the customer gets the fixed text of the
approved template with the same name (docs/WAHUB-LEAD-TEMPLATES-FOR-OWNER.md):
the same sentence without the variables.

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

One phrasing here is no round of the bot's at all (BY_HAND): the message that
tells a customer on a waiting list that a seat came free. It leaves only when
a person presses for one row of the list (waiting_notify.py), and it is filed
with the others so that it, too, is edited on the "הבוט" screen and has its
line on the templates card. It names the lesson itself — the row's own class,
day, hour and branch — so its variables are its own:

    {חוג}     the class of the lesson waited for ("קפוארה גן חובה")
    {יום}     its day ("שלישי")
    {שעה}     its hour ("17:00")
    {סניף}    its branch
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
    KnowledgeHistory,
    KnowledgeItem,
)

SEED_TAG = 'חזרה ללידים 12.10.2026'
VARIABLES_NOTE = 'משתנים: {חוג} {סניף} {ילד} {גיל} {קישור}. קטע בסוגריים מרובעים נשמט כשחסר בו פרט.'
# The message a person sends by a press — "a seat came free" — to one customer on a waiting list
# (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ז.4). Not a kind of LeadFollowUp: the engine never plans it.
SEAT_FREED = 'seat_freed'
# The kinds that leave on a person's press and not by a round of the engine: no delays, no steps.
BY_HAND = (SEAT_FREED,)
VARIABLES_NOTES = {
    SEAT_FREED: 'משתנים (של השיעור שחיכו לו): {חוג} {יום} {שעה} {סניף}. קטע בסוגריים מרובעים נשמט כשחסר בו פרט.',
}
# Why the texts changed after they were first filed (the evening of 11.10). A row still as the filing left it is
# brought to the text below, as a new version with this reason in its history; a row a person has edited is left as
# they wrote it. `REVISED_BEFORE` holds the reasons of earlier rewrites, so a row only this file has rewritten still
# counts as untouched by a person the next time the words change.
REVISED = 'בעל המערכת 12.10.2026: חזרה לליד כמו שבן אדם שואל — הכי לא פורמלי, בלי "כאן קוגומלו" ובלי פסיק אחרי "היי"'
REVISED_BEFORE = ()
# The last reminder of a round. The customer who answers "לא" to it is not written to again (lead_ai.asked_to_stop):
# whoever changes these words keeps the request for that word and the promise beside it.
LAST_TIME = 'היי פעם אחרונה שאני מזכירה 🙂 אם זה כבר לא רלוונטי תכתבו לי לא ואני מפסיקה. ואם כן אני כאן'

# (kind, variant, title, when it is sent, the first time, [the second, the third], filed active)
PHRASINGS = [
    (
        NUDGE_LINK_REMINDER, '', 'חזרה לליד: קיבל קישור ועוד לא נרשם',
        'יום, שלושה ימים ושבוע אחרי שקיבל קישור להרשמה ולא נרשם',
        'היי מה נשמע 🙂 רציתי לבדוק אם הצלחתם להירשם ל{חוג}[ ב{סניף}] או שמשהו נתקע בדרך',
        [
            'היי רק מזכירה 🙂 אם בא לכם לנסות שיעור אחד[ ב{חוג}] ולראות אם זה מתאים[ ל{ילד}] תכתבו לי ואשלח קישור',
            LAST_TIME,
        ],
        True,
    ),
    (
        NUDGE_LINK_REMINDER, 'signup', 'חזרה לליד: התחיל הרשמה ולא סיים',
        'יום אחרי שמילא פרטים באתר ולא שילם (הפניות הבאות — כמו "קיבל קישור")',
        'היי ראיתי שהתחלתם להירשם[ ל{חוג}] ולא סיימתם 🙂 משהו נתקע באתר?',
        [],
        True,
    ),
    (
        NUDGE_LINK_REMINDER, 'commitment', 'חזרה לליד: חושש מהתחייבות',
        'יום אחרי ששאל מה קורה אם החוג לא יתאים (מי שקיבל קישור — הפניות הבאות כמו "קיבל קישור"; מי שלא — פעם אחת)',
        'היי לגבי מה ששאלתם 🙂 אפשר להתחיל משיעור ניסיון אחד ולראות אם זה מתאים. רוצים קישור?',
        [],
        True,
    ),
    (
        NUDGE_LINK_REMINDER, 'external', 'חזרה לליד: סניף חיצוני — ההרשמה מול העירייה',
        'פעם אחת, 3 ימים אחרי השיחה האחרונה עם מי שפנה על סניף חיצוני: לבדוק אם הסתדרו עם ההרשמה מול העירייה',
        'היי מה נשמע 🙂 רציתי לבדוק אם הסתדרתם עם ההרשמה ל{חוג}[ ב{סניף}] מול העירייה. אם משהו לא ברור או נתקע תכתבו לי ואעזור',
        [],
        True,
    ),
    (
        NUDGE_LINK_REMINDER, 'silent', 'חזרה לליד: קיבל תשובה ושתק',
        'פעם אחת, יומיים אחרי שקיבל תשובה ושתק — בלי קישור להרשמה ובלי סיבה ידועה שעצרה אותו',
        'היי מה נשמע 🙂 רציתי לבדוק אם נשארו שאלות לגבי {חוג}[ ל{ילד}]. אפשר להתחיל משיעור ניסיון אחד ולראות אם זה מתאים, לשלוח לכם קישור?',
        [],
        True,
    ),
    (
        NUDGE_WILL_CHECK, '', 'חזרה לליד: אמר שיבדוק ויחזור',
        'פעם אחת, יום אחרי שאמר שיבדוק או ישאל מישהו; בלי תשובה — עובר למשרד',
        'היי רק מזכירה לגבי {חוג}[ ל{ילד}] 🙂 יש כבר החלטה או שנשארה שאלה?',
        [],
        True,
    ),
    (
        NUDGE_TRIAL_FEEDBACK, '', 'חזרה לליד: אחרי שיעור ניסיון',
        'יום, שלושה ימים ושבוע אחרי שיעור ניסיון שלא הסתיים בהרשמה. בפנייה הראשונה שואלים רק איך היה '
        '(והיא לא יוצאת למי שכבר קיבל את ההודעה האוטומטית שאחרי השיעור)',
        'היי איך היה[ ל{ילד}] בשיעור הניסיון? 🙂',
        [
            'היי 🙂 אם היה לכם טוב בשיעור הניסיון אפשר להירשם כאן\n🔗 {קישור}\n'
            'ואם משהו לא התאים (היום, הקבוצה, הגיל) תכתבו לי ואבדוק אפשרות אחרת',
            'היי רק בודקת אם החוג עדיין רלוונטי[ ל{ילד}] 🙂 אם נוח לכם יותר לדבר בטלפון תכתבו מתי',
        ],
        True,
    ),
    (
        NUDGE_TRIAL_NO_SHOW, '', 'חזרה לליד: נרשם לניסיון ולא הגיע',
        'יום ושלושה ימים אחרי שיעור ניסיון שלא הגיעו אליו',
        'היי ראיתי שלא יצא לכם להגיע לשיעור הניסיון, קורה 🙂 רוצים שנקבע מועד אחר?',
        ['היי יש שיעורי ניסיון[ ב{חוג}] גם בימים אחרים 🙂 רוצים שאבדוק מה מתאים לכם?'],
        True,
    ),
    (
        NUDGE_DATE_ARRIVED, '', 'חזרה לליד: הגיע המועד שהלקוח נקב בו',
        'ביום שהלקוח אמר ("אחרי החגים", "בעוד חצי שנה"), ושוב אחרי שלושה ימים ואחרי שבוע',
        'היי מה שלומכם 🙂 דיברנו בעבר על חוג[ {חוג}][ ל{ילד}] ואמרתם שעכשיו יהיה זמן טוב יותר. רוצים שנקבע שיעור ניסיון?',
        ['היי רק מזכירה 🙂 רוצים שאשלח קישור לשיעור ניסיון[ ב{חוג}]?', LAST_TIME],
        True,
    ),
    (
        NUDGE_DATE_ARRIVED, 'age', 'חזרה לליד: הילד הגיע לגיל שממנו יש חוג',
        'ביום שבו מלאו לילד 3, ושוב אחרי שבוע',
        'היי 🙂 שאלתם אותנו פעם על חוג והילד היה עוד קטן מדי. עכשיו כבר אפשר, מתחילים מגיל {גיל}. '
        'רוצים שאבדוק לכם קבוצה[ ב{סניף}]?',
        ['היי רק מזכירה שכבר אפשר להתחיל[ ב{חוג}] 🙂 רוצים שאבדוק קבוצה מתאימה?'],
        True,
    ),
    (
        NUDGE_DATE_ARRIVED, 'not_ready', 'חזרה לליד: הילד לא רצה — שנה אחרי',
        'פעם אחת, ב-1 בספטמבר שאחרי שההורה אמר שהילד לא רצה',
        'היי מה נשמע 🙂 בשנה שעברה הייתם אצלנו בשיעור ניסיון[ ב{חוג}]. ילדים משתנים המון בשנה, רוצים לנסות שוב?',
        [],
        True,
    ),
    (
        NUDGE_OPENING, '', 'חזרה לליד: פנה ולא כתב מה הוא מחפש',
        'פעם אחת, כשעתיים אחרי הודעה שיש בה רק הנוסח של המודעה או "היי"',
        'היי 🙂 ראיתי שהתעניינתם בחוגים שלנו. לאיזה גיל ובאיזו עיר?',
        [],
        True,
    ),
    (
        NUDGE_DORMANT, '', 'חזרה לליד רדום: נפתחה הרשמה',
        'פעם אחת, ביום שייקבע (פתיחת הרשמה או עונה). טיוטה — לא פעילה עד שבעל המערכת מאשר נוסח ותאריך',
        'היי נפתחה ההרשמה לחוגים של קוגומלו 🙂 רוצים לשמוע מה חדש?',
        [],
        False,
    ),
    (
        SEAT_FREED, '', 'הודעה ללקוח: התפנה מקום ברשימת ההמתנה',
        'בלחיצה של המשרד על "להודיע ללקוח", כשהתפנה מקום למי שברשימת ההמתנה. לא נשלחת לבד',
        'היי התפנה מקום ב *{חוג}*[ ביום {יום}][ ב-{שעה}][ ({סניף})] 🎉 רוצים שנשמור לכם אותו?',
        [],
        True,
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
            'data': {'variants': list(later), 'verbatim': True, 'when': f'{when}. {VARIABLES_NOTES.get(kind, VARIABLES_NOTE)}'},
        }
        for kind, variant, title, when, body, later, active in PHRASINGS
    ]


DEFAULTS = {
    phrasing_key(kind, variant): {'texts': [body, *later], 'active': active, 'title': title, 'when': when}
    for kind, variant, title, when, body, later, active in PHRASINGS
}


def _revise(record: dict, user, result: dict) -> None:
    """
    A text that changed after it was filed, against the row that is already
    there (the way knowledge_import does it for the old bot's texts). Untouched
    since the filing — or rewritten since only by this file — → the new words,
    as a new version with REVISED in its history. Edited by a person → theirs
    stands, and the row is named in `revision_left`. Only the words are
    written: never whether the row is on, its title or its scope.
    """
    item = KnowledgeItem.objects.filter(source_note=record['source_note']).order_by('id').first()
    if item is None or KnowledgeHistory.objects.filter(item=item, note=REVISED[:300]).exists():
        return
    ours = {earlier[:300] for earlier in REVISED_BEFORE}
    later = KnowledgeHistory.objects.filter(item=item, version__gt=1).values_list('note', 'changed_by_id')
    if item.version != 1 and not (later and all(note in ours and who is None for note, who in later)):
        result['revision_left'].append(item.title or item.key)
        return
    updated = knowledge.update_item(item, {'body': record['body'], 'data': dict(record['data'])}, user, note=REVISED)
    if updated.version != item.version:
        result['revised'] += 1


def run(*, user=None, dry_run: bool = False) -> dict:
    """
    File the drafts in the bot's knowledge. A record that is already there —
    by its `source_note` — is not filed again, and neither is a key somebody
    already wrote a live phrasing for. A row filed with older words is brought
    to these ones only while nobody has edited it (`_revise`) — and then the
    follow-ups that are already planned are reworded with it.
    """
    existing = set(
        KnowledgeItem.objects.filter(source_note__startswith=SEED_TAG).values_list('source_note', flat=True)
    )
    taken = set(
        KnowledgeItem.objects.filter(kind=KIND_PHRASING, key__startswith=KEY_PREFIX, is_active=True)
        .values_list('key', flat=True)
    )
    result = {'created': 0, 'skipped': 0, 'revised': 0, 'revision_left': [], 'reworded': 0}
    for record in records():
        if record['source_note'] in existing:
            result['skipped'] += 1
            if not dry_run:
                _revise(record, user, result)
            continue
        if record['is_active'] and record['key'] in taken:
            result['skipped'] += 1
            continue
        result['created'] += 1
        if not dry_run:
            knowledge.create_item(dict(record), user, note='נוסחי החזרה ללידים')
    if result['revised']:
        # Follow-ups already planned with the older words say the new ones too — on the screen now, not only
        # on the day they leave.
        from apps.wahub import lead_followups

        result['reworded'] = lead_followups.reword_open()
    return result
