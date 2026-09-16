"""
The screen that answers "I sent a link — what happened with it?".

`GET /api/v1/customers/card-links/` without `child_id`: every card link and
every standing-order card-update link, across all children, newest first. With
`child_id` it is still exactly the per-child list `SendCardLinkDialog` reads,
and these tests hold that shape still.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.core.models import UserProfile
from apps.customers.models import Child
from apps.payment_links.models import CardLink, CardUpdateLink

from .test_card_link import _Base, _client_for, _user


class OverviewListTests(_Base):
    """The cross-child list: what it contains, and in what order."""

    def _card_update(self, **kwargs):
        defaults = dict(child=self.child, mode='renew', amount=Decimal('500.00'),
                        months=['2026-08', '2026-09'], channel='copy', token='signed~token~1',
                        created_by=self.manager)
        defaults.update(kwargs)
        return CardUpdateLink.objects.create(**defaults)

    def test_without_child_id_it_lists_every_child(self):
        other_family_child = Child.objects.create(
            family=self.family, first_name='Ido', last_name='Cohen',
            birth_date=date(2014, 5, 2), gender='male', status='active',
        )
        mine = self._one_time_link()
        theirs = CardLink.objects.create(
            kind=CardLink.KIND_ONE_TIME, child=other_family_child, amount=Decimal('99.00'),
            description='מדים', branch=self.branch, created_by=self.manager,
        )

        res = self.client.get('/api/v1/customers/card-links/')

        self.assertEqual(res.status_code, 200)
        ids = {row['id'] for row in res.data['results']}
        self.assertEqual(ids, {str(mine.id), str(theirs.id)})
        self.assertEqual(res.data['count'], 2)

    def test_card_update_links_sit_in_the_same_list_as_card_links(self):
        link = self._sto_link()
        update = self._card_update()

        res = self.client.get('/api/v1/customers/card-links/')

        rows = {row['id']: row for row in res.data['results']}
        self.assertEqual(set(rows), {str(link.id), str(update.id)})
        self.assertEqual(rows[str(update.id)]['kind'], 'card_update')
        self.assertEqual(rows[str(update.id)]['mode'], 'renew')
        self.assertEqual(rows[str(link.id)]['kind'], CardLink.KIND_STANDING_ORDER)

    def test_newest_first_across_both_kinds(self):
        old = self._one_time_link()
        CardLink.objects.filter(id=old.id).update(created_at=timezone.now() - timedelta(days=3))
        middle = self._card_update()
        CardUpdateLink.objects.filter(id=middle.id).update(created_at=timezone.now() - timedelta(days=2))
        newest = self._sto_link()

        res = self.client.get('/api/v1/customers/card-links/')

        self.assertEqual(
            [row['id'] for row in res.data['results']],
            [str(newest.id), str(middle.id), str(old.id)],
        )

    def test_a_row_says_who_it_is_for_and_what_happened(self):
        sent = timezone.now() - timedelta(hours=2)
        link = self._one_time_link(status=CardLink.STATUS_COMPLETED, sent_at=sent, completed_at=timezone.now())

        row = next(r for r in self.client.get('/api/v1/customers/card-links/').data['results']
                   if r['id'] == str(link.id))

        self.assertEqual(row['child_name'], 'Noa Cohen')
        self.assertEqual(row['family_name'], 'Cohen')
        self.assertEqual(row['branch_name'], 'Main')
        self.assertEqual(row['branch_id'], str(self.branch.id))
        self.assertEqual(row['kind_label'], 'חיוב חד-פעמי')
        self.assertEqual(row['status'], CardLink.STATUS_COMPLETED)
        self.assertEqual(row['status_label'], 'הושלם')
        self.assertEqual(row['amount'], '150.00')
        self.assertEqual(row['description'], 'חולצה')
        self.assertEqual(row['sent_via'], 'whatsapp')
        self.assertEqual(row['sent_at'], sent.isoformat())
        self.assertIsNone(row['first_opened_at'])
        self.assertTrue(row['completed_at'])
        self.assertEqual(row['created_by_name'], 'm@test.com')

    def test_a_failed_link_carries_its_error_and_a_url_to_send_again(self):
        link = self._sto_link(last_error='הכרטיס נדחה', attempts=2)

        row = next(r for r in self.client.get('/api/v1/customers/card-links/').data['results']
                   if r['id'] == str(link.id))

        self.assertEqual(row['last_error'], 'הכרטיס נדחה')
        self.assertIn(f'/c/{link.token}', row['public_url'])

    def test_a_one_time_link_says_it_was_copied_rather_than_never_sent(self):
        """WhatsApp carries no template for it, so copying it is how it is sent."""
        one_time = self._one_time_link()
        standing = self._sto_link()

        rows = {r['id']: r for r in self.client.get('/api/v1/customers/card-links/').data['results']}

        self.assertEqual(rows[str(one_time.id)]['sent_via'], 'copy')
        self.assertEqual(rows[str(standing.id)]['sent_via'], '')

    def test_a_link_that_is_done_offers_no_url_to_resend(self):
        link = self._one_time_link(status=CardLink.STATUS_COMPLETED)

        row = next(r for r in self.client.get('/api/v1/customers/card-links/').data['results']
                   if r['id'] == str(link.id))

        self.assertEqual(row['public_url'], '')

    def test_a_card_update_row_carries_the_url_the_office_can_copy_again(self):
        update = self._card_update(token='abc~def')

        row = next(r for r in self.client.get('/api/v1/customers/card-links/').data['results']
                   if r['id'] == str(update.id))

        self.assertTrue(row['public_url'].endswith('/update-card/abc~def'))
        self.assertEqual(row['status'], CardUpdateLink.STATUS_CREATED)
        self.assertEqual(row['sent_via'], 'copy')
        self.assertEqual(row['description'], 'חידוש הוראת קבע · אוגוסט 2026, ספטמבר 2026')

    def test_a_card_update_row_shows_what_was_taken_not_what_was_asked(self):
        update = self._card_update(
            status=CardUpdateLink.STATUS_CHARGED, amount=Decimal('500.00'),
            charged_amount=Decimal('250.00'), completed_at=timezone.now(),
        )

        row = next(r for r in self.client.get('/api/v1/customers/card-links/').data['results']
                   if r['id'] == str(update.id))

        self.assertEqual(row['amount'], '250.00')
        self.assertEqual(row['status_label'], 'חויב')
        self.assertEqual(row['public_url'], '')


class OverviewFilterTests(_Base):
    def _card_update(self, **kwargs):
        defaults = dict(child=self.child, mode='card_only', channel='whatsapp',
                        token='tok~1', created_by=self.manager)
        defaults.update(kwargs)
        return CardUpdateLink.objects.create(**defaults)

    def test_kind_narrows_to_one_family_of_link(self):
        one_time = self._one_time_link()
        sto = self._sto_link()
        update = self._card_update()

        def ids(kind):
            return {row['id'] for row in self.client.get(f'/api/v1/customers/card-links/?kind={kind}').data['results']}

        self.assertEqual(ids('one_time'), {str(one_time.id)})
        self.assertEqual(ids('standing_order'), {str(sto.id)})
        self.assertEqual(ids('card_update'), {str(update.id)})

    def test_status_empties_the_table_that_does_not_know_the_word(self):
        pending = self._sto_link()
        self._card_update(status=CardUpdateLink.STATUS_OPENED)

        res = self.client.get('/api/v1/customers/card-links/?status=pending')

        self.assertEqual([row['id'] for row in res.data['results']], [str(pending.id)])

    def test_search_finds_a_child_by_name_in_both_tables(self):
        other = Child.objects.create(
            family=self.family, first_name='Tamar', last_name='Levi',
            birth_date=date(2015, 3, 3), gender='female', status='active',
        )
        mine = self._one_time_link()
        self._card_update(child=other, token='tok~2')

        res = self.client.get('/api/v1/customers/card-links/?q=Noa')

        self.assertEqual([row['id'] for row in res.data['results']], [str(mine.id)])

    def test_paging_walks_back_through_the_merged_list(self):
        rows = []
        for index in range(3):
            link = self._one_time_link(amount=f'{10 + index}.00')
            CardLink.objects.filter(id=link.id).update(created_at=timezone.now() - timedelta(hours=index))
            rows.append(str(link.id))

        first = self.client.get('/api/v1/customers/card-links/?limit=2').data
        second = self.client.get('/api/v1/customers/card-links/?limit=2&offset=2').data

        self.assertEqual([row['id'] for row in first['results']], rows[:2])
        self.assertEqual([row['id'] for row in second['results']], rows[2:])
        self.assertEqual(first['count'], 3)
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])

    def test_only_a_manager_may_read_it(self):
        worker = _user(UserProfile.ROLE_WORKER, 'w@test.com')

        res = _client_for(worker).get('/api/v1/customers/card-links/')

        self.assertEqual(res.status_code, 403)


class PerChildFormUnchangedTests(_Base):
    """
    `SendCardLinkDialog` reads this response. Its shape is frozen: a bare list
    of card links for one child, with no card-update rows mixed in.
    """

    def test_it_is_still_a_bare_list_of_that_child_s_card_links(self):
        link = self._sto_link()
        CardUpdateLink.objects.create(child=self.child, mode='renew', token='tok~x')

        res = self.client.get(f'/api/v1/customers/card-links/?child_id={self.child.id}')

        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.data, list)
        self.assertEqual([row['id'] for row in res.data], [str(link.id)])

    def test_every_key_the_dialog_reads_is_still_there(self):
        link = self._sto_link()

        row = self.client.get(f'/api/v1/customers/card-links/?child_id={self.child.id}').data[0]

        self.assertEqual(
            set(row),
            {
                'id', 'kind', 'status', 'child_id', 'lesson_id', 'bundle_id', 'lesson_label',
                'include_registration_fee', 'amount', 'description', 'branch_id', 'business_id',
                'business_category_id', 'public_url', 'attempts', 'last_error', 'review_reason',
                'payment_id', 'recurring_payment_id', 'sent_at', 'sent_result', 'completed_at',
                'created_at',
            },
        )
        self.assertEqual(row['child_id'], str(self.child.id))
        self.assertEqual(row['lesson_id'], str(link.lesson_id))
