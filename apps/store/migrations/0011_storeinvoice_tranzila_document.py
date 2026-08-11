from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0010_storeinvoice_customer_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='storeinvoice',
            name='tranzila_doc_id',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='מזהה מסמך טרנזילה'),
        ),
        migrations.AddField(
            model_name='storeinvoice',
            name='tranzila_retrieval_key',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='מפתח אחזור טרנזילה'),
        ),
        migrations.AddField(
            model_name='storeinvoice',
            name='tranzila_document_number',
            field=models.CharField(blank=True, default='', max_length=50, verbose_name='מספר מסמך טרנזילה'),
        ),
        migrations.AddField(
            model_name='storeinvoice',
            name='pdf_url',
            field=models.URLField(blank=True, default='', verbose_name='קישור PDF טרנזילה'),
        ),
        migrations.AddField(
            model_name='storeinvoice',
            name='tranzila_issued',
            field=models.BooleanField(default=False, verbose_name='הופק בטרנזילה'),
        ),
    ]
