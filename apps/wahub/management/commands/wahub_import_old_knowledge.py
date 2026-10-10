from django.core.management.base import BaseCommand

from apps.wahub import knowledge_import
from apps.wahub.knowledge import KIND_LABELS


class Command(BaseCommand):
    help = (
        'Files the old bot\'s knowledge (docs/bot-knowledge/, mapped by report 00 and the owner\'s '
        'decisions in 04) into the bot\'s knowledge. Runs once; a second run adds nothing '
        '(idempotent by source_note). Sends nothing and touches no customer data.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--docs', default='', help='Folder with 01–03 to check the verbatim phrasings against (default: ../docs/bot-knowledge).')
        parser.add_argument('--dry-run', action='store_true', help='Only report what would be created.')

    def handle(self, *args, **options):
        result = knowledge_import.run(docs_dir=options['docs'] or None, dry_run=options['dry_run'])
        prefix = 'היה נוצר' if options['dry_run'] else 'נוצר'
        for kind, count in sorted(result['created'].items()):
            self.stdout.write(f'{prefix}: {count} × {KIND_LABELS.get(kind, kind)}')
        self.stdout.write(self.style.SUCCESS(
            f'סה"כ {result["created_total"]} רשומות ({result["inactive"]} מהן לא פעילות — החלטות פתוחות או תאריכים שעברו); '
            f'{result["skipped"]} כבר היו; תגיות חדשות: {result["tags"]}; תשובות מוכנות חדשות: {result["quick_replies"]}.'
        ))
        if result['docs_found']:
            self.stdout.write(f'נבדקו {result["verbatim_checked"]} נוסחים מילה במילה מול המסמכים.')
            for key in result['verbatim_mismatch']:
                self.stdout.write(self.style.WARNING(f'  הנוסח "{key}" לא נמצא מילה במילה במסמכים — לבדוק.'))
        else:
            self.stdout.write(self.style.WARNING('תיקיית המסמכים לא נמצאה; הנוסחים לא נבדקו מולה.'))
        self.stdout.write('לא יובאו (לפי טבלת המיפוי וההחלטות):')
        for source, what, why in result['dropped']:
            self.stdout.write(f'  {source}: {what} — {why}')
