"""
Pre-flight check for the Tranzila configuration.

Run this after swapping test credentials for live ones:
    python manage.py check_tranzila

Exits non-zero when a blocking check fails, so it can gate a deploy.
"""
from django.core.management.base import BaseCommand

from apps.core.tranzila_service import TranzilaService


class Command(BaseCommand):
    help = 'Verify the Tranzila credentials, terminal handshake and webhook configuration'

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Also fail on non-blocking warnings (recommended before going live)',
        )

    def handle(self, *args, **options):
        report = TranzilaService().live_readiness()

        self.stdout.write(f"terminal:    {report['terminal'] or '(missing)'}")
        self.stdout.write(f"environment: {report['environment']}")
        self.stdout.write('')

        warnings = 0
        for check in report['checks']:
            if check['ok']:
                marker, style = 'PASS', self.style.SUCCESS
            elif check['blocking']:
                marker, style = 'FAIL', self.style.ERROR
            else:
                marker, style = 'WARN', self.style.WARNING
                warnings += 1
            self.stdout.write(style(f"[{marker}] {check['name']}: {check['detail']}"))

        self.stdout.write('')
        if not report['ready']:
            raise SystemExit(
                self.style.ERROR(
                    'NOT ready for live payments. Blocking: '
                    + ', '.join(report['blocking_failures'])
                )
            )
        if options['strict'] and warnings:
            raise SystemExit(self.style.ERROR(f'{warnings} warning(s) with --strict.'))

        self.stdout.write(self.style.SUCCESS('Ready to accept payments.'))
