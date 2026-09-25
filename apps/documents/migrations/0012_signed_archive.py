"""Signed archive copies of documents issued before signing, and a log of every stored file handed out.

signed_originals.purpose says what a stored file is: the one original signed at
issue, or an archive copy — a document issued before signing existed, drawn
again from its record, marked "העתק לארכיון" and signed only to be kept. It is
nullable, like document_payments.check_crossed in 0011: Vercel migrates the
production database while the previous code still serves, and that code inserts
originals without the column. Existing rows get 'original' (the column is added
with that default, which Django then drops); a row the previous code inserts
during the deploy is NULL, and the code reads NULL as an original.

backup_at / backup_error record the copy in the locked backup bucket
(signing/backup.py); nullable for the same reason.

signed_file_access is a new table: one line per download or export of a stored
signed file. Additive only; nothing existing is altered.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('documents', '0011_signed_originals'),
    ]

    operations = [
        migrations.AddField(
            model_name='signedoriginal',
            name='purpose',
            field=models.CharField(blank=True, choices=[('original', 'מקור'), ('archive', 'העתק לארכיון')], db_index=True, default='original', max_length=10, null=True, verbose_name='מהות הקובץ'),
        ),
        migrations.AddField(
            model_name='signedoriginal',
            name='backup_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='גובה לאחסון הנעול'),
        ),
        migrations.AddField(
            model_name='signedoriginal',
            name='backup_error',
            field=models.CharField(blank=True, default='', max_length=300, null=True, verbose_name='שגיאת גיבוי אחרונה'),
        ),
        migrations.CreateModel(
            name='SignedFileAccess',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[('download', 'הורדה'), ('export', 'ייצוא')], max_length=10, verbose_name='פעולה')),
                ('at', models.DateTimeField(auto_now_add=True, verbose_name='מתי')),
                ('ip', models.GenericIPAddressField(blank=True, null=True, verbose_name='כתובת IP')),
                ('original', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='accesses', to='documents.signedoriginal', verbose_name='הקובץ החתום')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='משתמש')),
            ],
            options={
                'verbose_name': 'גישה לקובץ חתום',
                'verbose_name_plural': 'גישות לקבצים חתומים',
                'db_table': 'signed_file_access',
                'ordering': ['-at'],
            },
        ),
    ]
