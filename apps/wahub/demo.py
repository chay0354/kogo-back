"""
Invented conversations for looking at the "וואטסאפ ולידים" screens on a
developer's machine. Every name is made up and every phone is 050-555XXXX.

Nothing here may run against real data: `ensure_local_database` refuses any
database whose name does not start with `kogo_local`.

The contacts cover every box, every queue and every answer of the matching;
a few invented families and children are added so the matching has something
to find. Running it again replaces what it made before.
"""
from __future__ import annotations

import random
from datetime import time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from apps.wahub import analysis, inbound, matching, state
from apps.wahub.models import (
    EVENT_CREATED,
    EVENT_HANDLED_BY,
    EVENT_NEEDS_HUMAN,
    Contact,
    ContactEvent,
    Message,
    QuickReply,
    Tag,
)

LOCAL_ADMIN_EMAIL = 'local-admin@kogo.test'
DEMO_PHONE_PREFIX = '97250555'
DEMO_MARK = 'wahub-demo'   # in Family.notes: what this command made and may delete


def ensure_local_database() -> str:
    name = str(settings.DATABASES['default'].get('NAME') or '')
    if not name.startswith('kogo_local'):
        raise CommandError(
            f'הפקודה הזו רצה רק על DB מקומי ששמו מתחיל ב-kogo_local. ה-DB הנוכחי: "{name}". לא נעשה דבר.'
        )
    return name


def ensure_local_admin(password: str | None):
    """The local manager of the README, made when missing. Returns (user, created)."""
    from django.contrib.auth import get_user_model

    from apps.core.models import UserProfile

    User = get_user_model()
    user = User.objects.filter(username=LOCAL_ADMIN_EMAIL).first()
    created = user is None
    if created:
        user = User(
            username=LOCAL_ADMIN_EMAIL, email=LOCAL_ADMIN_EMAIL, first_name='מנהל', last_name='מקומי',
            is_active=True, is_staff=True,
        )
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
    UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
    # Read again: saving a user makes its profile, and the copy in hand still says "worker".
    return User.objects.select_related('profile').get(pk=user.pk), created


# --- the conversations ----------------------------------------------------------------
# talk: (who, text, minutes ago). who: c customer, b bot, o office, t a template the
# office sent, x an office message that failed.

HOUR, DAY = 60, 24 * 60
BOT_HELLO = 'היי! כאן דנה מקוגומלו 🙂 באיזה סניף ובאיזה גיל מדובר?'
BOT_PRICE = 'המחיר הוא 260 ₪ לחודש, ושיעור ניסיון ראשון ללא עלות. לקבוע לכם?'
BOT_LINK = 'מעולה! אפשר להירשם לשיעור ניסיון כאן: https://example.test/trial'


def S(name, phone, talk, **extra):
    return dict(name=name, phone=phone, talk=talk, **extra)


SCENARIOS = [
    # --- waiting for an answer ---
    S('רותם אברהמי', '0101', [('c', 'היי, אפשר פרטים על חוג קפוארה בראש העין? הבן שלי בן 5', 6)], tags=['ראש העין']),
    S('שירן דהן', '0102', [
        ('c', 'שלום, רוצה לרשום את הבת שלי לחוג מחול בכפר סבא', 40), ('b', BOT_HELLO, 39),
        ('c', 'היא בת 8. אבל אפשר לדבר עם נציג? יש לי כמה שאלות', 12),
    ], needs_human='ביקש נציג', tags=['כפר סבא']),
    S('מאיה פרידמן', '0103', [
        ('c', 'היי', 3 * HOUR), ('b', BOT_HELLO, 3 * HOUR - 1), ('c', 'מה המחיר של חוג היפ הופ בכפר סבא?', 45),
    ]),
    S('אורי בן דוד', '0104', [('c', 'שלום, יש מקום בחוג מחול לגיל 7 באם המושבות?', 3 * HOUR + 10)]),
    S('נטע שחר', '0105', [
        ('c', 'ראיתי את המודעה שלכם', 50), ('b', BOT_HELLO, 49),
        ('c', 'ראש העין, בן 6. רוצה להירשם לקפוארה, איך עושים את זה?', 25),
    ], source='ad', tags=['ראש העין', 'מודעה אוקטובר']),
    S('גל מזרחי', '0106', [('c', 'אפשר לקבוע שיעור ניסיון בקפוארה בשוהם ליום שלישי?', 20 * HOUR)]),

    # --- somebody asked for a person, or a person is answering ---
    S('יעל רוזן', '0107', [
        ('c', 'נרשמנו לשיעור ניסיון ואף אחד לא חזר אליי. אני מאוכזבת', 5 * HOUR),
        ('b', 'מצטערת לשמוע! אני מעבירה לנציג שיחזור אלייך.', 5 * HOUR - 1),
        ('c', 'תחזרו אליי בבקשה היום', 5 * HOUR - 3), ('b', 'העברתי את הבקשה לצוות.', 5 * HOUR - 4),
    ], needs_human='ביקש נציג', read=True),
    S('דנה קורן', '0108', [
        ('c', 'יש הנחה לשני אחים בחוג קפוארה?', 2 * HOUR), ('b', 'אבדוק עם הצוות ואחזור אלייך.', 2 * HOUR - 1),
        ('o', 'היי דנה, כאן מיכל מהמשרד. כן, יש הנחת אחים של 10%.', 90),
        ('c', 'מעולה! ואפשר שיהיו באותה קבוצה? הם בני 6 ו-8', 8),
    ], handled_by='human'),
    S('עידו שמואלי', '0109', [
        ('c', 'הילד שלי עם קשיי קשב, זה מתאים לו קפוארה?', 6 * HOUR), ('b', BOT_HELLO, 6 * HOUR - 1),
        ('o', 'היי עידו, בהחלט. המדריך שלנו מנוסה מאוד, ממליצה להתחיל בשיעור ניסיון.', 4 * HOUR),
    ], handled_by='human', followup=('answered', None, 'מחכים שיקבע ניסיון')),
    S('ליאת גבאי', '0110', [
        ('c', 'אפשר לשלם במזומן על חוג מחול?', 26 * HOUR), ('b', 'התשלום בכרטיס אשראי בהוראת קבע.', 26 * HOUR - 1),
    ], needs_human='רוצה לשלם במזומן', read=True),

    # --- the bot answered; leads in every queue ---
    S('תמר לנדאו', '0111', [
        ('c', 'אפשר שיעור ניסיון בקפוארה בראש העין? בת 6', DAY + 2 * HOUR), ('b', BOT_LINK, DAY + 2 * HOUR - 1),
    ], read=True, tags=['ראש העין']),
    S('אביב נחום', '0112', [
        ('c', 'מה המחיר של חוג קפוארה?', 3 * DAY), ('b', BOT_PRICE, 3 * DAY - 1),
    ], read=True, followup=('no_answer', None, 'התקשרתי פעמיים, לא ענה')),
    S('הילה סויסה', '0113', [
        ('c', 'מתעניינת בחוג מחול לבת 9 בכפר סבא', 15 * DAY + 30), ('b', BOT_PRICE, 15 * DAY + 29),
        ('c', 'תודה, אחזור אליכם בעוד שבועיים אחרי שנחזור מחול', 15 * DAY), ('b', 'בשמחה, נהיה כאן!', 15 * DAY - 1),
    ], read=True, tags=['כפר סבא', 'אחרי החגים']),
    S('רון אלמוג', '0114', [
        ('c', 'יש חוג היפ הופ לגיל 10 בפתח תקווה?', 6 * DAY), ('b', BOT_HELLO, 6 * DAY - 1),
    ], read=True, followup=('later', 0, 'ביקש שנחזור אליו היום')),
    S('מיכל ברק', '0115', [
        ('c', 'רוצה לרשום את הבן לקפוארה אבל רק אחרי החגים', 4 * DAY), ('b', 'בשמחה, נהיה כאן!', 4 * DAY - 1),
    ], read=True, followup=('later', 5, 'אחרי החגים'), tags=['אחרי החגים']),
    S('שי וקנין', '0116', [
        ('c', 'אפשר להעביר את הילד לקבוצה של יום רביעי בקפוארה?', 2 * DAY), ('b', 'אבדוק ואחזור אליך.', 2 * DAY - 1),
    ], read=True, followup=('waiting_us', None, 'לבדוק מקום ברביעי')),
    S('ענבל חזן', '0117', [
        ('c', 'יש חוג אקרובטיקה לגיל 5?', 5 * DAY), ('b', BOT_HELLO, 5 * DAY - 1), ('c', 'ראש העין', 5 * DAY - 5),
        ('b', BOT_LINK, 5 * DAY - 6),
    ], read=True, followup=('answered', None, '')),
    S('אלון טל', '0118', [
        ('c', 'יש לכם חוג קפוארה בחיפה?', 7 * DAY), ('b', 'כרגע אין לנו סניף בחיפה.', 7 * DAY - 1),
    ], read=True, followup=('not_relevant', None, 'גר בחיפה')),
    S('נועם פרץ', '0120', [
        ('c', 'כמה עולה חוג קפוארה בראש העין?', 2 * DAY + 5), ('b', BOT_PRICE, 2 * DAY + 4),
        ('c', 'וואו זה יקר לנו. נחשוב על זה', 2 * DAY), ('b', 'מובן לגמרי. אנחנו כאן לכל שאלה.', 2 * DAY - 1),
    ], read=True, tags=['ראש העין']),
    S('סיון אדלר', '0121', [
        ('c', 'רציתי לרשום לחוג מחול ביום שני ואמרו לי שאין מקום', 3 * DAY), ('b', 'אפשר להירשם לרשימת המתנה.', 3 * DAY - 1),
    ], read=True),
    S('בר יוסף', '0122', [
        ('c', 'אפשר פרטים על השכרת הסטודיו לערב אחד?', 4 * DAY), ('b', 'העברתי את הפנייה לצוות ההשכרות.', 4 * DAY - 1),
    ], read=True),
    S('טל רביבו', '0123', [
        ('c', 'מה המחיר ליום הולדת לגיל 7?', 5 * DAY + 3), ('b', 'ימי הולדת מתואמים מול המשרד.', 5 * DAY + 2),
    ], read=True),
    S('לירון חדד', '0124', [
        ('c', 'יש לכם חוג קפוארה בנתניה?', DAY + 5 * HOUR), ('b', 'כרגע אין לנו סניף בנתניה.', DAY + 5 * HOUR - 1),
    ], read=True),
    S('עדי מלכה', '0125', [
        ('c', 'הבן שלי בן 3, אפשר לרשום אותו לקפוארה?', 6 * DAY + 4), ('b', 'החוגים מגיל 4.', 6 * DAY + 3),
        ('c', 'אז הוא קטן מדי. חבל', 6 * DAY), ('b', 'נשמח לראות אתכם בשנה הבאה!', 6 * DAY - 1),
    ], read=True),
    S('אסף גולן', '0126', [
        ('c', 'כבר נרשמנו אתמול לחוג קפוארה ולא קיבלתי אישור', 9 * HOUR), ('b', 'אבדוק מול המשרד.', 9 * HOUR - 1),
    ], needs_human='אומר שנרשם ולא קיבל אישור'),
    S('אלה נגר', '0138', [
        ('c', 'היי, אפשר לקבל מידע נוסף על זה?', 2 * DAY + 3 * HOUR), ('b', BOT_HELLO, 2 * DAY + 3 * HOUR - 1),
    ], read=True, source='ad', tags=['מודעה אוקטובר']),
    S('איתמר רז', '0139', [
        ('t', 'חזרה ללידים - אוקטובר', 3 * DAY), ('c', 'כן, מעוניין בפרטים על חוג קפוארה', 3 * DAY - 40),
        ('b', BOT_HELLO, 3 * DAY - 41),
    ], read=True, source='broadcast_reply'),
    S('', '0140', [('c', 'שלום', 30), ('b', BOT_HELLO, 29)]),
    S('סיגל (התקשרה למשרד)', '0141', [], source='manual'),
    S('מעיין שטרן', '0142', [
        ('c', 'היי, רוצה לשמוע על חוג היפ הופ', 2 * DAY + 60), ('b', BOT_HELLO, 2 * DAY + 59),
        ('c', 'כפר סבא, בת 11', 2 * DAY + 55), ('b', BOT_PRICE, 2 * DAY + 54),
        ('c', 'ובאילו ימים?', 2 * DAY + 50), ('b', 'ימי שני וחמישי ב-17:00.', 2 * DAY + 49),
        ('c', 'יש גם קבוצת בנות בלבד?', 2 * DAY + 45), ('b', 'אבדוק עם הצוות.', 2 * DAY + 44),
        ('o', 'היי מעיין, כאן מיכל. הקבוצה מעורבת, אבל רוב המשתתפות בנות.', 2 * DAY + 10),
        ('c', 'אוקיי תודה! אז נבוא לשיעור ניסיון ביום חמישי', 2 * DAY + 5),
        ('c', 'צריך להביא משהו?', 2 * DAY + 4), ('o', 'מצוין, רשמתי אתכן. רק בגדים נוחים ומים. נתראה!', 2 * DAY),
    ], read=True, handled_by='human', tags=['כפר סבא'], followup=('answered', None, 'מגיעות לניסיון ביום חמישי')),
    S('יונתן הראל', '0143', [
        ('c', 'מתעניין בחוג קפוארה לבן 7 באם המושבות', 6 * DAY + 60), ('b', BOT_PRICE, 6 * DAY + 59),
        ('t', 'תזכורת - שיעור ניסיון', DAY),
    ], read=True),
    S('אופיר דיין', '0144', [
        ('c', 'אפשר לשנות את יום החוג?', 10 * HOUR), ('b', 'אבדוק ואחזור.', 10 * HOUR - 1),
        ('x', 'היי אופיר, אפשר לעבור ליום רביעי.', 9 * HOUR),
    ], read=True),
    S('ליה אפרתי', '0145', [('c', 'שלום, יש חוג מחול לבת 6 בראש העין?', 1)], fresh=True),

    # --- phones the registrations know ---
    S('אורית שגיא', '0127', [
        ('c', 'היי, אפשר לדעת אם יש חוג בחול המועד?', 8 * HOUR), ('b', 'בחול המועד אין חוגים.', 8 * HOUR - 1),
    ], read=True, system='customer_before'),
    S('יובל מרום', '0128', [
        ('c', 'קיבלתי הודעה שהחיוב לא עבר, מה עושים?', 5 * HOUR), ('b', 'אפשר לעדכן כרטיס בקישור שנשלח.', 5 * HOUR - 1),
    ], system='customer_problem', needs_human='בעיה בחיוב'),
    S('מור אשכנזי', '0129', [
        ('c', 'רוצה לרשום את הבת לקפוארה בראש העין', 9 * DAY), ('b', BOT_LINK, 9 * DAY - 1),
    ], read=True, system='registered_after', followup=('registered', None, ''), tags=['ראש העין']),
    S('קרן אוחיון', '0119', [
        ('c', 'אפשר פרטים על חוג מחול?', 12 * DAY), ('b', BOT_PRICE, 12 * DAY - 1), ('c', 'נרשמנו, תודה!', 10 * DAY),
        ('b', 'איזה כיף, נתראה בחוג!', 10 * DAY - 1),
    ], read=True, system='registered_after', followup=('registered', None, 'נרשמה דרך האתר')),
    S('חן ביטון', '0130', [
        ('c', 'נרשמנו לשיעור ניסיון, מה צריך להביא?', 7 * HOUR), ('b', 'בגדים נוחים ובקבוק מים.', 7 * HOUR - 1),
    ], read=True, system='trial_upcoming'),
    S('רעות עמר', '0131', [
        ('c', 'היה לנו שיעור ניסיון מעולה בקפוארה!', 4 * DAY), ('b', 'איזה כיף לשמוע! רוצים להירשם?', 4 * DAY - 1),
        ('c', 'נחשוב על זה ונעדכן', 4 * DAY - 10), ('b', 'בשמחה, אנחנו כאן.', 4 * DAY - 11),
    ], read=True, system='trial_attended'),
    S('דור סלע', '0132', [
        ('c', 'סליחה שלא הגענו לשיעור ניסיון, הילד היה חולה', 5 * DAY), ('b', 'רפואה שלמה! נקבע מועד חדש?', 5 * DAY - 1),
    ], read=True, system='trial_no_show', followup=('no_answer', None, '')),
    S('שני לביא', '0133', [
        ('c', 'מתי אפשר להירשם אחרי שיעור ניסיון?', 3 * DAY + HOUR), ('b', BOT_LINK, 3 * DAY + HOUR - 1),
    ], read=True, system='trial_unmarked'),
    S('ניר אזולאי', '0134', [
        ('c', 'ניסיתי להירשם לחוג קפוארה והתשלום לא עבר', DAY + HOUR), ('b', 'אפשר לנסות שוב עם כרטיס אחר.', DAY + HOUR - 1),
    ], read=True, system='signup_declined'),
    S('הדר מימון', '0135', [
        ('c', 'התחלתי להירשם לחוג מחול ולא סיימתי, אפשר עזרה?', 2 * DAY + 2 * HOUR), ('b', BOT_LINK, 2 * DAY + 2 * HOUR - 1),
    ], read=True, system='pending'),
    S('עומר שלום', '0136', [
        ('c', 'היינו אצלכם בשנה שעברה, יש חוג קפוארה גם השנה?', 3 * DAY + 4 * HOUR), ('b', BOT_HELLO, 3 * DAY + 4 * HOUR - 1),
    ], read=True, system='inactive'),
    S('ורד זכאי', '0137', [
        ('c', 'רוצים לחזור לחוג מחול אחרי הפסקה', 6 * DAY + 2 * HOUR), ('b', 'בשמחה! באיזה סניף?', 6 * DAY + 2 * HOUR - 1),
    ], read=True, system='inactive', followup=('later', 2, 'לחזור אליה ביום ראשון')),
]

TAGS = [
    ('ראש העין', '#2563eb'), ('כפר סבא', '#7c3aed'), ('אחרי החגים', '#d97706'), ('מודעה אוקטובר', '#059669'),
]
QUICK_REPLIES = [
    ('פתיחה', 'היי {{first_name}}, כאן המשרד של קוגומלו 🙂 איך אפשר לעזור?'),
    ('מחיר', 'המחיר הוא 260 ₪ לחודש, ושיעור ניסיון ראשון ללא עלות.'),
    ('קישור לניסיון', 'אפשר להירשם לשיעור ניסיון כאן: https://example.test/trial'),
    ('אחרי ניסיון', 'היי {{first_name}}, איך היה שיעור הניסיון? נשמח לראות אתכם שוב 🙂'),
]

SIMULATED_NAMES = ['נועה שפירא', 'איתי בן חיים', 'יעלי מור', 'אדם קפלן', 'רוני אסולין', 'שקד יעקב', 'עלמה רוט', 'ארי זילבר']
SIMULATED_TEXTS = [
    'היי, אפשר פרטים על חוג קפוארה?', 'מה המחיר של חוג מחול בכפר סבא?', 'יש מקום בחוג היפ הופ לגיל 9?',
    'אפשר לקבוע שיעור ניסיון בראש העין?', 'רוצה להירשם, איך עושים את זה?', 'באילו ימים החוג?',
    'אפשר לדבר עם נציג בבקשה?', 'תודה רבה!', 'הבת שלי בת 6, זה מתאים?', 'אחזור אליכם בעוד שבוע',
    'יש הנחה לאחים?', 'איפה בדיוק הסניף באם המושבות?',
]
SIMULATED_BOT = [BOT_HELLO, BOT_PRICE, BOT_LINK, 'בשמחה! אבדוק ואחזור אליך.', 'הקבוצה מתאימה לגילאי 5 עד 7.']


def _full_phone(suffix: str) -> str:
    return DEMO_PHONE_PREFIX + suffix


def _local_phone(suffix: str) -> str:
    return f'050-555{suffix}'


# --- the invented catalogue and families ----------------------------------------------------

def _catalogue():
    """Three branches and one class each — only what a registration needs to hang on."""
    from apps.core.models import Branch, City, Room
    from apps.courses.models import Course, CourseType, Lesson
    from apps.instructors.models import Instructor

    lessons = {}
    kind, _ = CourseType.objects.get_or_create(name='קפוארה')
    for city_name, branch_name in (
        ('ראש העין', 'קרל וגרטי קורי 8'), ('כפר סבא', 'קניון דמרי סנטר'),
        ('פתח תקווה', 'אם המושבות, רפאל איתן 5'), ('פתח תקווה', 'מרכז העיר - מינץ 24'),
    ):
        city, _ = City.objects.get_or_create(name=city_name)
        branch, _ = Branch.objects.get_or_create(name=branch_name, defaults={'city': city, 'is_active': True})
        room, _ = Room.objects.get_or_create(branch=branch, name='סטודיו הדגמה', defaults={'capacity': 20})
        instructor, _ = Instructor.objects.get_or_create(
            first_name='מדריך', last_name='הדגמה', primary_branch=branch, defaults={'phone': '050-5559000'},
        )
        course, _ = Course.objects.get_or_create(
            name='קפוארה צעירים (הדגמה)', branch=branch,
            defaults={'course_type': kind, 'price': Decimal('260.00'), 'capacity': 20, 'is_active': True},
        )
        lesson, _ = Lesson.objects.get_or_create(
            course=course, day_of_week=2,
            defaults={'room': room, 'instructor': instructor, 'start_time': time(17, 0), 'end_time': time(17, 45),
                      'status': 'scheduled'},
        )
        lessons[branch_name] = lesson
    return lessons['קרל וגרטי קורי 8']


def _family(scenario: dict, lesson, first_message_at) -> None:
    """The invented family behind a phone the registrations are meant to know."""
    from apps.customers.models import Child, Family, Parent, Payment
    from apps.enrollments.models import LessonEnrollment

    kind = scenario['system']
    today = state.now_israel_date()
    first, _, last = scenario['name'].partition(' ')
    family = Family.objects.create(
        name=last or first, phone=_local_phone(scenario['phone']), branch=lesson.course.branch, notes=DEMO_MARK,
    )
    Parent.objects.create(
        family=family, first_name=first, last_name=last, phone=_local_phone(scenario['phone']), is_primary=True,
    )
    status = {
        'customer_before': 'active', 'customer_problem': 'payment_problem', 'registered_after': 'active',
        'trial_upcoming': 'trial_signed', 'trial_attended': 'trial_completed', 'trial_no_show': 'trial_completed',
        'trial_unmarked': 'trial_completed', 'signup_declined': 'pending', 'pending': 'pending',
        'inactive': 'inactive',
    }[kind]
    child = Child.objects.create(
        family=family, first_name=random.choice(['נועה', 'איתי', 'תמר', 'יואב', 'מאיה', 'עומר']), last_name=last or first,
        birth_date=today - timedelta(days=365 * 7), gender='female', status=status,
    )

    def pay(when, state_='completed', **fields):
        payment = Payment.objects.create(
            child=child, family=family, lesson=lesson, payment_type='recurring_subscription', status=state_,
            base_amount=Decimal('260'), final_amount=Decimal('260'), description='מנוי חודשי - קפוארה צעירים (הדגמה)',
            payment_date=when if state_ == 'completed' else None, **fields,
        )
        Payment.objects.filter(pk=payment.pk).update(created_at=when)

    if kind in ('customer_before', 'customer_problem'):
        pay(first_message_at - timedelta(days=60))
        LessonEnrollment.objects.create(child=child, lesson=lesson, status='active')
    elif kind == 'registered_after':
        pay(first_message_at + timedelta(hours=20))
        LessonEnrollment.objects.create(child=child, lesson=lesson, status='active')
    elif kind == 'trial_upcoming':
        LessonEnrollment.objects.create(child=child, lesson=lesson, status='active', trial_lesson_date=today + timedelta(days=3))
    elif kind.startswith('trial_'):
        LessonEnrollment.objects.create(
            child=child, lesson=lesson, status='active', trial_lesson_date=today - timedelta(days=6),
            trial_outcome={'trial_attended': 'attended', 'trial_no_show': 'no_show', 'trial_unmarked': 'unmarked'}[kind],
        )
    elif kind == 'signup_declined':
        pay(timezone.now() - timedelta(days=1, hours=2), 'failed', failure_code='141',
            failure_reason='חברת האשראי סירבה לעסקה (קוד 141).')
    elif kind == 'inactive':
        pay(first_message_at - timedelta(days=400))


# --- making it --------------------------------------------------------------------------------

def clear_demo() -> None:
    from apps.customers.models import Family

    Contact.objects.filter(phone__startswith=DEMO_PHONE_PREFIX).delete()
    Family.objects.filter(notes=DEMO_MARK).delete()


def _contact(scenario: dict, now, office_user, tags: dict) -> Contact:
    contact = Contact.objects.create(
        phone=_full_phone(scenario['phone']), name=scenario['name'], source=scenario.get('source', 'whatsapp'),
        manychat_subscriber_id='' if scenario.get('source') == 'manual' else f"9{scenario['phone']}",
        handled_by=scenario.get('handled_by', 'bot'),
    )
    unread, waiting, first_in, last_in, last = 0, None, None, None, None
    rows = []
    for who, text, minutes_ago in scenario['talk']:
        sent_at = now - timedelta(minutes=minutes_ago)
        incoming = who == 'c'
        row = Message(
            contact=contact, text=text, sent_at=sent_at,
            direction='in' if incoming else 'out',
            sender={'c': 'customer', 'b': 'bot'}.get(who, 'office'),
            sender_name='' if who in 'cb' else state.user_display_name(office_user),
            sent_by=None if who in 'cb' else office_user,
            message_type='template' if who == 't' else 'text',
            status='received' if incoming else ('failed' if who == 'x' else 'sent'),
            error='ManyChat דחה את הפעולה: Validation error' if who == 'x' else '',
            source='manychat' if who in 'cb' else 'kogo',
        )
        rows.append(row)
        if incoming:
            unread += 1
            waiting = waiting or sent_at
            first_in = first_in or sent_at
            last_in = sent_at
        elif row.status != 'failed':
            waiting = None
            if row.sender == 'office':
                unread = 0
        if row.status != 'failed':
            last = row
    Message.objects.bulk_create(rows)

    started = rows[0].sent_at if rows else now - timedelta(days=1)
    fields = dict(
        created_at=started, messages_count=len(rows), unread_count=0 if scenario.get('read') else unread,
        waiting_since=waiting, first_inbound_at=first_in, last_inbound_at=last_in,
        needs_analysis=bool(first_in),
    )
    if last is not None:
        fields.update(
            last_message_at=last.sent_at, last_message_text=state.preview(last.text),
            last_message_direction=last.direction, last_message_sender=last.sender,
        )
    if scenario.get('needs_human'):
        fields.update(needs_human=True, needs_human_reason=scenario['needs_human'], needs_human_at=last_in or now)
    if scenario.get('followup'):
        mark, due_in_days, note = scenario['followup']
        fields.update(
            followup_status=mark, followup_note=note, followup_by=office_user, followup_at=now - timedelta(hours=3),
            followup_due=state.now_israel_date() + timedelta(days=due_in_days) if due_in_days is not None else None,
        )
    Contact.objects.filter(pk=contact.pk).update(**fields)
    if scenario.get('tags'):
        contact.tags.set([tags[name] for name in scenario['tags']])

    events = [ContactEvent(contact=contact, kind=EVENT_CREATED, text='נוסף ידנית' if not rows else 'נוצר מהודעת וואטסאפ')]
    if scenario.get('needs_human'):
        events.append(ContactEvent(contact=contact, kind=EVENT_NEEDS_HUMAN, text=scenario['needs_human']))
    if scenario.get('handled_by') == 'human':
        events.append(ContactEvent(contact=contact, kind=EVENT_HANDLED_BY, text='נציג לקח את השיחה, הבוט הושתק', actor=office_user))
    ContactEvent.objects.bulk_create(events)
    ContactEvent.objects.filter(contact=contact).update(created_at=started)
    contact.refresh_from_db()
    return contact


@transaction.atomic
def seed_demo(office_user) -> dict:
    """Replace the demo data. Returns what was made."""
    random.seed(20261010)
    clear_demo()
    now = timezone.now()
    lesson = _catalogue()
    places = analysis.load_places()

    tags = {}
    for name, color in TAGS:
        tags[name], _ = Tag.objects.get_or_create(name=name, defaults={'color': color})
    for title, text in QUICK_REPLIES:
        QuickReply.objects.get_or_create(title=title, defaults={'text': text})

    made = {'contacts': 0, 'messages': 0, 'families': 0}
    for scenario in SCENARIOS:
        contact = _contact(scenario, now, office_user, tags)
        made['contacts'] += 1
        made['messages'] += len(scenario['talk'])
        if scenario.get('system'):
            _family(scenario, lesson, contact.first_inbound_at or now)
            made['families'] += 1
        if scenario.get('fresh'):
            continue    # left for the cron: not summarised, not matched yet
        # The keyword rules only: a demo must not depend on a key, or spend one.
        analysis.analyze_contact(contact, places=places, use_ai=False)
        matching.recheck_contact(contact)

    # Nothing was "just touched": the live update starts quiet.
    Contact.objects.filter(phone__startswith=DEMO_PHONE_PREFIX).update(touched_at=now - timedelta(minutes=1))
    ContactEvent.objects.filter(contact__phone__startswith=DEMO_PHONE_PREFIX, kind='analyzed').delete()
    return made


def simulate_one(rng: random.Random) -> tuple[str, str, object]:
    """
    One invented incoming message, through the same door ManyChat uses
    (inbound.store_event). Mostly from a contact that exists, sometimes a new
    one. Returns (phone, text, result).
    """
    existing = list(
        Contact.objects.filter(phone__startswith=DEMO_PHONE_PREFIX).exclude(last_inbound_at__isnull=True)
        .values_list('phone', 'name')
    )
    if existing and rng.random() < 0.7:
        phone, name = rng.choice(existing)
    else:
        phone, name = _full_phone(f'{rng.randint(200, 899):04d}'), rng.choice(SIMULATED_NAMES)
    text = rng.choice(SIMULATED_TEXTS)
    result = inbound.store_event(
        event=inbound.EVENT_CUSTOMER_MESSAGE, phone=phone, text=text, name=name, subscriber_id=f'9{phone[-4:]}',
    )
    return phone, text, result


def simulate_bot_reply(phone: str, rng: random.Random):
    return inbound.store_event(event=inbound.EVENT_BOT_REPLY, phone=phone, text=rng.choice(SIMULATED_BOT))
