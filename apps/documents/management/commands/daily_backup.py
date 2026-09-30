"""
The daily copy of the books to Israel (apps/documents/quarterly_backup.run_daily_backup).

    python manage.py daily_backup                  # to the bucket
    python manage.py daily_backup --out ./backups  # also (or only, with no bucket) to a directory

Reads the database and writes nothing to it. Exits non-zero when a part could
not be built or uploaded.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.documents.quarterly_backup import BackupNotConfigured, run_daily_backup


class Command(BaseCommand):
    help = 'Copy every fiscal table to the backup bucket in Israel (one folder a run).'

    def add_arguments(self, parser):
        parser.add_argument('--out', help='Also write the files under this directory (required with no bucket).')

    def handle(self, *args, **options):
        try:
            result = run_daily_backup(out_dir=options.get('out'))
        except BackupNotConfigured as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Day: {result['day']}  folder: {result['prefix']}  ({result['seconds']}s)")
        for part in result['parts']:
            tables = part.get('tables')
            rows = f"  ({sum(tables.values())} rows in {len(tables)} tables)" if tables else ''
            self.stdout.write(f"  {part['name']}  {part['size']} bytes  sha256 {part['sha256']}{rows}")
        if result['local_dir']:
            self.stdout.write(f"Written to: {result['local_dir']}")
        if result['bucket']:
            self.stdout.write(f"Uploaded to gs://{result['bucket']}/{result['prefix']}/: "
                              f"{len(result['uploaded'])} of {len(result['parts'])} files")
        for error in result['errors']:
            self.stderr.write(f'  ! {error}')
        if not result['ok']:
            raise CommandError('The daily backup is incomplete — see the errors above.')
        self.stdout.write(self.style.SUCCESS('Daily backup complete.'))
