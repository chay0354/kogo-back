# Orphan columns from an old 0011_storeinvoice_tranzila_document migration that was
# superseded by formal_document FK. Django no longer maps them, so INSERTs fail with
# NOT NULL violations on pending website checkout invoices.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0011_storeinvoice_formal_document'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE store_invoices
                    DROP COLUMN IF EXISTS tranzila_doc_id,
                    DROP COLUMN IF EXISTS tranzila_retrieval_key,
                    DROP COLUMN IF EXISTS tranzila_document_number,
                    DROP COLUMN IF EXISTS pdf_url,
                    DROP COLUMN IF EXISTS tranzila_issued;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
