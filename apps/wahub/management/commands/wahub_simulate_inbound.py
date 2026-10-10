import random
import time

from django.core.management.base import BaseCommand

from apps.wahub import demo


class Command(BaseCommand):
    help = (
        'Local only: every few seconds an invented WhatsApp message comes in, and sometimes '
        'a bot reply after it — through the same function the ManyChat endpoint uses. '
        'For watching the screen update without a refresh.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--every', type=float, default=8, help='Seconds between messages (default 8).')
        parser.add_argument('--count', type=int, default=30, help='How many messages to send (default 30).')
        parser.add_argument('--seed', type=int, default=None, help='Fix the random choices, for a repeatable run.')

    def handle(self, *args, **options):
        database = demo.ensure_local_database()
        rng = random.Random(options['seed'])
        every, count = max(0.0, options['every']), max(0, options['count'])
        self.stdout.write(f'{database}: {count} הודעות, אחת כל {every:g} שניות. Ctrl+C לעצירה.')
        stored = 0
        for index in range(count):
            phone, text, result = demo.simulate_one(rng)
            stored += int(result.stored)
            self.stdout.write(f'[{index + 1}/{count}] {phone} ← "{text}"' + ('' if result.stored else f' (לא נשמר: {result.reason})'))
            if result.stored and rng.random() < 0.6:
                time.sleep(min(2.0, every / 2))
                reply = demo.simulate_bot_reply(phone, rng)
                self.stdout.write('        הבוט ענה' if reply.stored else f'        תשובת הבוט לא נשמרה: {reply.reason}')
            if index + 1 < count:
                time.sleep(every)
        self.stdout.write(self.style.SUCCESS(f'נשמרו {stored} הודעות נכנסות.'))
