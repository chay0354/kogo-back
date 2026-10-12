"""
The bot sees everything about a class, and nothing about money that is not a
parent's business (docs/WAHUB-CONTRACT-BOT-TOOLS.md).

What is held here:
  * the trial lesson is said as the class's own card says it — the ראש העין
    mistake of 11.10.2026, where a five-year-old was matched with the group
    of grades ג–ד because an age in years was compared with a position on the
    age-group scale, and that group's trial is free;
  * each tool's answer on invented Kogo data;
  * "how much if I register now" equals what the registration charges;
  * no tool answers with a salary, a bonus, a payment, an invoice, a card or a
    token — by key and by value — and no tool writes.
"""
import json
import re
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import override_settings

from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import LessonBundle, LessonPriceOption
from apps.customers.financial_models import Discount
from apps.customers.models import Child, Family, Payment, RecurringPayment
from apps.enrollments.models import LessonEnrollment, TrialRegistrationPolicy
from apps.enrollments.trial_reminders import iter_upcoming_lesson_occurrences
from apps.scheduling.models import LessonCancellation
from apps.wahub import knowledge, knowledge_import, shadow, shadow_tools
from apps.wahub.knowledge_seed import OFFICE_PHONE
from apps.wahub.models import Contact, ShadowReply, Tag
from apps.wahub.shadow_tools import ToolContext, run_tool, summarize_result
from apps.wahub.tests.base import PHONE, WahubTestCase

POST = 'apps.wahub.shadow.requests.post'
SEND = 'apps.wahub.sending._deliver'
HANDOFF = 'apps.wahub.handoff._in_manychat'

# What no tool may answer with: a key that names it…
FORBIDDEN_KEYS = ('salary', 'bonus', 'payment', 'invoice', 'card', 'token', 'iban')
# …or the thing itself, planted in the data below.
SALARY_OF_THE_GROUP = Decimal('7777.77')
SALARY_OF_THE_LESSON = Decimal('5555.55')
SALARY_PER_LESSON = Decimal('8888.88')
COST_OF_THE_BRANCH = Decimal('6666.66')
INSTRUCTOR_PHONE = '050-9999999'
INSTRUCTOR_EMAIL = 'moshe.private@example.com'
WIFI_CODE = 'wifi-secret-77'
CARD_TOKEN = 'tok_SECRET_4580'
FAMILY_PAID = Decimal('431.37')
FORBIDDEN_VALUES = (
    '7777.77', '5555.55', '8888.88', '6666.66', INSTRUCTOR_PHONE, '0509999999', INSTRUCTOR_EMAIL, WIFI_CODE,
    CARD_TOKEN, '431.37',
)


_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def _without_ids(payload) -> str:
    """The payload as text, without its ids: a random UUID may hold any digits ("…b71199b0…"), and a price check must not trip on one."""
    return _UUID.sub('', json.dumps(payload, ensure_ascii=False))


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from _keys(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _keys(inner)


def _discount(name, value, kind='fixed', **extra):
    return Discount.objects.create(
        name=name, discount_type=kind, value=Decimal(value), applies_to='child',
        promotion_type='permanent', is_active=True, is_built_in=True, **extra,
    )


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='', ANTHROPIC_API_KEY='')
class CourseToolsTestCase(WahubTestCase):
    """
    A branch in ראש העין shaped like the real one: a capoeira group for ages
    4.5–6 whose trial costs ₪30, a capoeira group for grades ג–ד whose trial is
    free, a dance group — and beside it everything a tool must not repeat.
    """

    def setUp(self):
        super().setUp()
        self.deliver = patch(SEND, side_effect=AssertionError('a tool tried to send')).start()
        self.addCleanup(patch.stopall)
        self.handoff = patch(HANDOFF, side_effect=AssertionError('a tool tried to touch ManyChat')).start()
        TrialRegistrationPolicy.current()       # made on first use; here, so that the tools find it

        city = TestDataFactory.create_city('ראש העין')
        self.branch = TestDataFactory.create_branch(
            name='קרל וגרטי קורי 8', city=city, address='קרל וגרטי קורי 8, ראש העין', phone='03-5550000',
            arrival_directions='חניה מאחור, קומה 2', manager_name='דנה', monthly_cost=COST_OF_THE_BRANCH, wifi_code=WIFI_CODE,
        )
        self.capoeira = TestDataFactory.create_course_type('קפוארה')
        self.capoeira.description = 'אומנות לחימה ברזילאית עם מוזיקה'
        self.capoeira.trial_bring_note = 'בגדים נוחים ובקבוק מים'
        self.capoeira.save()
        self.dance = TestDataFactory.create_course_type('היפהופ - מחול')
        self.moshe = TestDataFactory.create_instructor(
            first_name='משה', last_name='לוי', branch=self.branch, phone=INSTRUCTOR_PHONE, email=INSTRUCTOR_EMAIL,
            specialization='קפוארה', photo_url='https://cdn.example/moshe.jpg', fixed_salary_per_lesson=SALARY_PER_LESSON,
        )
        self.room = TestDataFactory.create_room(name='סטודיו 1', branch=self.branch, capacity=10)

        # Ages 4.5–6 (position 2 on the scale). The trial costs ₪30.
        self.young = TestDataFactory.create_course(
            name='קפוארה 4.5-6 יום שלישי', branch=self.branch, course_type=self.capoeira, price=Decimal('260.00'),
            min_age=2, max_age=2, capacity=12, trial_lesson_is_paid=True, trial_lesson_price=Decimal('30.00'),
            description='שיעור קפוארה לגיל הרך', instructor=self.moshe, instructor_salary_override=SALARY_OF_THE_GROUP,
        )
        self.young_lesson = TestDataFactory.create_lesson(
            course=self.young, room=self.room, instructor=self.moshe, day_of_week=2,
            start_time=time(17, 0), end_time=time(17, 45), instructor_salary_override=SALARY_OF_THE_LESSON,
        )
        # Grades ג–ד (positions 5–6: NOT ages five to six). The trial is free, and the fee is its own.
        self.older = TestDataFactory.create_course(
            name='ג-ד קפוארה ואקרובטיקה', branch=self.branch, course_type=self.capoeira, price=Decimal('260.00'),
            min_age=5, max_age=6, capacity=20, registration_fee_override=Decimal('60.00'),
        )
        self.older_sunday = TestDataFactory.create_lesson(
            course=self.older, room=self.room, instructor=self.moshe, day_of_week=0, start_time=time(16, 0), end_time=time(17, 0),
        )
        self.older_wednesday = TestDataFactory.create_lesson(
            course=self.older, room=self.room, instructor=self.moshe, day_of_week=3, start_time=time(16, 0), end_time=time(17, 0),
        )
        self.track = LessonBundle.objects.create(course=self.older, name='פעמיים בשבוע', combined_price=Decimal('400.00'))
        self.track.lessons.set([self.older_sunday, self.older_wednesday])
        # Grades א–ב, dance.
        self.dancers = TestDataFactory.create_course(
            name='א-ב ריקוד', branch=self.branch, course_type=self.dance, price=Decimal('280.00'), min_age=3, max_age=4,
        )
        self.dance_lesson = TestDataFactory.create_lesson(
            course=self.dancers, room=self.room, instructor=self.moshe, day_of_week=1, start_time=time(17, 0), end_time=time(18, 0),
        )
        self.external = TestDataFactory.create_branch(
            name='רמת גן - גאולים', city=TestDataFactory.create_city('רמת גן'), is_external=True,
            external_link='https://ramat-gan.example/signup',
        )
        self.external_course = TestDataFactory.create_course(
            name='קפוארה גאולים', branch=self.external, course_type=self.capoeira, price=Decimal('199.00'),
        )
        TestDataFactory.create_lesson(course=self.external_course, day_of_week=4)

    # --- helpers ---

    def tool(self, tool_name, ctx=None, /, **arguments):
        return run_tool(tool_name, arguments, ctx or ToolContext())

    def find(self, **arguments):
        return self.tool('find_courses', **{'city': 'ראש העין', **arguments})

    def card(self, course=None, **arguments):
        if course is not None:
            arguments['course_id'] = str(course.id)
        return self.tool('course_card', **arguments)

    def quote(self, course=None, ctx=None, **arguments):
        return self.tool('quote_registration', ctx, course_id=str((course or self.young).id), **arguments)

    def family_with_child(self, *, status='active', phone='0505550101', **child):
        family = TestDataFactory.create_family(branch=self.branch, phone=phone)
        TestDataFactory.create_parent(family=family, phone=phone)
        return family, TestDataFactory.create_child(family=family, status=status, **child)

    def identified(self, child):
        contact, _ = Contact.objects.update_or_create(
            phone=PHONE, defaults={'name': 'רותם', 'kogo_child_ids': [str(child.id)], 'kogo_outcome': 'registered'},
        )
        return ToolContext(contact)

    def registration(self, child, lesson, **extra):
        """What the registration itself would charge this child, with nothing written."""
        return PaymentService().initiate_subscription_payment(
            child_id=str(child.id), lesson_id=str(lesson.id), quote_only=True, **extra,
        )


class TrialIsSaidAsTheClassCardSaysItTest(CourseToolsTestCase):
    """The mistake of 11.10.2026 and the ways it could be made."""

    def test_a_five_year_old_gets_the_group_of_his_age_and_its_paid_trial(self):
        result = self.find(age=5, course_type='קפוארה')
        self.assertEqual([course['name'] for course in result['courses']], ['קפוארה 4.5-6 יום שלישי'])
        trial = result['courses'][0]['trial']
        self.assertEqual((trial['is_paid'], trial['price'], trial['text']), (True, '30.00', 'שיעור ניסיון בעלות ₪30'))
        self.assertIn('₪30 יורדים מהתשלום הראשון', trial['credit_rule'])
        self.assertIn('60 יום', trial['credit_rule'])
        self.assertEqual(trial['bring'], 'בגדים נוחים ובקבוק מים')
        # The group of grades ג–ד — positions 5–6 on the scale — is not offered to a child of five.
        self.assertNotIn('ג-ד קפוארה ואקרובטיקה', json.dumps(result, ensure_ascii=False))

    def test_a_nine_year_old_gets_the_group_of_grades_three_and_four_whose_trial_is_free(self):
        result = self.find(age=9, course_type='קפוארה')
        self.assertEqual([course['name'] for course in result['courses']], ['ג-ד קפוארה ואקרובטיקה'])
        self.assertEqual(result['courses'][0]['trial']['text'], 'שיעור ניסיון ללא עלות')
        self.assertEqual(result['courses'][0]['ages'], 'כיתות ג–ד')
        self.assertEqual((result['courses'][0]['min_age'], result['courses'][0]['max_age']), (8.0, 10.0))

    def test_a_grade_is_understood_as_a_grade(self):
        capoeira = {'course_type': 'קפוארה'}
        self.assertEqual([course['name'] for course in self.find(grade='כיתה ג', **capoeira)['courses']], ['ג-ד קפוארה ואקרובטיקה'])
        self.assertEqual([course['name'] for course in self.find(grade='גן חובה', **capoeira)['courses']], ['קפוארה 4.5-6 יום שלישי'])
        self.assertEqual([course['name'] for course in self.find(grade='ב', course_type='מחול')['courses']], ['א-ב ריקוד'])
        # The field is never assumed (the owner, 12.10.2026): one field for the grade is offered by name, as a question.
        alone = self.find(grade='ב')
        self.assertEqual((alone['need'], alone['course_types'], alone['courses']), ('course_type', ['היפהופ - מחול'], []))
        self.assertEqual(alone['ask'], 'יש כאן היפהופ - מחול — זה התחום שמחפשים?')

    def test_an_age_on_the_line_between_two_groups_asks_the_grade_before_any_class(self):
        """
        Until 12.10.2026 both groups came back with a hint to ask. The owner's
        order ("first find out, then offer") made the hint the answer: the two
        groups to choose between, the question, and no class of either.
        """
        result = self.find(age=6)
        self.assertEqual((result['need'], result['course_types']), ('course_type', ['היפהופ - מחול', 'קפוארה']))
        self.assertEqual(result['courses'], [])
        # The field is known and one group fits it: nothing is left to ask.
        result = self.find(age=6, course_type='קפוארה')
        self.assertEqual((result['need'], [course['ages'] for course in result['courses']]), ('', ['גיל 4.5-6']))
        # Two groups of one field meet at six — גן חובה or כיתה א: the grade is asked, and no class of either is given.
        first_grade = TestDataFactory.create_course(
            name='א-ב קפוארה', branch=self.branch, course_type=self.capoeira, price=Decimal('270.00'), min_age=3, max_age=4,
        )
        TestDataFactory.create_lesson(course=first_grade, room=self.room, instructor=self.moshe, day_of_week=4)
        result = self.find(age=6, course_type='קפוארה')
        self.assertEqual((result['need'], result['ask'], result['between']), ('grade', 'באיזו כיתה או גן הילד?', ['גיל 4.5-6', 'כיתות א–ב']))
        self.assertEqual(result['courses'], [])
        self.assertNotIn('270', json.dumps(result, ensure_ascii=False))
        self.assertEqual([course['name'] for course in self.find(grade='כיתה א', course_type='קפוארה')['courses']], ['א-ב קפוארה'])
        # A customer who pressed to see everything still gets both, with the line that says to ask.
        result = self.find(age=6, course_type='קפוארה', show_all=True)
        self.assertEqual({course['ages'] for course in result['courses']}, {'גיל 4.5-6', 'כיתות א–ב'})
        self.assertIn('לשאול באיזו כיתה או גן', result['hint'])

    def test_a_child_under_three_has_no_group(self):
        result = self.find(age=2, course_type='קפוארה')
        self.assertEqual(result['courses'], [])
        self.assertIn('החוגים מגיל 3', result['hint'])

    def test_one_class_is_never_read_as_the_branch(self):
        result = self.find(show_all=True)       # the whole branch: only for a customer who asked to see everything
        summary = result['trial_summary']
        self.assertEqual((summary['paid_courses'], summary['free_courses'], summary['mixed']), (1, 2, True))
        self.assertIn('שונה מחוג לחוג', summary['text'])
        self.assertIn('₪30', summary['text'])
        self.assertIn('ניסיון בתשלום ב-1, ללא עלות ב-2', summarize_result('find_courses', result))

    def test_a_long_list_is_cut_without_hiding_a_paid_trial(self):
        """
        Twelve are spelled out. In ראש העין that was eleven dance groups, all
        free, and one capoeira group: the other paid trials fell off the end
        unseen. Whatever the order, a class that is cut is still said in a line,
        and the summary counts every class found.
        """
        for index in range(13):
            for paid in (True, False):
                course = TestDataFactory.create_course(
                    name=f'קבוצה {index:02d} {"בתשלום" if paid else "חופשי"}', branch=self.branch, course_type=self.dance,
                    price=Decimal('260.00'), min_age=3, max_age=4,
                    trial_lesson_is_paid=paid, trial_lesson_price=Decimal('30.00') if paid else None,
                )
                TestDataFactory.create_lesson(course=course, room=self.room, instructor=self.moshe, day_of_week=5)
        result = self.find(show_all=True)
        self.assertEqual((len(result['courses']), len(result['more_courses']), result['courses_total']), (12, 17, 29))
        cut_and_paid = [row for row in result['more_courses'] if row['trial_is_paid']]
        self.assertGreaterEqual(len(cut_and_paid), 2)                  # fourteen are paid and twelve are spelled out
        self.assertTrue(all(row['trial_price'] == '30.00' for row in cut_and_paid))
        spelled_and_paid = [course for course in result['courses'] if course['trial']['is_paid']]
        self.assertEqual(len(cut_and_paid) + len(spelled_and_paid), 14)
        self.assertEqual({row['id'] for row in result['more_courses']} & {course['id'] for course in result['courses']}, set())
        summary = result['trial_summary']
        self.assertEqual((summary['paid_courses'], summary['free_courses'], summary['mixed']), (14, 15, True))
        self.assertIn('more_courses', result['hint'])
        self.assertIn('12 חוגים מפורטים מתוך 29', summarize_result('find_courses', result))

    def test_a_trial_marked_paid_with_no_price_is_never_called_free(self):
        self.young.trial_lesson_price = None
        self.young.save(update_fields=['trial_lesson_price'])
        trial = self.find(age=5, course_type='קפוארה')['courses'][0]['trial']
        self.assertEqual((trial['is_paid'], trial['price']), (True, None))
        self.assertNotIn('ללא עלות', trial['text'].replace('לא לומר שהוא ללא עלות', ''))
        self.assertIn('לבדוק במשרד', trial['text'])
        self.assertEqual(shadow_tools.trial_line([{'name': 'x', 'trial': trial}]), 'שיעור הניסיון בתשלום, את המחיר כדאי לבדוק במשרד')

    def test_the_trial_is_what_the_registration_form_is_given(self):
        """The same two fields of the course card, as the form's catalogue sends them."""
        from rest_framework.test import APIClient

        rows = APIClient().get('/api/v1/customers/widget/courses/', {'branch_id': str(self.branch.id)}).json()
        form = {row['id']: row for row in rows}
        for course in self.find(show_all=True)['courses']:
            shown = form[course['id']]
            self.assertEqual(course['trial']['is_paid'], shown['trial_lesson_is_paid'], course['name'])
            self.assertEqual(course['trial']['price'], shown['trial_lesson_price'], course['name'])
            self.assertEqual(course['price_monthly'], shown['price'], course['name'])

    def test_the_stub_says_the_trial_of_the_class_it_lists(self):
        knowledge_import.run()
        self.incoming('רוצה שיעור ניסיון בקפוארה בראש העין לבן 5')
        reply = shadow.propose(self.contact())
        self.assertIn('שיעור ניסיון בעלות *₪30*', reply.text)
        self.assertNotIn('ללא עלות', reply.text)
        self.incoming('ולבן 9? יש שיעור ניסיון בקפוארה בראש העין?', phone='972505550177')
        reply = shadow.propose(self.contact('972505550177'))
        self.assertIn('שיעור ניסיון ללא עלות', reply.text)

    def test_the_stub_names_each_class_when_their_trials_differ(self):
        line = shadow_tools.trial_line(self.find(course_type='קפוארה', show_all=True)['courses'])
        self.assertTrue(line.startswith('שיעור ניסיון: '))
        self.assertEqual(
            set(line[len('שיעור ניסיון: '):].split(', ')),
            {'ג-ד קפוארה ואקרובטיקה ללא עלות', 'קפוארה 4.5-6 יום שלישי בעלות ₪30'},
        )

    def test_the_instruction_is_in_the_system_task_and_in_the_tools(self):
        self.assertIn('מחיר, שיעור ניסיון, דמי רישום, הנחות, קיזוז, תפוסה, מועדים ומדריך — תמיד מהכלים, אף פעם לא מהזיכרון או מהידע', shadow.SYSTEM_TASK)
        self.assertIn('quote_registration', shadow.SYSTEM_TASK)
        descriptions = {tool['name']: tool['description'] for tool in shadow_tools.TOOLS}
        for name in ('find_courses', 'course_card'):
            self.assertIn('אף פעם לא מהזיכרון, מהשיחה או מהידע', descriptions[name])
            self.assertIn('התיקון הוא בכרטיס החוג ב-Kogo, לא בבוט', descriptions[name])
        self.assertIn('trial.is_paid=false פירושו ללא עלות', descriptions['find_courses'])
        self.assertIn('לא מחשבים לבד', descriptions['quote_registration'])


class FindCoursesTest(CourseToolsTestCase):
    def test_a_class_carries_what_a_parent_asks_about(self):
        course = self.find(age=5, course_type='קפוארה')['courses'][0]
        self.assertEqual(course['description'], 'שיעור קפוארה לגיל הרך')
        self.assertEqual((course['price_monthly'], course['registration_fee']), ('260.00', '120.00'))
        self.assertEqual((course['ages'], course['min_age'], course['max_age']), ('גיל 4.5-6', 4.5, 6.0))
        lesson = course['lessons'][0]
        self.assertEqual((lesson['day'], lesson['start'], lesson['end'], lesson['instructor']), ('שלישי', '17:00', '17:45', 'משה לוי'))
        self.assertEqual((lesson['capacity'], lesson['enrolled'], lesson['seats_left'], lesson['is_full']), (10, 0, 10, False))
        self.assertEqual(len(lesson['next_dates']), 2)
        coming = iter_upcoming_lesson_occurrences(self.young_lesson, count=2)
        self.assertEqual([row['date'] for row in lesson['next_dates']], [day.strftime('%d.%m.%Y') for day in coming])

    def test_the_description_falls_back_to_the_fields_and_the_fee_is_the_classs_own(self):
        course = self.find(age=9, course_type='קפוארה')['courses'][0]
        self.assertEqual(course['description'], 'אומנות לחימה ברזילאית עם מוזיקה')
        self.assertEqual(course['registration_fee'], '60.00')
        self.assertEqual(course['tracks'][0]['name'], 'פעמיים בשבוע')
        self.assertEqual(course['tracks'][0]['price_monthly'], '400.00')
        self.assertEqual(course['tracks'][0]['lessons'], ['ראשון 16:00–17:00', 'רביעי 16:00–17:00'])

    def test_an_external_branch_still_answers_with_no_class(self):
        result = self.tool('find_courses', city='רמת גן')
        self.assertEqual(result['courses'], [])
        self.assertTrue(result['branches'][0]['is_external'])
        self.assertNotIn('199', _without_ids(result))


class CourseCardTest(CourseToolsTestCase):
    def test_everything_about_one_class(self):
        result = self.card(self.young)
        self.assertTrue(result['found'])
        course = result['course']
        self.assertEqual((course['name'], course['course_type'], course['branch'], course['city']), ('קפוארה 4.5-6 יום שלישי', 'קפוארה', 'קרל וגרטי קורי 8', 'ראש העין'))
        self.assertEqual(course['description'], 'שיעור קפוארה לגיל הרך')
        self.assertEqual(course['course_type_description'], 'אומנות לחימה ברזילאית עם מוזיקה')
        self.assertEqual((course['ages'], course['is_adult'], course['must_attend_all_lessons']), ('גיל 4.5-6', False, False))
        self.assertEqual((course['price_monthly'], course['registration_fee']), ('260.00', '120.00'))
        self.assertIn('פעם אחת לכל ילד', course['registration_fee_note'])
        self.assertEqual(course['show_in_widget'], True)
        trial = course['trial']
        self.assertEqual((trial['is_paid'], trial['price'], trial['open'], trial['full']), (True, '30.00', True, False))
        self.assertEqual(trial['bring'], 'בגדים נוחים ובקבוק מים')
        lesson = course['lessons'][0]
        self.assertEqual((lesson['room'], lesson['status'], lesson['instructor']), ('סטודיו 1', 'מתוכנן', 'משה לוי'))
        self.assertEqual(len(lesson['next_dates']), 3)
        self.assertEqual(result['branch']['address'], 'קרל וגרטי קורי 8, ראש העין')
        self.assertEqual(result['branch']['directions'], 'חניה מאחור, קומה 2')
        self.assertIn('₪30', summarize_result('course_card', result))
        self.assertIn('דמי רישום ₪120', summarize_result('course_card', result))

    def test_how_many_are_enrolled_the_limit_of_a_lesson_and_the_seats_by_date(self):
        for index in range(3):
            _family, child = self.family_with_child(phone=f'05055502{index:02d}', first_name=f'ילד{index}')
            LessonEnrollment.objects.create(child=child, lesson=self.young_lesson, status='active')
        first, second = iter_upcoming_lesson_occurrences(self.young_lesson, count=2)
        _family, visitor = self.family_with_child(status='trial_signed', phone='0505550299', first_name='אורח')
        LessonEnrollment.objects.create(child=visitor, lesson=self.young_lesson, status='active', trial_lesson_date=first)
        lesson = self.card(self.young)['course']['lessons'][0]
        # The room holds ten and the group twelve: ten. Three pay; a trial sits in the room on its own date only.
        self.assertEqual((lesson['capacity'], lesson['enrolled'], lesson['seats_left']), (10, 3, 7))
        self.assertEqual(lesson['next_dates'][0], {'date': first.strftime('%d.%m.%Y'), 'trial_seats_left': 6})
        self.assertEqual(lesson['next_dates'][1], {'date': second.strftime('%d.%m.%Y'), 'trial_seats_left': 7})
        # A limit of the lesson itself is the tightest of the three.
        self.young_lesson.capacity = 3
        self.young_lesson.save(update_fields=['capacity'])
        lesson = self.card(self.young)['course']['lessons'][0]
        self.assertEqual((lesson['capacity'], lesson['seats_left'], lesson['is_full'], lesson['trial_is_full']), (3, 0, True, True))

    def test_a_cancelled_date_is_not_a_coming_date_and_a_closed_trial_is_closed(self):
        first, second = iter_upcoming_lesson_occurrences(self.young_lesson, count=2)
        LessonCancellation.objects.create(lesson=self.young_lesson, occurrence_date=first)
        self.young_lesson.trial_registration_open = False
        self.young_lesson.save(update_fields=['trial_registration_open'])
        course = self.card(self.young)['course']
        self.assertEqual(course['lessons'][0]['next_dates'][0]['date'], second.strftime('%d.%m.%Y'))
        self.assertEqual((course['lessons'][0]['trial_open'], course['trial']['open']), (False, False))

    def test_tracks_a_price_of_its_own_for_a_second_class_and_an_extra_price(self):
        self.older_sunday.additional_course_prices = [{'course_index': 2, 'price': 220}]
        self.older_sunday.save(update_fields=['additional_course_prices'])
        LessonPriceOption.objects.create(lesson=self.older_sunday, display_title='קפוארה מתקדמים', monthly_price=Decimal('300.00'))
        course = self.card(self.older)['course']
        self.assertEqual([track['price_monthly'] for track in course['tracks']], ['400.00'])
        sunday = next(row for row in course['lessons'] if row['day'] == 'ראשון')
        self.assertEqual(sunday['price_as_second_class'], '220.00')
        self.assertEqual(sunday['price_options'][0]['title'], 'קפוארה מתקדמים')
        self.assertEqual(sunday['price_options'][0]['price_monthly'], '300.00')

    def test_the_discounts_are_the_ones_the_crm_holds_and_none_is_invented(self):
        self.assertEqual(self.card(self.young)['discounts_available'], [])
        _discount('הנחת ילד שני', '10.00')
        _discount('הנחת שיעור נוסף', '0.00')                        # at 0: nobody configured it
        _discount('רישום מוקדם', '15.00', kind='percentage', start_date=date.today() - timedelta(days=2), end_date=date.today() + timedelta(days=5))
        _discount('רישום מוקדם ישן', '50.00', start_date=date.today() - timedelta(days=60), end_date=date.today() - timedelta(days=30))
        rows = self.card(self.young)['discounts_available']
        self.assertEqual([row['name'] for row in rows], ['הנחת ילד שני', 'רישום מוקדם'])
        self.assertEqual(rows[0]['value'], '₪10 הנחה בחודש')
        self.assertIn('אח או אחות כבר רשומים', rows[0]['rule'])
        self.assertEqual(rows[1]['value'], '15% הנחה מהמחיר החודשי')
        self.assertTrue(rows[1]['active_now'])

    def test_by_name_and_branch_and_a_name_that_fits_several(self):
        self.assertEqual(self.card(name='ג-ד קפוארה ואקרובטיקה', branch='קרל')['course']['id'], str(self.older.id))
        several = self.card(name='קפוארה', city='ראש העין')
        self.assertFalse(several['found'])
        self.assertEqual({row['name'] for row in several['matches']}, {'קפוארה 4.5-6 יום שלישי', 'ג-ד קפוארה ואקרובטיקה'})
        self.assertFalse(self.card(name='שחייה', city='ראש העין')['found'])
        self.assertFalse(self.card()['found'])
        self.assertFalse(self.tool('course_card', course_id='not-an-id')['found'])

    def test_a_class_hidden_from_the_form_is_marked_and_a_closed_one_is_not_found(self):
        self.young.show_in_widget = False
        self.young.save(update_fields=['show_in_widget'])
        result = self.card(self.young)
        self.assertEqual(result['course']['show_in_widget'], False)
        self.assertIn('לא מוצג בהרשמה באתר', result['course']['note'])
        self.assertEqual(self.find(age=5, course_type='קפוארה')['courses'], [])
        self.young.is_active = False
        self.young.save(update_fields=['is_active'])
        self.assertFalse(self.card(self.young)['found'])

    def test_a_class_of_an_external_branch_gives_the_link_and_no_price(self):
        result = self.card(self.external_course)
        self.assertTrue(result['is_external'])
        self.assertEqual(result['course']['external_link'], 'https://ramat-gan.example/signup')
        self.assertNotIn('199', _without_ids(result))
        self.assertIn('לבדוק מול העירייה', summarize_result('course_card', result))
        # …and when a name fits it among others, it is named with no price beside it.
        several = self.card(name='קפוארה')
        named = next(row for row in several['matches'] if row['name'] == 'קפוארה גאולים')
        self.assertEqual(set(named), {'id', 'name', 'branch', 'course_type', 'is_external'})
        self.assertNotIn('199', _without_ids(several))


class QuoteRegistrationTest(CourseToolsTestCase):
    def assertAddsUp(self, result):
        total = sum(Decimal(row['amount']) for row in result['first_charge']['breakdown'])
        self.assertEqual(total, Decimal(result['first_charge']['amount']))

    def test_a_new_child_is_quoted_what_the_registration_form_quotes(self):
        from rest_framework.test import APIClient

        result = self.quote()
        self.assertTrue(result['can_quote'])
        form = APIClient().post('/api/v1/customers/widget/quote/', {'items': [{
            'parent_id_number': '123456782', 'parent_first_name': 'Test', 'parent_last_name': 'Parent',
            'parent_phone': '0501234567', 'child_first_name': 'Kid', 'child_last_name': 'Parent',
            'child_id_number': '456789014', 'child_birth_date': '2021-01-01', 'child_gender': 'male',
            'course_id': str(self.young.id), 'lesson_id': str(self.young_lesson.id),
        }]}, format='json').json()['items'][0]
        self.assertEqual(float(result['first_charge']['amount']), form['final_amount'])
        self.assertEqual(float(result['monthly_after']['amount']), form['monthly_amount'])
        self.assertEqual(result['monthly_after']['from'], date.fromisoformat(form['next_billing_date']).strftime('%d.%m.%Y'))
        labels = [row['label'] for row in result['first_charge']['breakdown']]
        self.assertEqual(labels[-1], 'דמי רישום (פעם אחת לכל ילד)')
        self.assertEqual(result['first_charge']['breakdown'][-1]['amount'], '120.00')
        self.assertEqual(float(result['first_charge']['breakdown'][0]['amount']), form['prorated_amount'])
        self.assertAddsUp(result)
        self.assertEqual(result['based_on'], {'siblings_registered': False, 'another_class_same_child': False, 'took_paid_trial': False})
        self.assertEqual(Family.objects.filter(parent_id_number='123456782').count(), 0)

    def test_the_summary_line_spells_the_sum(self):
        result = self.quote(took_paid_trial=True)
        line = summarize_result('quote_registration', result)
        self.assertIn('תשלום ראשון ₪', line)
        self.assertIn('+ דמי רישום ₪120', line)
        self.assertIn('− קיזוז שיעור ניסיון ששולם ₪30', line)
        self.assertIn('אחר כך ₪260 לחודש', line)

    def test_a_paid_trial_told_by_the_customer_comes_off_and_the_note_says_on_what_it_rests(self):
        plain, credited = self.quote(), self.quote(took_paid_trial=True)
        self.assertEqual(Decimal(plain['first_charge']['amount']) - Decimal(credited['first_charge']['amount']), Decimal('30.00'))
        self.assertEqual(credited['first_charge']['breakdown'][-1], {'label': 'קיזוז שיעור ניסיון ששולם', 'amount': '-30.00'})
        self.assertEqual(credited['monthly_after']['amount'], '260.00')
        self.assertTrue(credited['based_on']['took_paid_trial'])
        self.assertTrue(any('בהנחה ששולם ₪30' in note for note in credited['notes']))
        self.assertAddsUp(credited)
        # A class whose trial is free has nothing to take off.
        free = self.quote(self.older, took_paid_trial=True)
        self.assertFalse(free['based_on']['took_paid_trial'])
        self.assertTrue(any('אין מה לקזז' in note for note in free['notes']))

    def test_a_sibling_and_a_second_class_are_priced_as_the_registration_prices_them(self):
        _discount('הנחת ילד שני', '10.00')
        _discount('הנחת שיעור נוסף', '15.00')
        family, big_brother = self.family_with_child()
        LessonEnrollment.objects.create(child=big_brother, lesson=self.dance_lesson, status='active')
        Payment.objects.create(
            child=big_brother, family=family, branch=self.branch, lesson=self.dance_lesson,
            payment_type='recurring_subscription', status='completed', base_amount=Decimal('280.00'),
            discount_amount=Decimal('0.00'), final_amount=Decimal('400.00'), registration_fee=Decimal('120.00'),
        )
        little = TestDataFactory.create_child(family=family, first_name='קטן', status='pending')

        sibling = self.quote(siblings_registered=True)
        registration = self.registration(little, self.young_lesson)
        self.assertEqual(float(sibling['first_charge']['amount']), registration['final_amount'])
        self.assertEqual(sibling['monthly_after']['amount'], '250.00')
        self.assertEqual(sibling['monthly_after']['price_before_discounts'], '260.00')
        self.assertEqual(sibling['monthly_after']['discounts'][0]['name'], 'הנחת ילד שני')
        self.assertEqual(sibling['monthly_after']['discounts'][0]['amount'], '10.00')
        self.assertAddsUp(sibling)

        second_class = self.quote(another_class_same_child=True)
        registration = self.registration(big_brother, self.young_lesson)
        self.assertEqual(float(second_class['first_charge']['amount']), registration['final_amount'])
        self.assertEqual(second_class['monthly_after']['amount'], '245.00')
        self.assertNotIn('דמי רישום (פעם אחת לכל ילד)', [row['label'] for row in second_class['first_charge']['breakdown']])
        self.assertTrue(any('דמי רישום לא נגבים שוב' in note for note in second_class['notes']))

    def test_an_identified_customer_is_read_from_their_own_card(self):
        """Nobody tells the tool anything: the contact's family has a child on a team, and a paid trial not yet used."""
        _discount('הנחת ילד שני', '10.00')
        family, big_sister = self.family_with_child()
        LessonEnrollment.objects.create(child=big_sister, lesson=self.dance_lesson, status='active')
        little = TestDataFactory.create_child(family=family, first_name='קטן', status='trial_completed', id_number='234567892')
        trial_day = date.today() - timedelta(days=5)
        Payment.objects.create(
            child=little, family=family, branch=self.branch, lesson=self.young_lesson, payment_type='one_time',
            status='completed', base_amount=Decimal('30.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('30.00'),
            trial_lesson_date=trial_day, description='שיעור ניסיון',
        )
        result = self.quote(ctx=self.identified(little))
        registration = self.registration(little, self.young_lesson)
        self.assertEqual(float(result['first_charge']['amount']), registration['final_amount'])
        self.assertEqual(float(result['monthly_after']['amount']), registration['monthly_amount'])
        self.assertEqual(result['based_on'], {'siblings_registered': True, 'another_class_same_child': False, 'took_paid_trial': True})
        self.assertIn(registration['trial_credit_reason'], result['notes'])           # the form's own sentence
        self.assertTrue(any('חושבה הנחת ילד שני' in note for note in result['notes']))
        self.assertAddsUp(result)
        # Told "no trial, no sibling", it is priced without them.
        bare = self.quote(ctx=self.identified(little), siblings_registered=False, took_paid_trial=False)
        self.assertEqual(bare['monthly_after']['amount'], '260.00')
        self.assertFalse(bare['based_on']['took_paid_trial'])

    def test_a_trial_the_card_does_not_hold_is_not_credited_to_an_identified_customer(self):
        _family, child = self.family_with_child(status='pending')
        result = self.quote(ctx=self.identified(child), took_paid_trial=True)
        self.assertFalse(result['based_on']['took_paid_trial'])
        self.assertTrue(any('לא נמצא בכרטיס הלקוח שיעור ניסיון בתשלום' in note for note in result['notes']))

    def test_a_second_class_of_the_only_child_is_not_given_a_brothers_discount(self):
        _discount('הנחת ילד שני', '10.00')
        _family, only = self.family_with_child()
        LessonEnrollment.objects.create(child=only, lesson=self.dance_lesson, status='active')
        result = self.quote(ctx=self.identified(only), another_class_same_child=True)
        self.assertFalse(result['based_on']['siblings_registered'])
        self.assertEqual(result['monthly_after']['amount'], '260.00')

    def test_a_fact_that_no_discount_answers_to_is_said_so(self):
        """No "ילד שני" discount is configured here: a brother on a team is then not a discount, and the note says it."""
        result = self.quote(siblings_registered=True, another_class_same_child=True)
        self.assertEqual(result['monthly_after']['amount'], '260.00')
        self.assertTrue(any('לא מוגדרת כרגע הנחת ילד שני' in note for note in result['notes']))
        self.assertTrue(any('לחוג נוסף' in note for note in result['notes']))
        _discount('הנחת ילד שני', '10.00')
        _discount('הנחת שיעור נוסף', '15.00')
        result = self.quote(siblings_registered=True, another_class_same_child=True)
        self.assertEqual(result['monthly_after']['amount'], '235.00')
        self.assertFalse(any('לא מוגדר' in note for note in result['notes']))

    def test_a_track_and_a_chosen_lesson(self):
        track = self.quote(self.older, track_id=str(self.track.id))
        self.assertEqual(track['monthly_after']['amount'], '400.00')
        self.assertEqual(track['track']['name'], 'פעמיים בשבוע')
        self.assertEqual([row['day'] for row in track['lessons']], ['ראשון', 'רביעי'])
        self.assertEqual(track['first_charge']['breakdown'][-1]['amount'], '60.00')        # this class's own fee
        wednesday = self.quote(self.older, lesson_id=str(self.older_wednesday.id))
        self.assertEqual([row['day'] for row in wednesday['lessons']], ['רביעי'])
        self.assertEqual(wednesday['monthly_after']['amount'], '260.00')
        self.assertFalse(self.quote(self.older, lesson_id=str(self.young_lesson.id))['can_quote'])
        self.assertFalse(self.quote(self.older, track_id='nope')['can_quote'])

    def test_a_date_ahead_is_quoted_for_that_day_and_a_date_behind_for_today(self):
        ahead = date.today().replace(day=1) + timedelta(days=62)
        ahead = ahead.replace(day=1)                                   # the first of a month ahead: a full month
        result = self.quote(start_date=ahead.isoformat())
        self.assertEqual(result['as_of'], ahead.strftime('%d.%m.%Y'))
        self.assertEqual(result['first_charge']['breakdown'][0]['amount'], '260.00')
        self.assertIn('חודש מלא', result['first_charge']['breakdown'][0]['label'])
        self.assertTrue(any('החישוב הוא להרשמה ב-' in note for note in result['notes']))
        self.assertEqual(self.quote(start_date='2020-01-01')['as_of'], date.today().strftime('%d.%m.%Y'))

    def test_what_cannot_be_quoted_says_why(self):
        self.assertFalse(self.tool('quote_registration')['can_quote'])
        external = self.quote(self.external_course)
        self.assertEqual((external['can_quote'], external['is_external']), (False, True))
        self.young.show_in_widget = False
        self.young.save(update_fields=['show_in_widget'])
        hidden = self.quote()
        self.assertEqual((hidden['can_quote'], hidden['reason']), (False, 'חוג זה אינו פתוח להרשמה'))
        self.assertIn('אין חישוב', summarize_result('quote_registration', hidden))

    def test_the_age_and_a_full_lesson_are_noted(self):
        self.young_lesson.capacity = 1
        self.young_lesson.save(update_fields=['capacity'])
        _family, child = self.family_with_child()
        LessonEnrollment.objects.create(child=child, lesson=self.young_lesson, status='active')
        result = self.quote(child_age=9)
        self.assertTrue(any('השיעור מלא כרגע' in note for note in result['notes']))
        self.assertTrue(any('מיועד לגיל 4.5-6' in note for note in result['notes']))
        self.assertFalse(any('מיועד ל' in note for note in self.quote(child_age=5)['notes']))
        self.assertFalse(any('מיועד ל' in note for note in self.quote(grade='לא יודעת')['notes']))
        self.assertTrue(any('מיועד לגיל 4.5-6' in note for note in self.quote(grade='כיתה ג')['notes']))


class InstructorAndBranchTest(CourseToolsTestCase):
    def test_an_instructor_by_name(self):
        result = self.tool('instructor_info', name='משה')
        row = result['instructors'][0]
        self.assertEqual((row['name'], row['specialization'], row['photo_url']), ('משה לוי', 'קפוארה', 'https://cdn.example/moshe.jpg'))
        self.assertEqual(row['course_types'], ['היפהופ - מחול', 'קפוארה'])
        self.assertEqual(row['branches'], ['קרל וגרטי קורי 8'])
        self.assertEqual(len(row['lessons']), 4)
        self.assertEqual(set(row), {'name', 'specialization', 'course_types', 'branches', 'photo_url', 'lessons'})
        self.assertEqual(summarize_result('instructor_info', result), 'משה לוי (היפהופ - מחול, קפוארה)')

    def test_the_instructors_of_a_branch_and_one_nobody_knows(self):
        listed = self.tool('instructor_info', branch='ראש העין')
        self.assertEqual([row['name'] for row in listed['instructors']], ['משה לוי'])
        self.assertNotIn('lessons', listed['instructors'][0])
        nobody = self.tool('instructor_info', name='זלמן')
        self.assertEqual(nobody['instructors'], [])
        self.assertIn('לא נמצא מדריך', summarize_result('instructor_info', nobody))
        self.assertEqual(self.tool('instructor_info', branch='נתניה')['instructors'], [])

    def test_a_branch_with_its_fields_its_class_days_and_the_offices_phone(self):
        knowledge_import.run()
        items = knowledge.relevant_items(today=date.today())
        ctx = ToolContext(items=items)
        result = run_tool('branch_info', {'branch': 'קרל וגרטי'}, ctx)
        row = result['branches'][0]
        self.assertEqual((row['address'], row['city'], row['phone'], row['directions']), ('קרל וגרטי קורי 8, ראש העין', 'ראש העין', '03-5550000', 'חניה מאחור, קומה 2'))
        self.assertEqual(row['course_types'], ['היפהופ - מחול', 'קפוארה'])
        self.assertEqual(row['class_days'], [
            {'day': 'ראשון', 'from': '16:00', 'to': '17:00'}, {'day': 'שני', 'from': '17:00', 'to': '18:00'},
            {'day': 'שלישי', 'from': '17:00', 'to': '17:45'}, {'day': 'רביעי', 'from': '16:00', 'to': '17:00'},
        ])
        self.assertEqual(result['office_phone'], OFFICE_PHONE)
        self.assertTrue(ctx.knowledge_used)                           # the office's contact item is named in "למה"
        self.assertEqual(summarize_result('branch_info', result), 'קרל וגרטי קורי 8 (שלנו)')


class NothingOfMoneyThatIsNotAParentsBusinessTest(CourseToolsTestCase):
    """Every tool, on data that holds what must never come out."""

    def setUp(self):
        super().setUp()
        _discount('הנחת ילד שני', '10.00')
        Tag.objects.create(name='ברכת יום הולדת')      # the tag a topic ends with; the tool only marks it
        self.family, self.child = self.family_with_child()
        LessonEnrollment.objects.create(child=self.child, lesson=self.dance_lesson, status='active')
        paid = Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.dance_lesson,
            payment_type='recurring_subscription', status='completed', base_amount=Decimal('280.00'),
            discount_amount=Decimal('0.00'), final_amount=FAMILY_PAID, registration_fee=Decimal('120.00'),
        )
        RecurringPayment.objects.create(
            child=self.child, initial_payment=paid, tranzila_token=CARD_TOKEN, status='active',
            base_amount=Decimal('280.00'), discount_amount=Decimal('0.00'), amount=FAMILY_PAID,
            billing_day=1, start_date=date.today(), next_billing_date=date.today() + timedelta(days=20),
        )
        self.ctx = self.identified(self.child)

    def every_answer(self) -> dict:
        ctx = self.ctx
        calls = [
            ('find_courses', {'city': 'ראש העין'}),
            ('find_courses', {'city': 'ראש העין', 'age': 5, 'course_type': 'קפוארה'}),
            ('find_courses', {'city': 'רמת גן'}),
            ('find_courses', {'city': 'נתניה'}),
            ('course_card', {'course_id': str(self.young.id)}),
            ('course_card', {'course_id': str(self.older.id)}),
            ('course_card', {'course_id': str(self.external_course.id)}),
            ('course_card', {'name': 'קפוארה', 'city': 'ראש העין'}),
            ('quote_registration', {'course_id': str(self.young.id)}),
            ('quote_registration', {'course_id': str(self.young.id), 'took_paid_trial': True, 'siblings_registered': True}),
            ('quote_registration', {'course_id': str(self.older.id), 'track_id': str(self.track.id), 'another_class_same_child': True}),
            ('quote_registration', {'course_id': str(self.external_course.id)}),
            ('instructor_info', {'name': 'משה לוי'}),
            ('instructor_info', {}),
            ('branch_info', {'branch': 'קרל וגרטי'}),
            ('branch_info', {'branch': 'רמת גן'}),
            ('office_hours_now', {}),
            ('customer_card', {}),
            ('request_human', {'reason': 'בדיקה'}),
            ('find_courses', {'city': 'ראש העין', 'show_all': True}),
            ('find_courses', {}),
            ('tag_conversation', {'tag': 'ברכת יום הולדת', 'details': 'נועה, בת, השרון'}),
        ]
        answers = {}
        for name, arguments in calls:
            result = run_tool(name, arguments, ctx)
            self.assertNotIn('error', result, (name, arguments, result))
            answers[f'{name} {json.dumps(arguments, ensure_ascii=False)}'] = result
        return answers

    def test_every_tool_of_the_bot_is_checked_here(self):
        called = {key.split(' ')[0] for key in self.every_answer()}
        self.assertEqual(called, shadow_tools.TOOL_NAMES)

    def test_no_answer_has_a_key_that_names_money_of_the_business_or_of_a_family(self):
        for call, result in self.every_answer().items():
            for key in _keys(result):
                for word in FORBIDDEN_KEYS:
                    self.assertNotIn(word, key.lower(), f'{call}: the key "{key}"')

    def test_no_answer_carries_a_salary_a_cost_a_private_phone_a_token_or_what_a_family_paid(self):
        for call, result in self.every_answer().items():
            text = json.dumps(result, ensure_ascii=False, default=str)
            for value in FORBIDDEN_VALUES:
                self.assertNotIn(value, text, f'{call}: "{value}"')
        # The planted figures are really there to be found.
        self.assertEqual(self.young.instructor_salary_override, SALARY_OF_THE_GROUP)
        self.assertEqual(self.young_lesson.instructor_salary_override, SALARY_OF_THE_LESSON)
        self.assertEqual(RecurringPayment.objects.get().tranzila_token, CARD_TOKEN)

    def test_no_tool_writes_and_none_reaches_out(self):
        counted = (Payment, RecurringPayment, Family, Child, LessonEnrollment, ShadowReply)
        before = [model.objects.count() for model in counted]

        def reading_only(execute, sql, params, many, context):
            verb = sql.lstrip().split(None, 1)[0].upper()
            if verb in ('INSERT', 'UPDATE', 'DELETE'):
                raise AssertionError(f'a tool wrote to the database: {sql[:120]}')
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reading_only):
            self.every_answer()
        self.assertEqual([model.objects.count() for model in counted], before)
        self.assertEqual(self.network.call_count, 0)
        self.deliver.assert_not_called()
        self.handoff.assert_not_called()

    def test_a_tool_that_breaks_answers_with_an_error_line(self):
        with patch('apps.wahub.course_tools.course_card', side_effect=RuntimeError('boom')), self.assertLogs('apps.wahub.shadow_tools', level='ERROR'):
            result = run_tool('course_card', {'course_id': str(self.young.id)}, ToolContext())
        self.assertEqual(result, {'error': 'הכלי נכשל: RuntimeError'})
        self.assertEqual(summarize_result('course_card', result), 'הכלי נכשל: RuntimeError')


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5', WAHUB_SHADOW_EFFORT='medium')
class ClaudeAsksTheNewToolsTest(CourseToolsTestCase):
    def _response(self, payload):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = payload
        return response

    def test_how_much_if_we_register_goes_through_the_quote(self):
        knowledge_import.run()
        self.incoming('כמה יעלה לרשום את הבן שלי, בן 5, לקפוארה בראש העין? הוא היה בשיעור ניסיון', name='רותם')
        turns = [
            [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'find_courses', 'input': {'city': 'ראש העין', 'age': 5, 'course_type': 'קפוארה'}}],
            [
                {'type': 'tool_use', 'id': 'toolu_2', 'name': 'course_card', 'input': {'course_id': str(self.young.id)}},
                {'type': 'tool_use', 'id': 'toolu_3', 'name': 'quote_registration', 'input': {'course_id': str(self.young.id), 'child_age': 5, 'took_paid_trial': True}},
            ],
        ]
        answer = {'text': 'התשלום הראשון ₪X', 'reasoning': 'לפי quote_registration', 'knowledge_ids': []}
        with patch(POST, side_effect=[
            self._response({'model': 'claude-opus-5-5', 'stop_reason': 'tool_use', 'content': turns[0]}),
            self._response({'model': 'claude-opus-5-5', 'stop_reason': 'tool_use', 'content': turns[1]}),
            self._response({'model': 'claude-opus-5-5', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}]}),
        ]) as post:
            reply = shadow.propose(self.contact(PHONE))
        self.assertEqual([tool['name'] for tool in reply.tools_used], ['find_courses', 'course_card', 'quote_registration'])
        self.assertIn('ניסיון בתשלום ב-1', reply.tools_used[0]['summary'])
        self.assertIn('ניסיון ₪30', reply.tools_used[1]['summary'])
        self.assertIn('− קיזוז שיעור ניסיון ששולם ₪30', reply.tools_used[2]['summary'])

        body = post.call_args_list[0].kwargs['json']
        self.assertIn('תמיד מהכלים, אף פעם לא מהזיכרון או מהידע', shadow.system_text(body))
        found = json.loads(post.call_args_list[1].kwargs['json']['messages'][2]['content'][0]['content'])
        self.assertEqual(found['courses'][0]['trial']['price'], '30.00')
        results = post.call_args_list[2].kwargs['json']['messages'][4]['content']
        self.assertEqual([row['tool_use_id'] for row in results], ['toolu_2', 'toolu_3'])
        quoted = json.loads(results[1]['content'])
        self.assertEqual(quoted['first_charge']['breakdown'][-1], {'label': 'קיזוז שיעור ניסיון ששולם', 'amount': '-30.00'})
        self.assertNotIn('is_error', results[1])
