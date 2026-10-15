"""
The tools the shadow bot may call (docs/WAHUB-CONTRACT-STAGE2.md, ב.2;
docs/WAHUB-CONTRACT-BOT-TOOLS.md for everything about a class).

Every tool reads Kogo in process — the same functions the public widget uses
for classes, dates, prices and seats (apps/customers/widget_views.py), and the
registration's own pieces for "how much if I register now"
(apps/core/registration_quote.py) — so the bot's answer about a class is the
widget's answer. Nothing here writes, and nothing here sends: `request_human`
only marks the draft. What a class costs the business — an instructor's pay, a
bonus — and what a family paid are read by no tool.

The same tools serve Claude (as tool definitions) and the stub (called
directly), so a stub answer is built from the very data Claude would get.
The payloads of a class are built in course_tools.py.

`find_courses` keeps the order of the conversation (the owner, 12.10.2026,
docs/WAHUB-MASTER-PLAN-2026-10-12.md 1ג: "first find out, then offer"): city or
branch, then the field, then the child's age or grade — and only then classes.
While one of them is missing it answers with `need` and the question to ask,
and with no class at all, so there is nothing to offer too early; `show_all`
is for a customer who really asked to see everything. An instruction can be
forgotten; a class that never came back cannot be recited.

`holiday_dates` (the owner, 13.10.2026) is the one tool whose answer is followed
by something: it names the ManyChat automation that carries the holidays board
of the branch (apps/wahub/holiday_flows.py). The tool itself still only reads —
which board, and whether ManyChat was last heard to have it. The automation
leaves after the answer's text, and only where the answer itself really leaves
(shadow.flows_to_send, live_test.py); in the shadow, in "נסה שאלה" and in the
bot's self-checks nothing is sent.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings

from apps.courses import age_groups
from apps.wahub import analysis, course_tools, holiday_flows, knowledge, matching, trial_offer
from apps.wahub.analysis import _same_city
from apps.wahub.course_tools import DAY_NAMES, trial_line  # noqa: F401 — both are read from here too
from apps.wahub.models import KIND_ALIAS, KIND_CONTACT, SCOPE_BRANCH, Contact, KnowledgeItem

logger = logging.getLogger(__name__)

MAX_COURSES = 12
EXTERNAL_NOTE = 'סניף חיצוני (עירייה / מתנ"ס): ההרשמה, המחירים והפרטים מולם. לא לנחש — "לבדוק מול העירייה" + קישור ההרשמה.'
# One line for every tool that speaks of a class: where the truth is, and where it is fixed.
FROM_KOGO_ONLY = (
    'מחיר, שיעור ניסיון, דמי רישום, הנחות, קיזוז, תפוסה, מועדים ומדריך נאמרים רק ממה שהכלי מחזיר עכשיו — '
    'אף פעם לא מהזיכרון, מהשיחה או מהידע. מה שחוזר הוא מה שהאתר מציג; אם הוא לא נכון, התיקון הוא בכרטיס החוג ב-Kogo, לא בבוט. '
)

# What `find_courses` still needs before it spells out a class, in the order it is asked.
NEED_CITY, NEED_BRANCH, NEED_FIELD, NEED_AGE, NEED_GRADE = 'city', 'branch', 'course_type', 'age', 'grade'
ASK = {
    NEED_CITY: 'באיזו עיר או סניף מדובר?',
    NEED_BRANCH: 'באיזה סניף נוח לכם?',
    NEED_FIELD: 'באיזה תחום מדובר?',
    NEED_AGE: 'בן כמה הילד, או באיזו כיתה?',
    NEED_GRADE: 'באיזו כיתה או גן הילד?',
}
NEED_LABELS = {
    NEED_CITY: 'עיר או סניף', NEED_BRANCH: 'סניף', NEED_FIELD: 'תחום', NEED_AGE: 'גיל או כיתה', NEED_GRADE: 'כיתה או גן',
}
ASK_FIRST = 'קודם שואלים את הלקוח את השאלה שב-ask, ורק אותה. לא מציעים חוג, יום, שעה או מחיר לפני שהיא נענתה.'
# Every branch of ours is looked at when the field and the age are known and no place is (the owner, 14.10.2026:
# "לסרוק את כל השיעורים במערכת ולבדוק איפה יש"), and when the place asked about has nothing that fits.
MAX_SCAN_BRANCHES = 4
SCAN_NOWHERE = 'נסרקו כל הסניפים שלנו: באף אחד מהם אין כרגע חוג בתחום ולגיל שנמסרו. אומרים את זה בפשטות; לא שואלים באיזו עיר.'
SCAN_FEW = 'נסרקו כל הסניפים שלנו: רק בסניפים שב-branches יש חוג שמתאים. אומרים ללקוח איפה יש (סניף ועיר), ושואלים מה נוח לו.'
SCAN_MANY = 'נסרקו כל הסניפים שלנו: יש חוג שמתאים בכמה ערים (cities). שואלים באיזו עיר נוח; אפשר לומר שיש בכמה ערים.'
SCAN_ONE = 'נסרקו כל הסניפים שלנו: רק בסניף הזה יש חוג שמתאים — אומרים איפה הוא (סניף ועיר).'
ELSEWHERE = (
    'במקום שנשאל אין, אבל בסניפים שב-elsewhere יש חוג בתחום ולגיל שנמסרו: אומרים שכאן אין ושם יש (סניף ועיר), '
    'ושואלים אם זה מתאים. פרטים של חוג משם (ימים, שעות, מחיר) — רק אחרי find_courses על הסניף הזה.'
)

TOOLS = [
    {
        'name': 'find_courses',
        'description': (
            'החוגים והמועדים כפי שהם במערכת Kogo: חוג, קבוצת גיל, יום, שעה, מדריך, מחיר חודשי, דמי רישום, שיעור ניסיון, '
            'כמות רשומים, מקומות פנויים והתאריכים שאפשר להירשם בהם עכשיו לשיעור ניסיון. מסננים לפי עיר, סניף, גיל הילד בשנים או כיתה, ותחום. '
            'הכלי שומר על סדר השיחה — עיר או סניף, אחר כך תחום, אחר כך גיל או כיתה — ומפרט חוגים רק כשכל אלה ידועים. '
            'כשחסר פרט הוא מחזיר need ו-ask ואת מה שיש לבחור ממנו (cities, branches, course_types, between), בלי חוגים: '
            '(בלי עיר ובלי סניף חוזרת רשימת הערים שיש לנו בהן סניף — cities. אבל כשהתחום והגיל ידועים, הכלי סורק את כל הסניפים שלנו '
            '(scanned_all): חוזר החוג מהסניף היחיד שיש בו, או need=branch עם הסניפים שיש בהם, או need=city עם הערים שיש בהן, '
            'ובאף סניף — אומרים שאין, בלי לשאול עיר. לכן כשהלקוח לא אמר מקום ושאל על תחום וגיל — קוראים עם תחום וגיל בלי עיר.) '
            '(כשבמקום שנשאל אין, elsewhere אומר באילו סניפים אחרים יש — אומרים את זה ללקוח.) '
            '(חוג שפתוח לגיל הזה רק דרך מסלול — join_note: אומרים את המסלול, שמו, ימיו ומחירו.) '
            'שואלים את הלקוח את השאלה הזאת בלבד, ולא מציעים חוג, יום, שעה או מחיר. בכל קריאה מעבירים את כל מה שהלקוח כבר אמר. '
            'show_all=true רק כשהלקוח ביקש במפורש לראות את כל מה שיש ("פשוט תגידו מה יש") — לא כדי לדלג על שאלה. '
            'חוגים חוזרים רק לגיל ולתחום שנמסרו, ורק עליהם עונים: לא מזכירים קבוצות גיל אחרות או תחומים אחרים. '
            'עלות שיעור ניסיון נאמרת על חוג מסוים שנבחר, וכששאלו עליה; לא מכלילים על כל החוגים. '
            'כל חוג נושא את הפרטים של עצמו בלבד — לא מעבירים מחיר, ניסיון, יום או מדריך מחוג אחד לאחר. '
            'חובה לקרוא לפני כל תשובה שמכילה פרט על חוג. ' + FROM_KOGO_ONLY +
            'שיעור ניסיון נקבע לכל חוג בנפרד (trial.is_paid, trial.price): באותו סניף יש חוגים שהניסיון בהם בתשלום וחוגים שלא, '
            'ולכן לא אומרים "הניסיון בסניף הזה ללא עלות" — עונים על החוג שמתאים לגיל ולתחום של הילד, וכשלא ברור איזה חוג שואלים גיל או כיתה ותחום. '
            'trial.is_paid=false פירושו ללא עלות, וזה מה שאומרים; is_paid=true — אומרים את trial.price ואת trial.credit_rule. '
            'קבוצת הגיל של חוג היא ages (במילים); גיל על הגבול בין שתי קבוצות (בן 6: גן חובה או כיתה א) מחזיר את שתיהן, ואז שואלים באיזו כיתה הילד. '
            '"מה להביא לשיעור ניסיון" הוא trial.bring של החוג — מצטטים אותו; ריק = "אין לי את הפרט הזה, אפשר לבדוק במשרד". '
            '"כמה רשומים", "כמה מקומות נשארו", "יש מקום", "קבוצה קטנה או גדולה" — עונים מהשיעור שנשאל עליו (היום והשעה): '
            'lessons[].enrolled (רשומים), capacity (כמה מקומות יש), seats_left (כמה פנויים), is_full (מלא), ומצטטים את lessons[].seats_text. '
            'לא מחברים ולא מחסרים לבד, לא עונים על ימים שלא נשאלו, ולא מעבירים מספר מיום אחד לאחר; כשלא ברור על איזה יום שואלים — שואלים. '
            'trial_seats_left הוא מקומות לשיעור ניסיון בתאריך מסוים, לא "כמה מקומות נשארו בקבוצה". '
            '"מתי אפשר להגיע לשיעור ניסיון", "אפשר השבוע", "אפשר היום" — עונים רק מ-trial.dates של החוג (התאריכים שהאתר מציע עכשיו; this_week ו-when אומרים אם זה השבוע, היום או מחר) '
            'ומצטטים את trial.dates_text. תאריך שלא שם לא מציעים ולא מחשבים לבד; full_dates מלאים, not_offered לא פתוחים לניסיון, ו-can_book=false פירושו שאין כרגע תאריך. '
            'כשיש הרבה חוגים, 12 מפורטים והשאר ב-more_courses בשורה אחת כל אחד — מצמצמים לפי תחום וגיל, או קוראים ל-course_card. '
            'סניף חיצוני חוזר עם is_external=true ובלי חוגים — אז אומרים "לבדוק מול העירייה" ושולחים את קישור ההרשמה.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'city': {'type': 'string', 'description': 'עיר, כפי שהלקוח כתב'},
                'branch': {'type': 'string', 'description': 'שם הסניף או כינוי שלו'},
                'age': {'type': 'number', 'description': 'גיל הילד בשנים (4.5 = ארבע וחצי)'},
                'grade': {'type': 'string', 'description': 'הכיתה או הגן, כשהלקוח אמר אותם: "ג", "כיתה ז", "גן חובה", "בוגרים"'},
                'course_type': {'type': 'string', 'description': 'התחום, כפי שהלקוח אמר אותו'},
                'show_all': {
                    'type': 'boolean',
                    'description': 'true רק כשהלקוח ביקש במפורש לראות את כל מה שיש, אחרי שנשאל. אחרת משאירים ריק',
                },
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'course_card',
        'description': (
            'כל מה שיש במערכת על חוג אחד: תיאור, קבוצת גיל, מחיר חודשי, דמי הרישום שייגבו בפועל לחוג הזה, שיעור ניסיון '
            '(בתשלום או לא, מחיר, כלל הקיזוז, מה להביא, פתוח או סגור), כל המועדים עם מדריך, חדר, כמות רשומים, מגבלת המקומות, '
            'מקומות פנויים, מסלולים משולבים (פעמיים בשבוע) ומחירם, וההנחות שמוגדרות במערכת. '
            'כמה רשומים וכמה מקומות נשארו נאמרים לכל מועד בנפרד, מהמועד שנשאל עליו (lessons[].seats_text) — לא מחשבים לבד. '
            'מועד שרשומים בו יותר מהמגבלה (enrolled גדול מ-capacity): אומרים רק שהשיעור מלא, כמו ב-seats_text, בלי "N מתוך M". '
            '"יש שיעור היום / השבוע / בתאריך מסוים": לכל מועד lessons[].cancelled_dates — התאריכים הקרובים שבהם השיעור בוטל במערכת, '
            'next_date — התאריך הקרוב שבו הוא מתקיים, ו-held_text — המשפט המוכן. תאריך שאינו ב-cancelled_dates לא בוטל במערכת; '
            'חג או חופשה שלא סומנו שם — המערכת לא יודעת עליהם. '
            'ב-trial: כל התאריכים שהאתר מציע עכשיו לשיעור ניסיון (dates), המשפט המוכן (dates_text), התאריכים המלאים (full_dates) '
            'ואלה שלא פתוחים לניסיון ולמה (not_offered); תאריך לניסיון נאמר רק מתוך dates. '
            'קוראים עם course_id מתוך find_courses (או שם החוג וסניף). ' + FROM_KOGO_ONLY
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'course_id': {'type': 'string', 'description': 'ה-id של החוג מתוך find_courses'},
                'name': {'type': 'string', 'description': 'שם החוג, כשאין id'},
                'branch': {'type': 'string', 'description': 'שם הסניף או כינוי שלו (עם name)'},
                'city': {'type': 'string', 'description': 'עיר (עם name)'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'quote_registration',
        'description': (
            '"כמה יעלה אם נירשם עכשיו": התשלום הראשון והמחיר החודשי שאחריו, מחושבים באותו קוד שמחייב בהרשמה באתר — '
            'החלק היחסי של החודש לפי השיעורים שעוד נשארו בו, דמי רישום, קיזוז שיעור ניסיון ששולם, וההנחות (ילד שני, חוג נוסף, רישום מוקדם). '
            'חובה לקרוא כששואלים כמה יעלה להירשם, כמה משלמים עכשיו או כמה יוצא החודש — לא מחשבים לבד ולא מעריכים. '
            'בתשובה מפרטים את first_charge.breakdown בשתיים–שלוש שורות ואז את monthly_after. '
            'מה שלא ידוע על המשפחה לא מניחים: אח רשום או ניסיון ששולם נמסרים רק אם הלקוח אמר או שזה בכרטיס הלקוח; '
            'אחרת משאירים ריק (ללקוח מזוהה המערכת בודקת לבד), ואומרים על מה החישוב מבוסס (based_on).'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'course_id': {'type': 'string', 'description': 'ה-id של החוג מתוך find_courses'},
                'lesson_id': {'type': 'string', 'description': 'ה-id של המועד שנבחר, כשיש לחוג כמה ימים. ריק = המועד הראשון'},
                'track_id': {'type': 'string', 'description': 'ה-id של מסלול משולב (tracks), כשנרשמים לפעמיים בשבוע'},
                'start_date': {'type': 'string', 'description': 'תאריך ההרשמה, YYYY-MM-DD. ריק = היום'},
                'child_age': {'type': 'number', 'description': 'גיל הילד בשנים, אם ידוע'},
                'grade': {'type': 'string', 'description': 'הכיתה או הגן, אם הלקוח אמר'},
                'siblings_registered': {'type': 'boolean', 'description': 'אח או אחות כבר רשומים לחוג אצלנו. ריק כשלא ידוע'},
                'took_paid_trial': {'type': 'boolean', 'description': 'הילד היה בשיעור ניסיון בתשלום בסניף הזה. ריק כשלא ידוע'},
                'another_class_same_child': {'type': 'boolean', 'description': 'זה חוג נוסף לילד שכבר רשום ומשלם על חוג אחר'},
            },
            'required': ['course_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'instructor_info',
        'description': (
            'מדריך לפי שם (אפשר גם לפי סניף): התחומים שהוא מלמד, הסניפים, המועדים שלו ותמונה. '
            'אין בכלי טלפון או פרט אישי של מדריך — את אלה לא מוסרים ללקוח; למי שמבקש לדבר עם מדריך יש נוסח משלו.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': 'שם המדריך או חלק ממנו. ריק = כל המדריכים של הסניף'},
                'branch': {'type': 'string', 'description': 'שם הסניף, כינוי או עיר'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'branch_info',
        'description': (
            'סניף: כתובת, עיר, הוראות הגעה, טלפון הסניף, טלפון המשרד, מנהל, התחומים שיש בו, הימים והשעות שבהם יש בו חוגים, '
            'והאם הוא סניף חיצוני. לסניף שלנו גם wifi — שם הרשת האלחוטית (name) והסיסמה שלה (code), כפי שהם רשומים במערכת: '
            'נמסרים רק כששואלים עליהם, בשתי שורות קצרות, ונכתבים אות באות כפי שהם (גם כשהם באותיות לועזיות; לא מתעתקים ולא מתקנים). '
            'wifi ריק (null), או סיסמה ריקה = לא רשום במערכת: אומרים שבודקים מול המשרד ומסמנים לנציג, ולא מנחשים. '
            'קוד כניסה, קוד לדלת וכל קוד אחר אינם בכלי ואינם נמסרים. שדה ריק = אין את המידע; לא להמציא. '
            'phone — הטלפון של המשרד, שהוא הטלפון של כל הסניפים: נמסר כששואלים על טלפון של סניף. '
            'לסניף אין מנהל ואין בכלי שם של מנהל: מי ששואל מי מנהל את הסניף או מבקש לדבר עם מנהל — הכול דרך המשרד, '
            'request_human עם מה שהלקוח ביקש; שם לא ממציאים.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {'branch': {'type': 'string', 'description': 'שם הסניף או כינוי'}},
            'required': ['branch'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'holiday_dates',
        'description': (
            'חגים וחופשות: לכל שאלה על שיעורים בחג או בחופשה — "יש שיעורים בחנוכה?", "יש שיעור בחג?", "מתי החופשה?", '
            '"עובדים בפסח?", ערב חג, חול המועד — קוראים לכלי הזה עם הסניף שנשאל עליו (או העיר). '
            'לכל סניף יש לוח חופשות: אוטומציה שהמערכת שולחת ללקוח אחרי התשובה, עם תמונה של כל התאריכים. '
            'found=true: התשובה היא משפט קצר אחד שהלוח נשלח (אפשר לצטט את say), והמערכת שולחת אותו מיד אחריה. '
            'לא מפרטים תאריכי חופשה בעצמנו — לא מהזיכרון, לא מהשיחה ולא מהימים המיוחדים שבידע — ולא עונים "כן" או "לא" על חג מסוים: הלוח עונה. '
            'need=branch: עוד לא ידוע באיזה סניף — שואלים את ask, ורק אותה. ללקוח רשום אפשר לקרוא בלי סניף: הכלי לוקח את הסניף של החוג של הילד. '
            'found=false בלי need: לסניף הזה אין לוח, או שהוא לא נמצא — אומרים את הנוסח missing_info ומסמנים לנציג (request_human); '
            'לא כותבים שנשלח לוח. הכלי עצמו רק קורא, אבל קריאה שחזרה עם found=true פירושה שהלוח יישלח ללקוח אחרי התשובה: '
            'קוראים לו רק כשהלקוח שאל על חגים או על חופשות, ולא "ליתר ביטחון".'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'branch': {'type': 'string', 'description': 'שם הסניף או כינוי שלו, כפי שהלקוח כתב או כפי שהוא בכרטיס הלקוח'},
                'city': {'type': 'string', 'description': 'עיר, כשהלקוח אמר רק עיר'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'office_hours_now',
        'description': 'האם המשרד פתוח עכשיו לפי שעון ישראל, השעות של היום, יום מיוחד אם יש, והודעת "סגור".',
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'customer_card',
        'description': (
            'מה המערכת יודעת על הכותב: אם הוא לקוח רשום, הילדים והסטטוס שלהם, לאיזה חוג, סניף, יום ושעה כל ילד רשום (classes), '
            'שיעור ניסיון שנקבע או שהיה (trial), ומה ידוע מהשיחות. מה שכתוב כאן על ילד רשום לא נשאל שוב מהלקוח. '
            'what_we_know נאסף משיחות קודמות והוא רקע בלבד.'
        ),
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'request_human',
        'description': (
            'מסמן שהשיחה צריכה נציג אנושי (הלקוח ביקש מפורשות, השכרת סטודיו, בעיה רפואית, או מה שהנושא קובע). '
            'רק מסמן — לא שולח דבר. אחרי הסימון התשובה ללקוח אומרת שנציג יחזור אליו.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {'reason': {'type': 'string', 'description': 'הסיבה, בקצרה ובעברית'}},
            'required': ['reason'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'tag_conversation',
        'description': (
            'מסמן על השיחה תגית שנושא קובע ("בסיום לתייג" — ברכת יום הולדת, הפעלת יום הולדת), עם הפרטים שנאספו. '
            'רק מסמן — לא שולח דבר ולא משנה דבר אצל הלקוח. קוראים פעם אחת, כשהנושא הגיע לשלב התיוג; תגית שאין בנושאים לא מתקבלת. '
            'נושא שכולו תשובה אחת (הפעלת יום הולדת: הנוסח של גלעד) מתויג באותה תשובה — קוראים לכלי לפני שעונים, גם כששאלו רק על מחיר.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'tag': {'type': 'string', 'description': 'שם התגית, בדיוק כפי שהנושא כותב אותו'},
                'details': {'type': 'string', 'description': 'הפרטים שנאספו בשיחה, בשורה אחת (למשל: שם, בן או בת, אזור)'},
            },
            'required': ['tag'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'join_waiting_list',
        'description': (
            'רשימת המתנה לשיעור מסוים שמלא (חוג, יום ושעה). קוראים רק כשהשיעור שהלקוח רוצה חזר מלא (lessons[].is_full=true), '
            'אחרי שהוצע לו מועד אחר שחזר מהכלי והוא רוצה דווקא את השיעור הזה — או שאין מועד אחר שמתאים. '
            'הכלי בודק בעצמו את התפוסה: כשיש מקום הוא מחזיר has_room=true ולא מוסיף — אז מציעים להירשם. '
            'כשהשיעור מלא חוזר would_add=true (או already=true כשהלקוח כבר ברשימה): רק מסמן — לא שולח דבר. '
            'אחרי הסימון אומרים בקצרה שהלקוח ברשימת ההמתנה לשיעור הזה ושנעדכן כשיתפנה מקום. '
            'לא מבטיחים מקום, לא מועד ולא מקום בתור. lesson_id הוא ה-id של המועד (lessons[].id) מתוך find_courses או course_card.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'lesson_id': {'type': 'string', 'description': 'ה-id של המועד המלא (lessons[].id), לא של החוג'},
                'child': {'type': 'string', 'description': 'מה שהלקוח אמר על הילד, בקצרה: גיל, כיתה, שם אם נכתב'},
            },
            'required': ['lesson_id'],
            'additionalProperties': False,
        },
    },
]
TOOL_NAMES = {tool['name'] for tool in TOOLS}


class ToolContext:
    """What a tool run knows: the contact (if any), the knowledge in play, and the marks the tools leave."""

    def __init__(self, contact: Contact | None = None, *, now=None, items=None):
        self.contact = contact
        self.now = now
        self.items = list(items or [])
        self.request_human_reason: str | None = None
        self.knowledge_used: list = []
        # The tags a topic asked for ([{tag, details}]) — marks only, like request_human.
        self.tags: list = []
        # Every call of this run, with what came back ([{name, input, result}]): what the bot's
        # self-checks hold an answer against (apps/wahub/evals/checks.py).
        self.calls: list = []
        # A conversation that exists only in memory (shadow.scripted_context) may be given the card of a customer who is nobody's.
        self.scripted_customer: dict | None = None
        # The waiting lists the bot would put the customer on ([{lesson_id, child, label}]) — marks only, like
        # request_human: a row is written where the answer really leaves (waiting_list.apply_bot_marks).
        self.waiting_list: list = []

    def used(self, item: KnowledgeItem) -> None:
        if item.id not in self.knowledge_used:
            self.knowledge_used.append(item.id)


# --- resolving names -----------------------------------------------------------------------------

def _norm(text) -> str:
    return re.sub(r'[\s\-"״\'׳]', '', str(text or '').replace('תקוה', 'תקווה')).casefold()


def _alias_targets(text: str, ctx: ToolContext) -> list[str]:
    """The names a nickname from the knowledge points at, when the text contains the nickname."""
    found = []
    wanted = _norm(text)
    for item in ctx.items:
        if item.kind != KIND_ALIAS:
            continue
        data = item.data or {}
        for nick in re.split(r'\s*/\s*', str(data.get('what_customer_writes') or '')):
            if nick and _norm(nick) and _norm(nick) in wanted:
                found.append(str(data.get('means') or ''))
                ctx.used(item)
    return found


def resolve_branches(ctx: ToolContext, *, city: str = '', branch: str = ''):
    """
    The active branches a city or a branch name points at. The branch wins
    over the city. A name is matched on the branch's own name, its city, the
    conversation nicknames (analysis.BRANCH_ALIASES) and the knowledge aliases.
    """
    from apps.core.models import Branch

    rows = list(Branch.objects.filter(is_active=True).select_related('city').order_by('name'))
    if branch:
        wanted = _norm(branch)
        direct = [row for row in rows if wanted and (wanted in _norm(row.name) or _norm(row.name) in wanted)]
        if direct:
            return direct
        for label, _city, pattern in analysis.BRANCH_ALIASES:
            if pattern.search(branch):
                named = [row for row in rows if pattern.search(row.name or '')]
                if named:
                    return named
                in_city = [row for row in rows if _same_city(row.city.name if row.city_id else '', _city)]
                if in_city:
                    return in_city
        for target in _alias_targets(branch, ctx):
            hit = [row for row in rows if any(_norm(part) in _norm(row.name) for part in re.findall(r'[א-ת]{3,}', target))]
            if hit:
                return hit
    if city:
        in_city = [row for row in rows if _same_city(row.city.name if row.city_id else '', city)]
        if in_city:
            return in_city
        for label, alias_city, pattern in analysis.BRANCH_ALIASES:
            if pattern.search(city):
                hit = [row for row in rows if _same_city(row.city.name if row.city_id else '', alias_city) or pattern.search(row.name or '')]
                if hit:
                    return hit
    return []


def _course_type_matches(course, wanted: str, ctx: ToolContext) -> bool:
    if not wanted:
        return True
    names = [course.name or '', course.course_type.name if course.course_type_id else '']
    words = {_norm(wanted)} | {_norm(target) for target in _alias_targets(wanted, ctx)}
    words |= {_norm(name) for name, pattern in analysis.COURSE_WORDS if pattern.search(wanted)}
    text = _norm(' '.join(names))
    return any(word and (word in text or text in word) for word in words)


def office_phone(ctx=None) -> str:
    """
    The one phone of every branch: the office's (the owner, 13.10.2026: "טלפון של כל
    הסניפים — המשרד"). Read from the office's own line in the knowledge when the
    conversation has it, so that a number changed on the screen is the number said.
    """
    from apps.wahub.knowledge_seed import OFFICE_PHONE
    from apps.wahub.models import KIND_CONTACT

    for item in getattr(ctx, 'items', None) or ():
        data = getattr(item, 'data', None) or {}
        if getattr(item, 'kind', '') == KIND_CONTACT and data.get('name') == 'המשרד' and str(data.get('phone') or '').strip():
            return str(data['phone']).strip()
    return OFFICE_PHONE


def _branch_payload(branch, ctx=None) -> dict:
    missing = []
    if not branch.is_external:
        for name, value in (('address', branch.address), ('directions', branch.arrival_directions)):
            if not (value or '').strip():
                missing.append(name)
    return {
        'id': str(branch.id),
        'name': branch.name,
        'city': branch.city.name if branch.city_id else '',
        'is_external': branch.is_external,
        'external_link': branch.external_link or '',
        'address': branch.address or '',
        'directions': branch.arrival_directions or '',
        # The office's, whichever the branch: the number on a branch's own card is not said, and a branch has no manager.
        'phone': office_phone(ctx),
        'missing': missing,
        'note': EXTERNAL_NOTE if branch.is_external else '',
    }


def branch_wifi(branch) -> dict | None:
    """
    The wireless network of a branch of ours as its card holds it — the name
    and the password, and nothing else of the card (the owner, 12.10.2026: "take
    the wifi code and the wifi name too, when the branches' are asked for").
    None when neither is on the card. The door codes, the bluetooth codes and
    what the branch costs stay where they are: no tool reads them.
    """
    name, code = (branch.wifi_name or '').strip(), (branch.wifi_code or '').strip()
    return {'name': name, 'code': code} if (name or code) else None


def letter_for_letter(calls: list) -> list:
    """
    What the tools of a run returned that has to reach the customer exactly as
    it is written — today, a network's name and its password. The style rules
    (shadow.polish) and the Hebrew-only check pass over these.
    """
    found = []
    for call in calls or []:
        result = call.get('result') if isinstance(call.get('result'), dict) else {}
        for row in result.get('branches') or []:
            wifi = row.get('wifi') if isinstance(row, dict) else None
            if isinstance(wifi, dict):
                found += [str(wifi.get(key) or '') for key in ('name', 'code')]
    return [value for value in dict.fromkeys(found) if value.strip()]


WIFI_NOTE = (
    'wifi — שם הרשת והסיסמה של הסניף כפי שהם במערכת: נמסרים רק כששואלים עליהם, אות באות. '
    'null או סיסמה ריקה = לא רשום במערכת; אז בודקים מול המשרד ומסמנים לנציג. קודי כניסה לא נמסרים.'
)


# --- the tools -----------------------------------------------------------------------------------

def _office_phone(ctx: ToolContext) -> str:
    """The office's phone as the knowledge holds it (the contact item of the office)."""
    for item in ctx.items:
        phone = (item.data or {}).get('phone')
        if item.kind == KIND_CONTACT and phone and 'משרד' in (item.title or ''):
            ctx.used(item)
            return str(phone)
    return ''


def _field_of(course) -> str:
    """The field a class belongs to, as a parent would name it; a class with no field is its own."""
    return (course.course_type.name if course.course_type_id else '') or course.name


def _fields(courses) -> list:
    return sorted({_field_of(course) for course in courses})


def _cities() -> list:
    """The cities the business has a branch in — ours or one run by a municipality."""
    from apps.core.models import Branch

    rows = Branch.objects.filter(is_active=True).select_related('city')
    return sorted({row.city.name for row in rows if row.city_id and row.city.name})


def _branch_brief(branch) -> dict:
    """A branch while the conversation is still being narrowed: who it is, and nothing to recite."""
    return {
        'id': str(branch.id),
        'name': branch.name,
        'city': branch.city.name if branch.city_id else '',
        'is_external': branch.is_external,
        'note': EXTERNAL_NOTE if branch.is_external else '',
    }


def _our_branches() -> list:
    """Every active branch of ours (not one run by a municipality), as `resolve_branches` returns branches."""
    from apps.core.models import Branch

    return list(Branch.objects.filter(is_active=True, is_external=False).select_related('city').order_by('name'))


def _elsewhere(ctx: ToolContext, *, skip: set, codes: set, course_type: str) -> list:
    """
    The other branches of ours that hold a class of this field open to a child of this
    age, the way the form shows it: [{branch, city, classes}], by city, at most
    MAX_SCAN_BRANCHES + 2. What they hold is named, never spelled out: details come
    from a search of that branch.
    """
    from django.db.models import Q

    from apps.customers.widget_views import _widget_catalog_courses

    others = [row for row in _our_branches() if row.id not in skip]
    if not others:
        return []
    found = {}
    rows = course_tools.with_lessons(
        _widget_catalog_courses()
        .filter(branch_id__in=[row.id for row in others])
        .filter(Q(course_type__is_active=True) | Q(course_type__isnull=True))
    ).order_by('branch__name', 'name')
    for course in rows:
        if not trial_offer.selections(course) or not _course_type_matches(course, course_type, ctx):
            continue
        offer = course_tools.age_offer(course, codes)
        if offer is None:
            continue
        names = found.setdefault(course.branch_id, (course.branch, []))[1]
        tracks = [row.name for row in course.lesson_bundles.all() if str(row.id) in offer['tracks'] and row.name]
        names.append(f'{course.name} — {tracks[0]}' if tracks and not offer['days'] else course.name)
    listed = sorted(found.values(), key=lambda pair: ((pair[0].city.name if pair[0].city_id else ''), pair[0].name))
    return [
        {'branch': branch.name, 'city': branch.city.name if branch.city_id else '', 'classes': list(dict.fromkeys(names))[:4]}
        for branch, names in listed[:MAX_SCAN_BRANCHES + 2]
    ]


def _ask(need: str, branches: list, *, hint: str = '', ask: str = '', **choice) -> dict:
    """The answer of `find_courses` while something is still missing: the question, what to choose from, no class."""
    return {
        'branches': branches,
        'need': need,
        'ask': ask or ASK[need],
        **choice,
        'courses': [],
        'more_courses': [],
        'hint': f'{ASK_FIRST} {hint}'.strip(),
    }


def find_courses(ctx: ToolContext, *, city: str = '', branch: str = '', age=None, grade: str = '', course_type: str = '',
                 show_all: bool = False) -> dict:
    """
    The classes of a branch for a field and an age — once all three are known.
    Until then: `need` (city → branch → course_type → age → grade) and the
    question to ask, with what there is to choose from and no class. A city
    with one branch of ours needs no branch question. The field is never
    assumed: where one field is all there is, it is offered by name, as a
    question. `show_all` gives everything the filters leave, as before.
    """
    from django.db.models import Q

    from apps.customers.widget_views import _widget_catalog_courses

    registration_fee = getattr(settings, 'REGISTRATION_FEE_ILS', None)
    try:
        age_value = float(age) if age not in (None, '') else None
    except (TypeError, ValueError):
        age_value = None
    # The child's place on the age-group scale; a course's min/max are places on it, not years.
    grade_codes = age_groups.codes_for_grade(grade)
    age_codes = grade_codes or (age_groups.codes_for_age(age_value) if age_value is not None else set())
    by_age = bool(grade_codes) or age_value is not None

    branches = resolve_branches(ctx, city=city, branch=branch)
    scanned = False
    if not branches and not (city or branch) and course_type.strip() and age_codes and not show_all:
        branches, scanned = _our_branches(), True
    if not branches:
        cities = _cities()
        if not (city or branch):
            return {**_ask(NEED_CITY, [], cities=cities), 'registration_fee': registration_fee}
        hint = f'אין לנו סניף ב{city}.' if city and not branch else 'לא נמצא סניף כזה במערכת.'
        return {'branches': [], 'need': '', 'courses': [], 'hint': hint, 'cities': cities, 'registration_fee': registration_fee}

    internal = [row for row in branches if not row.is_external]
    everything = list(
        course_tools.with_lessons(
            _widget_catalog_courses()
            .filter(branch_id__in=[row.id for row in internal])
            .filter(Q(course_type__is_active=True) | Q(course_type__isnull=True))
        ).order_by('branch__name', 'course_type__name', 'name')
    ) if internal else []
    # A class with nothing to choose on the form — no lesson, or every day required and no track — is not on the form.
    everything = [course for course in everything if trial_offer.selections(course)]
    # Open to the child the way the form opens it: the class's own days, or a track or a price of a day of his age.
    for_age = [course for course in everything if age_codes and course_tools.age_offer(course, age_codes)] if by_age else everything
    courses = [course for course in for_age if _course_type_matches(course, course_type, ctx)]

    hints = []
    if scanned:
        holding = [row for row in internal if any(course.branch_id == row.id for course in courses)]
        if not holding:
            return {'branches': [], 'need': '', 'courses': [], 'more_courses': [], 'scanned_all': True, 'cities': _cities(),
                    'hint': SCAN_NOWHERE, 'registration_fee': registration_fee}
        if len(holding) > MAX_SCAN_BRANCHES:
            cities = sorted({row.city.name for row in holding if row.city_id and row.city.name})
            return {**_ask(NEED_CITY, [], cities=cities, hint=SCAN_MANY), 'scanned_all': True, 'registration_fee': registration_fee}
        if len(holding) > 1:
            rows = [{**_branch_brief(row), 'course_types': _fields(course for course in courses if course.branch_id == row.id)} for row in holding]
            return {**_ask(NEED_BRANCH, rows, hint=SCAN_FEW), 'scanned_all': True}
        branches = internal = holding
        hints.append(SCAN_ONE)
    if courses and not show_all:
        # Where: several branches of ours still hold a class that fits — the customer chooses one.
        holding = {course.branch_id for course in courses}
        if len(holding) > 1:
            rows = []
            for row in branches:
                brief = _branch_brief(row)
                if not row.is_external:
                    brief['course_types'] = _fields(course for course in courses if course.branch_id == row.id)
                rows.append(brief)
            return _ask(NEED_BRANCH, rows)
        if len(internal) > 1:
            hints.append('רק בסניף הזה יש חוג שמתאים למה שנמסר.')
        chosen = [row for row in branches if row.is_external or row.id in holding]
        # What: the field comes before any class, and it is the customer's to say. One field only is offered by name.
        fields = _fields(courses)
        if not course_type.strip():
            only = f'יש כאן {fields[0]} — זה התחום שמחפשים?' if len(fields) == 1 else ''
            return _ask(NEED_FIELD, [_branch_brief(row) for row in chosen], course_types=fields, ask=only)
        # Who for: the child's age or grade comes before any class.
        if not by_age:
            return _ask(NEED_AGE, [_branch_brief(row) for row in chosen])
        # An age on the line between two groups (six: גן חובה or כיתה א) is settled by the grade.
        groups = sorted({(course.min_age or 0, age_groups.range_label(course.min_age, course.max_age)) for course in courses})
        labels = [label for _place, label in groups if label]
        if len(age_codes) > 1 and len(labels) > 1:
            return _ask(NEED_GRADE, [_branch_brief(row) for row in chosen], between=labels)
        branches = chosen

    shown, rest = courses[:MAX_COURSES], courses[MAX_COURSES:]
    seats = course_tools.Seats(shown, now=ctx.now)
    result_courses = [course_tools.course_payload(course, seats, codes=age_codes if by_age else None) for course in shown]

    result = {
        'branches': [_branch_payload(row, ctx) for row in branches],
        'need': '',
        'courses': result_courses,
        'more_courses': [course_tools.brief_course(course) for course in rest],
        'courses_total': len(courses),
        # Over every class found, not only the ones spelled out: one class must never be read as the branch.
        'trial_summary': course_tools.trial_summary(courses),
        # The general fee; each class carries the fee it will actually charge (courses[].registration_fee).
        'registration_fee': registration_fee,
    }
    if scanned:
        result['scanned_all'] = True
    if result_courses:
        result['trial_dates_rule'] = course_tools.TRIAL_DATES_RULE
    if internal and not result_courses:
        hints.append('בסניפים האלה אין חוג שמתאים לסינון (גיל/תחום). אפשר להציע תחום אחר מאותו סניף, או להפנות למשרד.')
        # What the branch does have for this child, so that another field is offered from the system and not from memory.
        result['course_types'] = _fields(for_age)
        # And where else this very field is held for this very age: the customer is not asked for another city blindly.
        if course_type.strip() and age_codes and not show_all:
            others = _elsewhere(ctx, skip={row.id for row in internal}, codes=age_codes, course_type=course_type)
            if others:
                result['elsewhere'] = others
                hints.append(ELSEWHERE)
    if by_age and not age_codes:
        hints.append('אין קבוצת גיל לגיל הזה: החוגים מגיל 3.')
    if grade_codes and age_value is not None and not (age_groups.codes_for_age(age_value) & grade_codes):
        # "לגיל 14" and then "הוא בכיתה ו": the grade is the later and the more exact word, and the customer is told which was used.
        hints.append('הגיל והכיתה שנמסרו לא מתאימים זה לזה; החיפוש נעשה לפי הכיתה. לומר ללקוח במילה לפי מה ענינו, כדי שיוכל לתקן.')
    if len({course['ages'] for course in result_courses}) > 1 and len(age_codes) > 1:
        hints.append('הגיל יושב על הגבול בין שתי קבוצות גיל, ולכן חזרו חוגים משתיהן — לשאול באיזו כיתה או גן הילד לפני שמציעים חוג.')
    if rest:
        hints.append(
            f'נמצאו {len(courses)} חוגים; {len(shown)} מפורטים והשאר ב-more_courses בקיצור. '
            'לצמצם לפי תחום וגיל או כיתה, או לקרוא ל-course_card לחוג מסוים.'
        )
    result['hint'] = ' '.join(hints)
    return result


def branch_info(ctx: ToolContext, *, branch: str = '') -> dict:
    rows = resolve_branches(ctx, branch=branch) or resolve_branches(ctx, city=branch)
    if not rows:
        return {'branches': [], 'hint': 'לא נמצא סניף כזה במערכת.'}
    office_phone = _office_phone(ctx)
    branches = []
    for row in rows:
        payload = {**_branch_payload(row, ctx), **course_tools.branch_extras(row)}
        if not row.is_external:
            # Only here, and only of a branch of ours: a password does not travel with every search for a class.
            payload['wifi'] = branch_wifi(row)
        branches.append(payload)
    return {
        'branches': branches,
        'office_phone': office_phone,
        'note': 'class_days הם הימים והשעות שבהם יש חוגים בסניף, לא שעות קבלה. שעות המשרד — בכלי office_hours_now. ' + WIFI_NOTE,
    }


HOLIDAYS_ASK = 'באיזה סניף מדובר?'
HOLIDAYS_ASK_FIRST = 'לוח החופשות הוא של סניף מסוים: קודם שואלים את הלקוח את השאלה שב-ask, ורק אותה. לא מפרטים תאריכים.'
HOLIDAYS_SENT = (
    'אחרי התשובה המערכת שולחת ללקוח את לוח החופשות של הסניף — תמונה עם כל התאריכים. '
    'בתשובה: משפט קצר אחד שהלוח נשלח (אפשר לצטט את say). לא מפרטים תאריכים ולא מוסיפים קישור.'
)
HOLIDAYS_OFFICE = (
    'אין לוח חופשות לשלוח לסניף הזה: אומרים את הנוסח missing_info (שבודקים מול המשרד ושיחזרו עם תשובה) '
    'ומסמנים לנציג (request_human). לא מפרטים תאריכים ולא כותבים שנשלח לוח.'
)


def _registered_branches(ctx: ToolContext) -> list:
    """The branches the writer's registered children go to, as their card names them — what a registered parent means by "the branch"."""
    card = customer_card(ctx) if (ctx.contact is not None or ctx.scripted_customer) else {}
    names = [
        str(row.get('branch') or '').strip()
        for child in card.get('children') or [] if isinstance(child, dict)
        for row in child.get('classes') or [] if isinstance(row, dict)
    ]
    return list(dict.fromkeys(name for name in names if name))


def holiday_dates(ctx: ToolContext, *, branch: str = '', city: str = '') -> dict:
    """
    Which holidays board is sent for the branch asked about, and whether there
    is one to send (holiday_flows.py). The branch is settled as every tool
    settles it (resolve_branches); with none named, a registered parent's is
    the branch of the child's class. Several branches that are not on one
    board are a question — which branch — and never a guess.

    Reads only: the board of a branch is a constant, and whether ManyChat has
    its automation is what the cron last kept. `send_flow` in the answer is
    what the paths that really send an answer look for (shadow.flows_to_send).
    """
    rows = resolve_branches(ctx, city=city, branch=branch)
    if not rows and (branch or city):
        # A city written where the branch goes, or the other way round — as `branch_info` reads it.
        word = branch or city
        rows = resolve_branches(ctx, city=word) or resolve_branches(ctx, branch=word)
    if not rows and not (branch or city):
        for name in _registered_branches(ctx):
            rows += [row for row in resolve_branches(ctx, branch=name) if row not in rows]
    if not rows:
        result = {'found': False, 'need': '', 'branches': [], 'cities': _cities()}
        if branch or city:
            return {**result, 'hint': 'לא נמצא סניף כזה במערכת.'}
        return {**result, 'need': NEED_BRANCH, 'ask': HOLIDAYS_ASK, 'hint': HOLIDAYS_ASK_FIRST}

    listed = [{'name': row.name, 'city': row.city.name if row.city_id else ''} for row in rows]
    boards = [holiday_flows.board_of(row) for row in rows]
    flows = {board.flow if board else '' for board in boards}
    if len(flows) > 1:
        # A city whose branches are on different boards (or one of them on none): the customer says which.
        return {'found': False, 'need': NEED_BRANCH, 'ask': HOLIDAYS_ASK, 'branches': listed, 'hint': HOLIDAYS_ASK_FIRST}
    board = boards[0]
    if board is None:
        return {'found': False, 'need': '', 'branches': listed, 'flow_name': '', 'why': 'הסניף אינו באף לוח חופשות', 'note': HOLIDAYS_OFFICE}
    flow = holiday_flows.flow_of(board)
    if flow is None:
        return {
            'found': False, 'need': '', 'branches': listed, 'flow_name': board.flow,
            'why': holiday_flows.why_missing(board), 'note': HOLIDAYS_OFFICE,
        }
    return {
        'found': True, 'need': '', 'branches': listed, 'flow_name': board.flow, 'send_flow': flow,
        'say': holiday_flows.SAY, 'note': HOLIDAYS_SENT,
    }


def office_hours_now(ctx: ToolContext) -> dict:
    hours = knowledge.office_hours_now(ctx.now)
    hours.pop('special', None) if hours.get('special') is None else None
    return hours


def _classes_of(child_ids: list) -> dict:
    """
    Where each child is registered now: the class, the branch, the day and the
    hour — what a parent knows about their own child, so that the bot does not
    ask it again. Nothing of what the family pays.
    """
    from apps.enrollments.models import LessonEnrollment

    by_child: dict = {}
    rows = LessonEnrollment.objects.filter(child_id__in=child_ids, status='active').select_related(
        'lesson', 'lesson__instructor', 'lesson__course', 'lesson__course__course_type', 'lesson__course__branch', 'lesson__course__branch__city',
    ).order_by('lesson__day_of_week', 'lesson__start_time')
    for row in rows:
        lesson, course = row.lesson, row.lesson.course
        entry = {
            'course_id': str(course.id),
            'course': course.name,
            'course_type': course.course_type.name if course.course_type_id else '',
            'branch': course.branch.name,
            'city': course.branch.city.name if course.branch.city_id else '',
            'day': DAY_NAMES[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else '',
            'start': str(lesson.start_time)[:5],
            'end': str(lesson.end_time)[:5],
            'instructor': lesson.instructor.full_name if lesson.instructor_id else '',
        }
        if row.trial_lesson_date:
            entry['trial_on'] = row.trial_lesson_date.strftime('%d.%m.%Y')
        by_child.setdefault(str(row.child_id), []).append(entry)
    return by_child


def customer_card(ctx: ToolContext) -> dict:
    contact = ctx.contact
    if contact is None:
        return ctx.scripted_customer or {'known': False, 'note': 'אין איש קשר — שאלת ניסיון בלי לקוח.'}
    from apps.wahub.serializers import UNCHECKED_LABEL

    # One row a child, the duplicate cards of one child as one; the classes are the bot's own list, as they were.
    children = matching.children_of(contact, classes=False)
    classes = _classes_of([child['id'] for child in children]) if children else {}
    for child in children:
        child['classes'] = classes.get(child['id'], [])
    return {
        'known': True,
        'name': contact.name,
        'outcome': contact.kogo_outcome,
        'outcome_label': matching.outcome_label(contact, UNCHECKED_LABEL),
        'detail': contact.kogo_detail,
        'children': children,
        # The trial lesson the matching found for this phone: the day, the class, whether the child came.
        'trial': dict(contact.kogo_trial or {}),
        'what_we_know': {
            'topic': contact.known_topic,
            'course_type': contact.known_course_type,
            'city': contact.known_city,
            'branch': contact.known_branch_name,
            'child_age': contact.known_child_age,
            'summary': contact.known_summary,
        },
        'handled_by': contact.handled_by,
        'needs_human': contact.needs_human,
    }


def request_human(ctx: ToolContext, *, reason: str = '') -> dict:
    ctx.request_human_reason = ' '.join(str(reason or 'הלקוח ביקש נציג').split())[:200]
    return {'ok': True, 'note': 'סומן (לא נשלח דבר). בתשובה ללקוח: נציג יחזור אליו; מחוץ לשעות — הודעת "סגור".'}


def _topic_tags(ctx: ToolContext) -> list:
    """The tags a topic of the knowledge ends with; the section's own tags when no knowledge is in play."""
    from apps.wahub.models import KIND_TOPIC, Tag

    named = [str((item.data or {}).get('tag') or '').strip() for item in ctx.items if item.kind == KIND_TOPIC]
    named = list(dict.fromkeys(tag for tag in named if tag))
    return named or list(Tag.objects.order_by('name').values_list('name', flat=True))


def tag_conversation(ctx: ToolContext, *, tag: str = '', details: str = '') -> dict:
    """
    The owner's decisions 6–7 (10.10.2026): both birthday paths end tagged, with
    the details kept. Here it is a mark on the draft, as `request_human` is —
    the shadow writes nothing on a contact. The tag is kept on the contact where
    the answer really leaves (birthday.apply_tag_marks), and "ברכת יום הולדת"
    then puts the customer in the office's box of who waits for the greeting.
    """
    wanted = ' '.join(str(tag or '').split())
    allowed = _topic_tags(ctx)
    match = next((name for name in allowed if _norm(name) == _norm(wanted)), None)
    if match is None:
        return {'error': f'אין תגית כזאת. התגיות שנושא קובע: {", ".join(allowed) or "אין"}'}
    mark = {'tag': match, 'details': ' '.join(str(details or '').split())[:300]}
    if mark not in ctx.tags:
        ctx.tags.append(mark)
    return {'ok': True, 'tag': match, 'note': 'סומן (לא נשלח דבר ולא שונה דבר אצל הלקוח).'}


def _flag(value):
    """True, False, or None when the caller did not say."""
    return value if isinstance(value, bool) else None


def run_tool(name: str, arguments: dict, ctx: ToolContext) -> dict:
    """One tool call. A broken tool answers with an error line, never raises. The call is kept on the context."""
    arguments = arguments if isinstance(arguments, dict) else {}
    result = _run(name, arguments, ctx)
    ctx.calls.append({'name': name, 'input': arguments, 'result': result})
    return result


def _run(name: str, arguments: dict, ctx: ToolContext) -> dict:
    try:
        if name == 'find_courses':
            return find_courses(
                ctx, city=str(arguments.get('city') or ''), branch=str(arguments.get('branch') or ''),
                age=arguments.get('age'), grade=str(arguments.get('grade') or ''),
                course_type=str(arguments.get('course_type') or ''), show_all=arguments.get('show_all') is True,
            )
        if name == 'course_card':
            return course_tools.course_card(
                ctx, course_id=str(arguments.get('course_id') or ''), name=str(arguments.get('name') or ''),
                branch=str(arguments.get('branch') or ''), city=str(arguments.get('city') or ''),
            )
        if name == 'quote_registration':
            return course_tools.quote_registration(
                ctx, course_id=str(arguments.get('course_id') or ''), lesson_id=str(arguments.get('lesson_id') or ''),
                track_id=str(arguments.get('track_id') or ''), start_date=str(arguments.get('start_date') or ''),
                child_age=arguments.get('child_age'), grade=str(arguments.get('grade') or ''),
                siblings_registered=_flag(arguments.get('siblings_registered')),
                took_paid_trial=_flag(arguments.get('took_paid_trial')),
                another_class_same_child=_flag(arguments.get('another_class_same_child')),
            )
        if name == 'instructor_info':
            return course_tools.instructor_info(
                ctx, name=str(arguments.get('name') or ''), branch=str(arguments.get('branch') or ''),
            )
        if name == 'branch_info':
            return branch_info(ctx, branch=str(arguments.get('branch') or ''))
        if name == 'holiday_dates':
            return holiday_dates(ctx, branch=str(arguments.get('branch') or ''), city=str(arguments.get('city') or ''))
        if name == 'office_hours_now':
            return office_hours_now(ctx)
        if name == 'customer_card':
            return customer_card(ctx)
        if name == 'request_human':
            return request_human(ctx, reason=str(arguments.get('reason') or ''))
        if name == 'tag_conversation':
            return tag_conversation(ctx, tag=str(arguments.get('tag') or ''), details=str(arguments.get('details') or ''))
        if name == 'join_waiting_list':
            from apps.wahub import waiting_list

            return waiting_list.bot_would_join(
                ctx, lesson_id=str(arguments.get('lesson_id') or ''), child=str(arguments.get('child') or ''),
            )
    except Exception as exc:  # the answer must still come, and the fault is not lost
        logger.exception('wahub tool %s failed', name)
        return {'error': f'הכלי נכשל: {type(exc).__name__}'}
    return {'error': f'כלי לא מוכר: {name}'}


# The tools whose answer is followed by an automation. Which automation is the tool's own to say (a constant of
# holiday_flows.py) — never the model's, which chooses only the branch it asks about.
SENDING_TOOLS = ('holiday_dates',)


def used(name: str, arguments: dict, result: dict) -> dict:
    """
    One line of an answer's `tools_used`: the call, the line the screen shows
    under "אילו כלים נקראו" — and, for the tool whose answer is followed by an
    automation, which one (`send_flow`: {name, ns, label}). That mark is how
    the automation reaches the path that sends the answer, with no table to
    change (shadow.flows_to_send).
    """
    line = {'name': name, 'input': arguments, 'summary': summarize_result(name, result)}
    flow = result.get('send_flow') if name in SENDING_TOOLS and isinstance(result, dict) and result.get('found') else None
    if isinstance(flow, dict) and str(flow.get('ns') or '').strip():
        line['send_flow'] = {key: str(flow.get(key) or '') for key in ('name', 'ns', 'label')}
    return line


def _shekel_text(amount) -> str:
    """ "260.00" → ₪260, "187.50" → ₪187.50."""
    text = str(amount or '0')
    return '₪' + (text[:-3] if text.endswith('.00') else text)


def summarize_result(name: str, result: dict) -> str:
    """One Hebrew line about what a tool returned — what the screen shows under "אילו כלים נקראו"."""
    if 'error' in result:
        return result['error']
    if name == 'find_courses':
        branches = result.get('branches') or []
        need = result.get('need')
        if need:
            choice = result.get('course_types') or result.get('between') or (
                [row['name'] for row in branches if not row.get('is_external')] if need == NEED_BRANCH else []
            )
            return f'קודם שואלים {NEED_LABELS.get(need, need)}; חוגים לא הוחזרו' + (f' ({", ".join(choice[:8])})' if choice else '')
        if not branches:
            return result.get('hint') or 'לא נמצא סניף'
        external = [row['name'] for row in branches if row.get('is_external')]
        shown, total = len(result.get('courses') or []), result.get('courses_total') or 0
        text = f'{len(branches)} סניפים, {shown} חוגים'
        if total > shown:
            text += f' מפורטים מתוך {total}'
        if not shown and result.get('course_types'):
            # What the branch does have, said on the line: whoever reads it (the owner, the judge) sees what the bot could offer instead.
            text += f'; אין חוג שמתאים למה שנשאל. התחומים שיש שם: {", ".join(result["course_types"][:8])}'
        trial = result.get('trial_summary') or {}
        if trial.get('paid_courses'):
            text += f'; ניסיון בתשלום ב-{trial["paid_courses"]}, ללא עלות ב-{trial.get("free_courses", 0)}'
        if external:
            text += f'; חיצוניים: {", ".join(external)}'
        return text
    if name == 'course_card':
        if not result.get('found'):
            return result.get('hint') or 'החוג לא נמצא'
        if result.get('is_external'):
            return f'{result["course"]["name"]}: סניף חיצוני — לבדוק מול העירייה'
        course = result['course']
        trial = course['trial']
        dates = trial.get('dates') or []
        coming = f'ניסיון קרוב ב-{dates[0]["date"][:5]}' if dates else 'אין כרגע תאריך פנוי לניסיון'
        return (
            f'{course["name"]} ({course["branch"]}): {_shekel_text(course["price_monthly"])} לחודש, '
            f'דמי רישום {_shekel_text(course["registration_fee"])}, '
            f'{"ניסיון " + _shekel_text(trial["price"]) if trial["price"] else ("ניסיון בתשלום, בלי מחיר" if trial["is_paid"] else "ניסיון ללא עלות")}, '
            f'{len(course["lessons"])} מועדים, {coming}'
        )
    if name == 'quote_registration':
        if not result.get('can_quote'):
            return f'אין חישוב: {result.get("reason") or "חסר מידע"}'
        parts = ' '.join(
            ('− ' if row['amount'].startswith('-') else '+ ') + f'{row["label"].split(" — ")[0].split(" (")[0]} {_shekel_text(row["amount"].lstrip("-"))}'
            for row in result['first_charge']['breakdown']
        ).lstrip('+ ')
        return (
            f'הרשמה ב-{result["as_of"]} ל{result["course"]["name"]}: תשלום ראשון {_shekel_text(result["first_charge"]["amount"])}'
            f'{" (" + parts + ")" if parts else ""}, אחר כך {_shekel_text(result["monthly_after"]["amount"])} לחודש'
        )
    if name == 'instructor_info':
        rows = result.get('instructors') or []
        if not rows:
            return result.get('hint') or 'לא נמצא מדריך'
        names = ', '.join(f'{row["name"]} ({", ".join(row["course_types"]) or "בלי תחום"})' for row in rows[:3])
        return names + (f' ועוד {len(rows) - 3}' if len(rows) > 3 else '')
    if name == 'branch_info':
        rows = result.get('branches') or []

        def described(row: dict) -> str:
            if row['is_external']:
                return f'{row["name"]} (חיצוני)'
            wifi = row.get('wifi') or {}
            # Whether the network is on the card — never the password: this line is shown on the screen.
            if wifi.get('name') and wifi.get('code'):
                network = 'וויפי רשום'
            elif wifi.get('name') or wifi.get('code'):
                network = 'וויפי רשום חלקית'
            else:
                network = 'וויפי לא רשום'
            return f'{row["name"]} (שלנו; {network})'

        return ', '.join(described(row) for row in rows) or 'לא נמצא'
    if name == 'holiday_dates':
        where = ', '.join(row['name'] for row in result.get('branches') or [])
        flow = result.get('send_flow') or {}
        if result.get('found') and flow:
            # What will follow the answer — and that it follows only an answer that is itself sent.
            return (
                f'אחרי התשובה נשלחת האוטומציה {flow.get("name", "")} ({flow.get("label") or "לוח החופשות"}) ל{where} — '
                'רק כשהתשובה עצמה נשלחת ללקוח; בתשובה בצל, ב"נסה שאלה" ובבדיקות לא נשלח דבר'
            )
        if result.get('need'):
            return 'קודם שואלים באיזה סניף; לוח חופשות לא נשלח' + (f' ({where})' if where else '')
        if not result.get('branches'):
            return result.get('hint') or 'לא נמצא סניף'
        named = f'האוטומציה {result["flow_name"]}: ' if result.get('flow_name') else ''
        return f'{where}: {named}{result.get("why") or "אין לוח חופשות"} — לא נשלח דבר; בודקים מול המשרד'
    if name == 'office_hours_now':
        return 'המשרד פתוח' if result.get('open') else 'המשרד סגור'
    if name == 'customer_card':
        return result.get('outcome_label') or 'לא ידוע'
    if name == 'request_human':
        return 'סומן שצריך נציג'
    if name == 'tag_conversation':
        return f'סומנה התגית "{result.get("tag", "")}"'
    if name == 'join_waiting_list':
        label = (result.get('lesson') or {}).get('label', '')
        if result.get('has_room'):
            return f'יש מקום בשיעור — לא נוסף לרשימת המתנה ({label})'
        if result.get('already'):
            return f'כבר ברשימת ההמתנה: {label}'
        return f'סומן לרשימת ההמתנה: {label}' if result.get('would_add') else 'לא נוסף לרשימת ההמתנה'
    return ''
