"""
"הודפסו לאחרונה" — a printed paper original stays in sight (audit M12, 30.9.2026).

The hand-delivery list shows originals not yet printed; once printed a row left
it on the next reload with no trace. The originals list now takes
order=printed, so the tab lists the last printed first — however old the
document — and the office can send a copy of any of them.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.documents.models import SignedOriginal
from apps.documents.tests.signing_support import signing_on
from apps.documents.tests.test_signing_delivery import ReceiptsMixin

ORIGINALS = '/api/v1/documents/signing/originals/'


@signing_on()
class PrintedRecentlyTests(ReceiptsMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def test_the_last_printed_come_first(self):
        oldest, middle, newest = (self.receipt('מזומן') for _ in range(3))
        # Printed in another order than issued: the first document was printed last.
        for doc, hours in ((oldest, 1), (middle, 5), (newest, 3)):
            response = self.client.post(f'{ORIGINALS}{self.row(doc).pk}/print-original/')
            self.assertEqual(response.status_code, 200)
            SignedOriginal.objects.filter(number=doc.document_number).update(
                paper_original_printed_at=timezone.now() - timedelta(hours=hours),
            )
        page = self.client.get(ORIGINALS, {'delivery': 'paper', 'printed': 'true', 'order': 'printed'}).json()
        self.assertEqual([row['number'] for row in page['results']],
                         [oldest.document_number, newest.document_number, middle.document_number])
        self.assertTrue(all(row['paper_original_printed_at'] for row in page['results']))
        # The list to print from no longer holds them.
        self.assertEqual(self.client.get(ORIGINALS, {'delivery': 'paper', 'printed': 'false'}).json()['count'], 0)

    def test_without_order_the_list_is_as_before_and_an_unknown_order_is_refused(self):
        first, second = self.receipt('מזומן'), self.receipt('מזומן')
        page = self.client.get(ORIGINALS, {'delivery': 'paper'}).json()
        self.assertEqual([row['number'] for row in page['results']],
                         [second.document_number, first.document_number])
        self.assertEqual(self.client.get(ORIGINALS, {'order': 'sha256'}).status_code, 400)
