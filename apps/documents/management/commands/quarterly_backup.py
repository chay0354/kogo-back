"""
The quarterly backup of the books (הוראות ניהול פנקסי חשבונות 25(ו)(2)).

    python manage.py quarterly_backup                    # the quarter that ended last, to the bucket
    python manage.py quarterly_backup --quarter 2026-Q3
    python manage.py quarterly_backup --out ./backups    # also (or only, with no bucket) to a directory

Reads the database and writes nothing to it. Writes the files to the Google
Cloud Storage bucket named by SIGNING_QUARTERLY_BACKUP_BUCKET, create-only, with
the signing service account's credentials (SIGNING_GCP_SA_KEY_JSON outside
Vercel). With no bucket configured, --out is required and the files are only
written there. See apps/documents/quarterly_backup.py for what the folder holds.

Exits non-zero when any part could not be built or uploaded.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.documents.quarterly_backup import BackupInputError, BackupNotConfigured, run_quarterly_backup


class Command(BaseCommand):
    help = 'Back up the fiscal tables, the register and the uniform-structure files of a quarter (25(ו)(2)).'

    def add_arguments(self, parser):
        parser.add_argument('--quarter', help='YYYY-Qn. Default: the quarter that ended last.')
        parser.add_argument('--out', help='Also write the files under this directory (required with no bucket).')

    def handle(self, *args, **options):
        try:
            result = run_quarterly_backup(options.get('quarter'), out_dir=options.get('out'))
        except (BackupInputError, BackupNotConfigured) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Quarter: {result['quarter']}  folder: {result['prefix']}")
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
            raise CommandError('The quarterly backup is incomplete — see the errors above.')
        self.stdout.write(self.style.SUCCESS('Quarterly backup complete.'))
