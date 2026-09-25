"""
Remove security codes (CVV) and full card numbers from stored gateway answers.

Tranzila's REST answer echoes the request (`original_request`), CVV included
for a typed card, and the whole answer was kept in
`tranzila_transactions.response_data` (759 rows on 25.9.2026). New rows are
clean (TranzilaService and TranzilaTransaction.save scrub them); this cleans
the old ones.

    python manage.py scrub_card_data            # count only — changes nothing
    python manage.py scrub_card_data --apply    # rewrite the affected rows

Prints counts only, never a value. Keeps what refunds read back: the expiry,
the saved-card token and the terminal name. Running it twice is harmless.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.card_data import holds_card_data, scrub_card_data
from apps.customers.models import TranzilaTransaction


class Command(BaseCommand):
    help = 'Remove CVVs and full card numbers from stored Tranzila answers (count only without --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Rewrite the affected rows.')
        parser.add_argument('--chunk', type=int, default=500)

    def handle(self, *args, **options):
        apply = options['apply']
        chunk = max(1, options['chunk'])
        scanned = affected = rewritten = 0
        rows = TranzilaTransaction.objects.only('pk', 'request_data', 'response_data').order_by('pk')
        batch = []
        for row in rows.iterator(chunk_size=chunk):
            scanned += 1
            if holds_card_data(row.response_data) or holds_card_data(row.request_data):
                affected += 1
                batch.append(row)
            if apply and len(batch) >= chunk:
                rewritten += self._rewrite(batch)
                batch = []
        if apply and batch:
            rewritten += self._rewrite(batch)

        remaining = sum(
            1 for row in rows.iterator(chunk_size=chunk)
            if holds_card_data(row.response_data) or holds_card_data(row.request_data)
        ) if apply else affected
        mode = 'APPLIED' if apply else 'DRY RUN (nothing changed)'
        self.stdout.write(
            f'{mode}: scanned={scanned} with_card_data={affected} rewritten={rewritten} remaining={remaining}'
        )

    @staticmethod
    def _rewrite(batch) -> int:
        # update(), not save(): only these two columns change, no timestamps move.
        with transaction.atomic():
            for row in batch:
                TranzilaTransaction.objects.filter(pk=row.pk).update(
                    request_data=scrub_card_data(row.request_data),
                    response_data=scrub_card_data(row.response_data),
                )
        return len(batch)
