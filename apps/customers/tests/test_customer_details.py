"""The office edits a customer from the child's card — PATCH children/{id}/details/."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.customer_details import SHARED_GHOST_FAMILY_NAME
from apps.customers.models import Child, Family, Parent, Payment

User = get_user_model()

# Valid Israeli ID numbers (check digit included).
ID_A = '000000018'
ID_B = '123456782'
ID_C = '031972540'


def _user(role, username):
    user = User.objects.create_user(username=username, email=username, password='x')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    return user


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return client


class _Card(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        self.other_branch = Branch.objects.create(name='סניף אחר', city=city)
        self.family = Family.objects.create(
            name='כהן', phone='050-777-8899', email='yael@example.com',
            parent_id_number=ID_A, branch=self.branch, address='הרצל 1',
        )
        self.parent = Parent.objects.create(
            family=self.family, first_name='יעל', last_name='כהן',
            phone='050-777-8899', email='yael@example.com', is_primary=True,
        )
        self.child = Child.objects.create(
            family=self.family, first_name='נועה', last_name='כהן', id_number='',
            birth_date=date(2016, 5, 5), gender='female', status='active',
            paid_until_date=date(2026, 10, 1),
        )
        self.manager = _user(UserProfile.ROLE_MANAGER, 'm@test.com')
        self.client = _client_for(self.manager)

    def url(self, child=None):
        return f'/api/v1/customers/children/{(child or self.child).id}/details/'

    def patch(self, body, client=None, child=None):
        return (client or self.client).patch(self.url(child), body, format='json')

    def reload(self):
        self.child.refresh_from_db()
        self.family.refresh_from_db()
        self.parent.refresh_from_db()


class EditChildTests(_Card):
    def test_child_fields_are_saved_and_the_card_row_comes_back(self):
        res = self.patch({'child': {
            'first_name': 'נועה ',
            'last_name': 'כהן-לוי',
            'birth_date': '2016-06-06',
            'gender': 'female',
            'id_number': ID_C,
            'phone_number': '+972 52-111-2233',
            'notes': 'אלרגיה לבוטנים',
        }})
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        self.assertEqual(self.child.last_name, 'כהן-לוי')
        self.assertEqual(self.child.birth_date, date(2016, 6, 6))
        self.assertEqual(self.child.id_number, ID_C)
        self.assertEqual(self.child.phone_number, '0521112233')
        self.assertEqual(self.child.notes, 'אלרגיה לבוטנים')
        # Only what really changed is reported; the trailing space is not a change.
        fields = {c['field'] for c in res.data['changes']}
        self.assertEqual(fields, {'child.last_name', 'child.birth_date', 'child.id_number',
                                  'child.phone_number', 'child.notes'})
        row = res.data['child']
        self.assertEqual(row['last_name'], 'כהן-לוי')
        self.assertEqual(row['notes'], 'אלרגיה לבוטנים')
        self.assertEqual(row['extra_phones'], [])

    def test_status_and_billing_fields_are_not_writable_here(self):
        res = self.patch({'child': {'first_name': 'נועה', 'status': 'inactive', 'paid_until_date': '2030-01-01'}})
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        self.assertEqual(self.child.status, 'active')
        self.assertEqual(self.child.paid_until_date, date(2026, 10, 1))
        self.assertEqual(res.data['changes'], [])

    def test_invalid_values_are_refused_and_nothing_is_saved(self):
        res = self.patch({
            'child': {'first_name': 'שם חדש', 'birth_date': '2099-01-01', 'id_number': '123456789'},
            'parent': {'phone': '12345'},
        })
        self.assertEqual(res.status_code, 400, res.content)
        errors = res.data['errors']
        self.assertIn('child.birth_date', errors)
        self.assertIn('child.id_number', errors)
        self.assertIn('parent.phone', errors)
        self.reload()
        self.assertEqual(self.child.first_name, 'נועה')
        self.assertEqual(self.parent.phone, '050-777-8899')

    def test_required_names_cannot_be_blanked(self):
        res = self.patch({'child': {'first_name': '  '}, 'parent': {'last_name': ''}})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(set(res.data['errors']), {'child.first_name', 'parent.last_name'})


class EditParentTests(_Card):
    def test_a_new_phone_is_written_to_the_parent_and_the_family_together(self):
        res = self.patch({'parent': {'phone': '054-123 4567'}})
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        self.assertEqual(self.parent.phone, '0541234567')
        self.assertEqual(self.family.phone, '0541234567')
        self.assertEqual(res.data['child']['parent_phone'], '0541234567')
        self.assertEqual(res.data['changes'][0]['old'], '050-777-8899')

    def test_the_same_number_typed_differently_is_not_a_change(self):
        res = self.patch({'parent': {'phone': '0507778899', 'email': 'yael@example.com'}})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['changes'], [])
        self.reload()
        self.assertEqual(self.parent.phone, '050-777-8899')   # stored formatting kept

    def test_email_goes_to_both_copies_and_can_be_cleared(self):
        res = self.patch({'parent': {'email': 'new@example.com'}})
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        self.assertEqual(self.parent.email, 'new@example.com')
        self.assertEqual(self.family.email, 'new@example.com')
        self.assertEqual(self.patch({'parent': {'email': 'not-an-email'}}).status_code, 400)
        self.assertEqual(self.patch({'parent': {'email': ''}}).status_code, 200)
        self.reload()
        self.assertEqual((self.parent.email, self.family.email), ('', ''))

    def test_names_and_parent_id(self):
        res = self.patch({'parent': {'first_name': 'יעלי', 'last_name': 'כהן', 'id_number': ID_B}})
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        self.assertEqual(self.parent.first_name, 'יעלי')
        self.assertEqual(self.family.parent_id_number, ID_B)
        self.assertEqual(res.data['child']['parent_first_name'], 'יעלי')
        self.assertEqual(res.data['child']['parent_id_number'], ID_B)

    def test_parent_id_must_be_valid_and_cannot_be_removed(self):
        self.assertEqual(self.patch({'parent': {'id_number': '123456789'}}).status_code, 400)
        res = self.patch({'parent': {'id_number': ''}})
        self.assertEqual(res.status_code, 400)
        self.assertIn('parent.id_number', res.data['errors'])
        self.reload()
        self.assertEqual(self.family.parent_id_number, ID_A)

    def test_a_phone_of_another_family_needs_confirming(self):
        other = Family.objects.create(name='לוי', phone='972541234567', branch=self.branch)
        res = self.patch({'parent': {'phone': '0541234567'}, 'child': {'first_name': 'נויה'}})
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.data['duplicates'][0]['field'], 'parent.phone')
        self.assertIn('לוי', res.data['duplicates'][0]['message'])
        self.reload()
        self.assertEqual(self.parent.phone, '050-777-8899')
        self.assertEqual(self.child.first_name, 'נועה')   # nothing saved while unconfirmed
        res = self.patch({'parent': {'phone': '0541234567'}, 'confirm_duplicates': True})
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        self.assertEqual(self.family.phone, '0541234567')
        other.refresh_from_db()
        self.assertEqual(other.phone, '972541234567')

    def test_a_parent_id_of_another_family_needs_confirming(self):
        Family.objects.create(name='לוי', phone='0500000001', parent_id_number=ID_B.lstrip('0'))
        res = self.patch({'parent': {'id_number': ID_B}})
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.data['duplicates'][0]['field'], 'parent.id_number')

    def test_a_partner_is_not_told_whose_number_it_is(self):
        Family.objects.create(name='משפחה סודית', phone='0541234567', branch=self.other_branch)
        partner = _user(UserProfile.ROLE_PARTNER, 'p@test.com')
        partner.profile.assigned_branches.add(self.branch)
        res = self.patch({'parent': {'phone': '0541234567'}}, client=_client_for(partner))
        self.assertEqual(res.status_code, 409, res.content)
        self.assertNotIn('סודית', res.data['duplicates'][0]['message'])

    def test_a_family_without_a_parent_gets_one(self):
        family = Family.objects.create(name='בלי הורה', phone='0501231231', branch=self.branch)
        child = Child.objects.create(family=family, first_name='דן', last_name='בלי',
                                     birth_date=date(2015, 1, 1), gender='male', status='active')
        res = self.patch({'parent': {'phone': '0501231231'}}, child=child)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(family.parents.exists())             # no change asked, none made
        res = self.patch({'parent': {'first_name': 'רון', 'last_name': 'בלי', 'phone': '0529998877'}}, child=child)
        self.assertEqual(res.status_code, 200, res.content)
        parent = family.parents.get()
        self.assertTrue(parent.is_primary)
        self.assertEqual((parent.first_name, parent.phone), ('רון', '0529998877'))
        family.refresh_from_db()
        self.assertEqual(family.phone, '0529998877')


class ExtraPhonesTests(_Card):
    def test_add_edit_and_remove(self):
        res = self.patch({'extra_phones': [{'name': 'סבתא רחל', 'phone': '052-555-6666'}]})
        self.assertEqual(res.status_code, 200, res.content)
        extra = Parent.objects.get(family=self.family, is_primary=False)
        self.assertEqual((extra.first_name, extra.last_name, extra.phone), ('סבתא', 'רחל', '0525556666'))
        self.assertEqual(res.data['child']['extra_phones'],
                         [{'id': str(extra.id), 'name': 'סבתא רחל', 'phone': '0525556666'}])
        # The primary is untouched.
        self.reload()
        self.assertTrue(self.parent.is_primary)
        self.assertEqual(self.family.phone, '050-777-8899')

        res = self.patch({'extra_phones': [{'id': str(extra.id), 'name': 'סבתא רחל', 'phone': '0525556667'}]})
        self.assertEqual(res.status_code, 200, res.content)
        extra.refresh_from_db()
        self.assertEqual(extra.phone, '0525556667')

        res = self.patch({'extra_phones': []})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(Parent.objects.filter(pk=extra.pk).exists())
        self.assertTrue(Parent.objects.filter(pk=self.parent.pk).exists())

    def test_an_extra_without_a_name_takes_the_parents(self):
        self.patch({'extra_phones': [{'phone': '0525556666'}]})
        extra = Parent.objects.get(family=self.family, is_primary=False)
        self.assertEqual((extra.first_name, extra.last_name), ('יעל', 'כהן'))

    def test_rules(self):
        cases = [
            [{'phone': '050-777-8899'}],                          # the primary's own number
            [{'phone': '0525556666'}, {'phone': '052 555 6666'}],  # twice
            [{'phone': '03-5551234'}],                             # a landline gets no WhatsApp
            [{'phone': ''}],
            [{'id': str(self.parent.id), 'phone': '0525556666'}],  # the primary is not an extra
        ]
        for rows in cases:
            res = self.patch({'extra_phones': rows})
            self.assertEqual(res.status_code, 400, rows)
        self.assertEqual(Parent.objects.filter(family=self.family).count(), 1)

    def test_an_extra_named_on_a_payment_is_not_removed(self):
        extra = Parent.objects.create(family=self.family, first_name='אבא', last_name='כהן', phone='0525556666')
        Payment.objects.create(child=self.child, parent=extra, family=self.family,
                               payment_type='one_time', base_amount=Decimal('100'), final_amount=Decimal('100'), status='completed')
        res = self.patch({'extra_phones': []})
        self.assertEqual(res.status_code, 400, res.content)
        self.assertTrue(Parent.objects.filter(pk=extra.pk).exists())

    def test_a_second_primary_becomes_an_extra(self):
        # An old record with two primaries: the card shows the first as the parent.
        second = Parent.objects.create(family=self.family, first_name='תמר', last_name='כהן',
                                       phone='0525556666', is_primary=True)
        row = self.client.get('/api/v1/customers/children/', {'family': str(self.family.id)}).data['results'][0]
        self.assertEqual(row['parent_phone'], '050-777-8899')
        self.assertEqual([e['id'] for e in row['extra_phones']], [str(second.id)])
        res = self.patch({'extra_phones': row['extra_phones']})
        self.assertEqual(res.status_code, 200, res.content)
        second.refresh_from_db()
        self.assertFalse(second.is_primary)
        self.assertEqual(Parent.objects.filter(family=self.family, is_primary=True).get(), self.parent)


class ScopeAndGuardsTests(_Card):
    def test_worker_is_refused_and_a_partner_only_reaches_their_branch(self):
        worker = _user(UserProfile.ROLE_WORKER, 'w@test.com')
        self.assertEqual(self.patch({'child': {'first_name': 'x'}}, client=_client_for(worker)).status_code, 403)
        partner = _user(UserProfile.ROLE_PARTNER, 'p@test.com')
        partner.profile.assigned_branches.add(self.other_branch)
        as_partner = _client_for(partner)
        self.assertEqual(self.patch({'child': {'first_name': 'x'}}, client=as_partner).status_code, 404)
        partner.profile.assigned_branches.add(self.branch)
        self.assertEqual(self.patch({'child': {'first_name': 'נועם'}}, client=as_partner).status_code, 200)

    def test_the_shared_walk_in_family_is_not_edited_from_a_card(self):
        shared = Family.objects.create(name=SHARED_GHOST_FAMILY_NAME, phone='0000000000', branch=self.branch)
        ghost = Child.objects.create(family=shared, first_name='אורח', last_name='',
                                     birth_date=date(2016, 1, 1), gender='male', status='ghost')
        res = self.patch({'parent': {'first_name': 'x', 'last_name': 'y', 'phone': '0521112233'}}, child=ghost)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(shared.parents.exists())
        # Its own name still can be.
        self.assertEqual(self.patch({'child': {'first_name': 'אורחת'}}, child=ghost).status_code, 200)
        row = self.client.get('/api/v1/customers/children/', {'family': str(shared.id)}).data['results'][0]
        self.assertFalse(row['family_editable'])

    def test_family_fields(self):
        res = self.patch({'family': {'name': 'כהן-לוי', 'address': 'הרצל 2', 'notes': 'להתקשר אחרי 17:00'}})
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        self.assertEqual((self.family.name, self.family.address, self.family.notes),
                         ('כהן-לוי', 'הרצל 2', 'להתקשר אחרי 17:00'))
        self.assertEqual(res.data['child']['family_notes'], 'להתקשר אחרי 17:00')
        self.assertTrue(res.data['child']['family_editable'])
        self.assertEqual(self.patch({'family': {'name': ''}}).status_code, 400)

    def test_branch_is_not_editable_here(self):
        self.patch({'family': {'branch': str(self.other_branch.id)}})
        self.reload()
        self.assertEqual(self.family.branch_id, self.branch.id)

    def test_malformed_payloads(self):
        self.assertEqual(self.patch({'child': 'x'}).status_code, 400)
        self.assertEqual(self.patch({'extra_phones': {'phone': '0521112233'}}).status_code, 400)
        self.assertEqual(self.patch({'extra_phones': ['0521112233']}).status_code, 400)


class ReviewRoundTests(_Card):
    """What the independent review of 29.9 found, pinned."""

    def test_an_id_is_stored_as_typed_without_padding(self):
        # The widget finds a returning parent by the exact string; up to nine digits.
        res = self.patch({'parent': {'id_number': ID_C.lstrip('0')}, 'child': {'id_number': ID_C.lstrip('0')}})
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        self.assertEqual(self.family.parent_id_number, ID_C.lstrip('0'))
        self.assertEqual(self.child.id_number, ID_C.lstrip('0'))
        self.assertEqual(self.patch({'parent': {'id_number': '1234'}}).status_code, 400)

    def test_the_parent_phone_cannot_take_an_extras_number(self):
        Parent.objects.create(family=self.family, first_name='סבתא', last_name='', phone='0525556666')
        res = self.patch({'parent': {'phone': '052-555-6666'}})
        self.assertEqual(res.status_code, 400)
        self.assertIn('parent.phone', res.data['errors'])

    def test_a_list_built_on_a_stale_card_is_refused(self):
        added = Parent.objects.create(family=self.family, first_name='דוד', last_name='', phone='0525556666')
        res = self.patch({'extra_phones': [], 'extra_phone_ids_seen': []})
        self.assertEqual(res.status_code, 400, res.content)
        self.assertTrue(Parent.objects.filter(pk=added.pk).exists())
        res = self.patch({'extra_phones': [], 'extra_phone_ids_seen': [str(added.id)]})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(Parent.objects.filter(pk=added.pk).exists())

    def test_names_of_an_extra(self):
        extra = Parent.objects.create(family=self.family, first_name='סבתא', last_name='רחל', phone='0525556666')
        row = {'id': str(extra.id), 'phone': '0525556666'}
        # One word is the whole name — not topped up with the parent's surname.
        self.assertEqual(self.patch({'extra_phones': [{**row, 'name': 'סבתא'}]}).status_code, 200)
        extra.refresh_from_db()
        self.assertEqual((extra.first_name, extra.last_name), ('סבתא', ''))
        # Cleared: the parent's name, as for an extra added without one — and it says so.
        res = self.patch({'extra_phones': [{**row, 'name': ''}]})
        self.assertEqual([c['field'] for c in res.data['changes']], ['extra_phones'])
        extra.refresh_from_db()
        self.assertEqual((extra.first_name, extra.last_name), ('יעל', 'כהן'))
        # The same again changes nothing.
        self.assertEqual(self.patch({'extra_phones': [{**row, 'name': ''}]}).data['changes'], [])

    def test_an_old_extra_that_fails_todays_rules_does_not_block_the_list(self):
        landline = Parent.objects.create(family=self.family, first_name='בית', last_name='', phone='03-5551234')
        empty = Parent.objects.create(family=self.family, first_name='ישן', last_name='', phone='')
        res = self.patch({'extra_phones': [
            {'id': str(landline.id), 'name': 'בית', 'phone': '03-5551234'},
            {'id': str(empty.id), 'name': 'ישן', 'phone': ''},
            {'name': 'דוד', 'phone': '0521112233'},
        ]})
        self.assertEqual(res.status_code, 200, res.content)
        landline.refresh_from_db()
        self.assertEqual(landline.phone, '03-5551234')
        self.assertEqual(Parent.objects.filter(family=self.family).count(), 4)

    def test_only_a_real_true_confirms_a_duplicate(self):
        Family.objects.create(name='לוי', phone='0541234567', branch=self.branch)
        res = self.patch({'parent': {'phone': '0541234567'}, 'confirm_duplicates': 'false'})
        self.assertEqual(res.status_code, 409)


class MakeExtraPrimaryTests(_Card):
    def test_the_swap_the_card_sends(self):
        # What the card's "הפוך לראשי" sends: the two swap names and phones.
        extra = Parent.objects.create(family=self.family, first_name='דני', last_name='כהן', phone='0525556666')
        res = self.patch({
            'parent': {'first_name': 'דני', 'last_name': 'כהן', 'phone': '0525556666'},
            'extra_phones': [{'id': str(extra.id), 'name': 'יעל כהן', 'phone': '050-777-8899'}],
            'extra_phone_ids_seen': [str(extra.id)],
        })
        self.assertEqual(res.status_code, 200, res.content)
        self.reload()
        extra.refresh_from_db()
        self.assertEqual((self.parent.first_name, self.parent.phone, self.parent.is_primary), ('דני', '0525556666', True))
        self.assertEqual(self.family.phone, '0525556666')
        self.assertEqual((extra.first_name, extra.last_name, extra.phone, extra.is_primary), ('יעל', 'כהן', '0507778899', False))
        # Payment and card links now go to the new number.
        from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
        self.assertEqual(build_enrollment_whatsapp_context(child=self.child)['phone'], '0525556666')
