"""
An invented catalogue for running the bot's self-checks on a machine with no
data: the unit tests, and `manage.py wahub_eval --fixtures` (which builds it
inside a transaction and rolls it back, so the local database is left as it
was). Never built on a server: there the scripts run against the real
catalogue.

Every name of a person here is made up. The cities are real names because the
conversation rules recognise a city by its name (analysis.BRANCH_ALIASES); the
branches, the classes, the prices and the people are not anybody's.

What it holds is what the scripts need the system to hold (world.py):

    ראש העין     one branch of ours, two fields. Capoeira for ages 4.5–6 whose
                 trial costs ₪30 and for grades ג–ד whose trial is free (with
                 a registration fee of its own); hip hop for ages 4.5–6 and
                 for grades א–ב, both free — so that at six the two hip hop
                 groups meet, and at five there are two fields to choose from.
                 The capoeira group of grades ג–ד is also where a trial cannot
                 always be booked: its nearest Sunday is full, and its nearest
                 Wednesday is closed for a holiday.
                 How full the lessons are differs on purpose: nine children pay
                 for the young capoeira group (five seats left), six for the
                 Wednesday of grades ג–ד and nobody for its Sunday, a capoeira
                 group of grades ה–ו is full (three of three), and the hip hop
                 groups are still empty. The branch's card holds a network name
                 and a password — invented, with an asterisk in it, the kind of
                 password a style rule would spoil.
    פתח תקווה    two branches of ours — a city where the branch is asked first.
                 Neither card holds a network.
    רמת גן       an external branch, run by the municipality.
"""
from __future__ import annotations

from datetime import time
from decimal import Decimal


def _trial_dates_that_cannot_be_taken(course, branch) -> None:
    """
    On the clock the scripts are played on (world.py, the "open" clock): the
    nearest Sunday of the class is full for a trial — the lesson takes two, and
    two invented children are booked — and its nearest Wednesday is a day the
    office closed to trials. The dates after them are free.
    """
    from django.utils import timezone

    from apps.core.tests.test_fixtures import TestDataFactory
    from apps.enrollments.models import LessonEnrollment, TrialBlockedDate
    from apps.enrollments.trial_reminders import iter_upcoming_lesson_occurrences
    from apps.wahub.evals import world

    at = timezone.localtime(world._clock(timezone.now(), is_open=True))
    sunday, wednesday = course.lessons.order_by('day_of_week')
    sunday.capacity = 2
    sunday.save(update_fields=['capacity'])
    full_on = iter_upcoming_lesson_occurrences(sunday, count=1, now=at)[0]
    for number in (1, 2):
        family = TestDataFactory.create_family(name=f'משפחת לדוגמה {number}', branch=branch, phone=f'050-00000{number}0')
        child = TestDataFactory.create_child(family=family, first_name=f'ילד לדוגמה {number}', last_name='בדוי', status='trial_signed')
        LessonEnrollment.objects.create(child=child, lesson=sunday, status='active', trial_lesson_date=full_on)
    TrialBlockedDate.objects.create(date=iter_upcoming_lesson_occurrences(wednesday, count=1, now=at)[0], reason='חופשה לדוגמה')


WIFI_NAME, WIFI_CODE = 'Studio-Demo-5G', 'Dance*2026'


def _children_who_pay(lesson, branch, count: int, tag: str) -> None:
    """`count` invented children registered to the lesson and paying — the ones the form counts as taking a seat."""
    from apps.core.tests.test_fixtures import TestDataFactory
    from apps.enrollments.models import LessonEnrollment

    for number in range(1, count + 1):
        family = TestDataFactory.create_family(name=f'משפחת בדויה {tag}{number}', branch=branch, phone=f'050-111{tag}{number:03d}')
        child = TestDataFactory.create_child(family=family, first_name=f'ילד בדוי {tag}{number}', last_name='בדוי', status='active')
        LessonEnrollment.objects.create(child=child, lesson=lesson, status='active')


def build() -> dict:
    """Creates the catalogue and, when the bot has no knowledge yet, files the knowledge. Returns the main rows."""
    from apps.core.tests.test_fixtures import TestDataFactory
    from apps.enrollments.models import TrialRegistrationPolicy
    from apps.wahub import knowledge_import
    from apps.wahub.models import KnowledgeItem

    TrialRegistrationPolicy.current()           # made on first use; here, so that the tools find it
    if not KnowledgeItem.objects.exists():
        knowledge_import.run()

    stage = TestDataFactory.create_branch(
        name='סטודיו הדגמה ראש העין', city=TestDataFactory.create_city('ראש העין'), address='רחוב הדוגמה 8, ראש העין',
        phone='03-5550000', arrival_directions='חניה מאחורי הבניין, קומה 2', wifi_name=WIFI_NAME, wifi_code=WIFI_CODE,
    )
    capoeira = TestDataFactory.create_course_type('קפוארה')
    capoeira.description = 'אומנות לחימה ברזילאית עם מוזיקה'
    capoeira.trial_bring_note = 'בגדים נוחים ובקבוק מים'
    capoeira.save()
    hip_hop = TestDataFactory.create_course_type('היפ הופ')
    uri = TestDataFactory.create_instructor(first_name='אורי', last_name='דוגמאי', branch=stage, phone='050-0000001')
    noa = TestDataFactory.create_instructor(first_name='נועה', last_name='לדוגמה', branch=stage, phone='050-0000002')
    room = TestDataFactory.create_room(name='אולם 1', branch=stage, capacity=14)

    def course(name, kind, low, high, price, *, lessons, instructor, **extra):
        row = TestDataFactory.create_course(
            name=name, branch=stage, course_type=kind, price=Decimal(price), min_age=low, max_age=high, capacity=14, **extra,
        )
        for day, start, end in lessons:
            TestDataFactory.create_lesson(course=row, room=room, instructor=instructor, day_of_week=day, start_time=start, end_time=end)
        return row

    young_capoeira = course(
        'קפוארה גיל 4.5-6', capoeira, 2, 2, '260.00', lessons=[(2, time(17, 0), time(17, 45))], instructor=uri,
        trial_lesson_is_paid=True, trial_lesson_price=Decimal('30.00'),
    )
    older_capoeira = course(
        'קפוארה כיתות ג-ד', capoeira, 5, 6, '270.00', instructor=uri, registration_fee_override=Decimal('60.00'),
        lessons=[(0, time(16, 0), time(17, 0)), (3, time(16, 0), time(17, 0))],
    )
    young_hip_hop = course('היפ הופ גיל 4.5-6', hip_hop, 2, 2, '250.00', lessons=[(4, time(16, 30), time(17, 15))], instructor=noa)
    older_hip_hop = course('היפ הופ כיתות א-ב', hip_hop, 3, 4, '280.00', lessons=[(1, time(17, 0), time(18, 0))], instructor=noa)
    # A group that is full: its one lesson takes three, and three pay.
    full_capoeira = course('קפוארה כיתות ה-ו', capoeira, 7, 8, '275.00', lessons=[(1, time(18, 0), time(19, 0))], instructor=uri)
    full_lesson = full_capoeira.lessons.get()
    full_lesson.capacity = 3
    full_lesson.save(update_fields=['capacity'])
    _children_who_pay(full_lesson, stage, 3, '1')
    _children_who_pay(young_capoeira.lessons.get(), stage, 9, '2')
    _children_who_pay(older_capoeira.lessons.get(day_of_week=3), stage, 6, '3')

    petah_tikva = TestDataFactory.create_city('פתח תקווה')
    for name, start in (('מינץ - הדגמה', time(16, 0)), ('אם המושבות - הדגמה', time(17, 30))):
        branch = TestDataFactory.create_branch(name=name, city=petah_tikva, address=f'{name}, פתח תקווה', phone='03-5550001')
        row = TestDataFactory.create_course(
            name=f'קפוארה כיתות א-ב {name}', branch=branch, course_type=capoeira, price=Decimal('265.00'), min_age=3, max_age=4,
        )
        TestDataFactory.create_lesson(course=row, day_of_week=1, start_time=start, end_time=time(start.hour + 1, start.minute))

    external = TestDataFactory.create_branch(
        name='רמת גן - הדגמה', city=TestDataFactory.create_city('רמת גן'), is_external=True,
        external_link='https://ramat-gan.example/signup',
    )
    _trial_dates_that_cannot_be_taken(older_capoeira, stage)
    return {
        'stage': stage, 'external': external, 'young_capoeira': young_capoeira, 'older_capoeira': older_capoeira,
        'young_hip_hop': young_hip_hop, 'older_hip_hop': older_hip_hop, 'full_capoeira': full_capoeira,
    }
