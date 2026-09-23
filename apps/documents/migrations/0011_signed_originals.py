"""The signed original of every fiscal document, and whether a check was crossed.

signed_originals is a new table (apps/documents/signing): the PDF signed once at
issue, its SHA-256, the key and certificate that signed it, and where the
original went (email, paper, held). document_payments.check_crossed says a check
was crossed "לא סחיר" in the customer's name — the one kind of check הוראה
18ב(ד) lets a signed document be mailed for. Nullable, so the previous code,
which inserts payments without it, keeps working while Vercel's build migrates.
Additive only; nothing existing is altered.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('documents', '0010_series_opening'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentpayment',
            name='check_crossed',
            field=models.BooleanField(blank=True, default=False, null=True, verbose_name="צ'ק משורטט לא סחיר על שם הלקוח"),
        ),
        migrations.CreateModel(
            name='SignedOriginal',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('number', models.CharField(max_length=50, unique=True, verbose_name='מספר מסמך')),
                ('kind', models.CharField(choices=[('ir', 'קבלת חוג'), ('store', 'מכירת חנות'), ('formal', 'מסמך')], max_length=10, verbose_name='סוג מקור')),
                ('source_id', models.CharField(max_length=64, verbose_name='מזהה המקור')),
                ('channel', models.CharField(blank=True, choices=[('', 'לא נשלח על ידי המערכת'), ('ir', 'מייל קבלת חוג'), ('store', 'מייל חנות האתר'), ('credit_note', 'מייל הודעת זיכוי'), ('rental', 'מייל קבלת שכירות')], default='', max_length=20, verbose_name='ערוץ שליחה')),
                ('pdf', models.BinaryField(blank=True, null=True, verbose_name='המקור החתום (PDF)')),
                ('sha256', models.CharField(blank=True, default='', max_length=64, verbose_name='SHA-256 של הקובץ')),
                ('size', models.PositiveIntegerField(default=0, verbose_name='גודל בבתים')),
                ('key_id', models.CharField(blank=True, default='', max_length=300, verbose_name='מפתח החתימה')),
                ('cert_fingerprint', models.CharField(blank=True, default='', max_length=64, verbose_name='טביעת אצבע של התעודה')),
                ('signed_at', models.DateTimeField(blank=True, null=True, verbose_name='מועד החתימה')),
                ('sign_attempts', models.PositiveSmallIntegerField(default=0, verbose_name='ניסיונות חתימה')),
                ('delivery', models.CharField(choices=[('email', 'במייל'), ('paper', 'למסירה על נייר'), ('held', 'ממתין'), ('none', 'לא נשלח')], default='held', max_length=10, verbose_name='מסירה')),
                ('delivery_reason', models.CharField(blank=True, default='', max_length=300, verbose_name='סיבה')),
                ('document_type_label', models.CharField(blank=True, default='', max_length=50, verbose_name='סוג מסמך')),
                ('customer_name', models.CharField(blank=True, default='', max_length=200, verbose_name='שם הלקוח')),
                ('document_date', models.DateField(blank=True, null=True, verbose_name='תאריך המסמך')),
                ('total', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='סה"כ')),
                ('email_to', models.CharField(blank=True, default='', max_length=254, verbose_name='נשלח אל')),
                ('sent_at', models.DateTimeField(blank=True, null=True, verbose_name='נשלח במייל')),
                ('send_attempts', models.PositiveSmallIntegerField(default=0, verbose_name='ניסיונות שליחה')),
                ('last_error', models.CharField(blank=True, default='', max_length=300, verbose_name='שגיאה אחרונה')),
                ('paper_original_printed_at', models.DateTimeField(blank=True, null=True, verbose_name='המקור הודפס')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='נוצר')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='עודכן')),
                ('paper_original_printed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='מי הדפיס את המקור')),
            ],
            options={
                'verbose_name': 'מקור חתום',
                'verbose_name_plural': 'מקורות חתומים',
                'db_table': 'signed_originals',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['kind', 'source_id'], name='signed_orig_source_idx'), models.Index(fields=['delivery', 'paper_original_printed_at'], name='signed_orig_delivery_idx'), models.Index(fields=['signed_at'], name='signed_orig_signed_at_idx')],
            },
        ),
        migrations.AddConstraint(
            model_name='signedoriginal',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('pdf__isnull', True), ('signed_at__isnull', True)), models.Q(('pdf__isnull', False), ('signed_at__isnull', False), ('size__gt', 0), models.Q(('sha256', ''), _negated=True)), _connector='OR'), name='signed_original_bytes_with_signature'),
        ),
    ]
