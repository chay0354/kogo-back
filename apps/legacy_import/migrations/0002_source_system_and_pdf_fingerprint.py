"""
Documents from any software: `source_system` in the key, and the PDF's fingerprint.

Safe for the code that is already running. A Vercel build of a back branch
migrates the production database while production's code — which knows none of
these columns — keeps serving, so:

* Every new column either allows NULL or has a DEFAULT *in the database*, and
  the default stays there. Django 4.2's AddField would add a default and drop it
  again straight away (it has no db_default), leaving NOT NULL columns the old
  code's INSERTs do not fill. So the columns are added with RunSQL, and the
  state operations tell Django the same fields exist (SeparateDatabaseAndState).
  A constant DEFAULT is a catalogue change in PostgreSQL 11+: no table rewrite.
* Every existing row is the previous software's (Tazman's) — the only source
  there was — so `source_system` defaults to 'tazman', and the old code's rows
  keep getting it.
* The new unique key (source_system, doc_type, number) is added before the old
  (doc_type, number) is dropped. Existing rows satisfy it by construction (the
  old key is stricter), so adding it cannot fail; and every row the old code
  writes has source_system 'tazman', so for the old code the new key is exactly
  the old one.

Reverse: drops the columns and restores the old key. It fails, on purpose, when
two softwares' documents share a (type, number) — delete one software's rows
first; that is a decision, not something a migration may make.
"""
from django.db import migrations, models

FORWARD = [
    """ALTER TABLE "legacy_imports" ADD COLUMN "source_system" varchar(40) DEFAULT 'tazman' NOT NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "source_system" varchar(40) DEFAULT 'tazman' NOT NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "original_number" varchar(40) DEFAULT '' NOT NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "amount_before_vat" numeric(12, 2) NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "vat_amount" numeric(12, 2) NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "allocation_number" varchar(40) DEFAULT '' NOT NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "linked_document" varchar(60) DEFAULT '' NOT NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "pdf_sha256" varchar(64) DEFAULT '' NOT NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "pdf_size" bigint NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "pdf_file_name" varchar(255) DEFAULT '' NOT NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "pdf_object" varchar(400) DEFAULT '' NOT NULL""",
    """ALTER TABLE "legacy_documents" ADD COLUMN "pdf_attached_at" timestamp with time zone NULL""",
    # The new key first, then the old one goes.
    """ALTER TABLE "legacy_documents" ADD CONSTRAINT "legacy_document_source_type_number_unique" """
    """UNIQUE ("source_system", "doc_type", "number")""",
    """ALTER TABLE "legacy_documents" DROP CONSTRAINT "legacy_document_type_number_unique\"""",
]

BACKWARD = [
    """ALTER TABLE "legacy_documents" ADD CONSTRAINT "legacy_document_type_number_unique" UNIQUE ("doc_type", "number")""",
    """ALTER TABLE "legacy_documents" DROP CONSTRAINT "legacy_document_source_type_number_unique\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "pdf_attached_at\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "pdf_object\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "pdf_file_name\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "pdf_size\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "pdf_sha256\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "linked_document\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "allocation_number\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "vat_amount\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "amount_before_vat\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "original_number\"""",
    """ALTER TABLE "legacy_documents" DROP COLUMN "source_system\"""",
    """ALTER TABLE "legacy_imports" DROP COLUMN "source_system\"""",
]

STATE = [
    migrations.AddField(
        model_name='legacyimport',
        name='source_system',
        field=models.CharField(default='tazman', max_length=40, verbose_name='תוכנת המקור'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='source_system',
        field=models.CharField(default='tazman', max_length=40, verbose_name='תוכנת המקור'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='original_number',
        field=models.CharField(blank=True, default='', max_length=40, verbose_name='המספר כפי שהודפס'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='amount_before_vat',
        field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='סכום לפני מע"מ'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='vat_amount',
        field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='מע"מ'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='allocation_number',
        field=models.CharField(blank=True, default='', max_length=40, verbose_name='מספר הקצאה'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='linked_document',
        field=models.CharField(blank=True, default='', max_length=60, verbose_name='מסמך מקושר'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='pdf_sha256',
        field=models.CharField(blank=True, default='', max_length=64, verbose_name='טביעת ה-PDF'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='pdf_size',
        field=models.BigIntegerField(blank=True, null=True, verbose_name='גודל ה-PDF'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='pdf_file_name',
        field=models.CharField(blank=True, default='', max_length=255, verbose_name='שם קובץ ה-PDF'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='pdf_object',
        field=models.CharField(blank=True, default='', max_length=400, verbose_name='ה-PDF באחסון הנעול'),
    ),
    migrations.AddField(
        model_name='legacydocument',
        name='pdf_attached_at',
        field=models.DateTimeField(blank=True, null=True, verbose_name='מועד צירוף ה-PDF'),
    ),
    migrations.AddConstraint(
        model_name='legacydocument',
        constraint=models.UniqueConstraint(
            fields=('source_system', 'doc_type', 'number'), name='legacy_document_source_type_number_unique',
        ),
    ),
    migrations.RemoveConstraint(
        model_name='legacydocument',
        name='legacy_document_type_number_unique',
    ),
]


class Migration(migrations.Migration):

    dependencies = [
        ('legacy_import', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(sql=FORWARD, reverse_sql=BACKWARD)],
            state_operations=STATE,
        ),
    ]
