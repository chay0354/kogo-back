# Generated manually to move snapshot models from instructors to core

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
        ('instructors', '0001_initial'),
        ('courses', '0001_initial'),
    ]

    operations = [
        # Create models in Django's migration state, but don't create the actual tables
        # because they already exist from instructors.0001_initial
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
            name='InstructorMonthlySnapshot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('month', models.CharField(max_length=7, verbose_name='חודש')),
                ('total_lessons', models.PositiveIntegerField(default=0, verbose_name='סה״כ שיעורים')),
                ('total_students', models.PositiveIntegerField(default=0, verbose_name='סה״כ תלמידים')),
                ('total_revenue', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='סה״כ הכנסות')),
                ('total_salary', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='סה״כ שכר')),
                ('profit', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='רווח')),
                ('cancelled_count', models.PositiveIntegerField(default=0, verbose_name='שיעורים שבוטלו')),
                ('avg_attendance_rate', models.DecimalField(decimal_places=2, default=0, max_digits=5, verbose_name='אחוז נוכחות ממוצע')),
                ('lesson_count', models.PositiveIntegerField(default=0, verbose_name='מספר שיעורים שהתרחשו')),
                ('payment_per_lesson', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='תשלום לשיעור (צילום)')),
                ('is_finalized', models.BooleanField(default=False, verbose_name='חודש סופי')),
                ('calculated_at', models.DateTimeField(auto_now=True, verbose_name='חושב בתאריך')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')),
                ('instructor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='monthly_snapshots', to='instructors.instructor', verbose_name='מדריך')),
            ],
            options={
                'verbose_name': 'צילום חודשי - מדריך',
                'verbose_name_plural': 'צילומים חודשיים - מדריכים',
                'db_table': 'instructor_monthly_snapshots',
                'ordering': ['-month', 'instructor'],
                'unique_together': {('instructor', 'month')},
            },
        ),
        migrations.CreateModel(
            name='LessonMonthlySnapshot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('month', models.CharField(max_length=7, verbose_name='חודש')),
                ('enrolled_students', models.PositiveIntegerField(default=0, verbose_name='תלמידים רשומים')),
                ('revenue', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='הכנסות')),
                ('instructor_salary', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='שכר מדריך')),
                ('profit', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='רווח')),
                ('is_finalized', models.BooleanField(default=False, verbose_name='חודש סופי')),
                ('calculated_at', models.DateTimeField(auto_now=True, verbose_name='חושב בתאריך')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lesson_snapshots', to='core.branch', verbose_name='סניף')),
                ('course', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lesson_snapshots', to='courses.course', verbose_name='חוג')),
                ('instructor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lesson_snapshots', to='instructors.instructor', verbose_name='מדריך')),
                ('lesson', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='monthly_snapshots', to='courses.lesson', verbose_name='שיעור')),
            ],
            options={
                'verbose_name': 'צילום חודשי - שיעור',
                'verbose_name_plural': 'צילומים חודשיים - שיעורים',
                'db_table': 'lesson_monthly_snapshots',
                'ordering': ['-month', 'lesson'],
                'unique_together': {('lesson', 'month')},
            },
        ),
        migrations.CreateModel(
            name='BranchMonthlySnapshot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('month', models.CharField(max_length=7, verbose_name='חודש')),
                ('total_students', models.PositiveIntegerField(default=0, verbose_name='סה״כ תלמידים')),
                ('total_revenue', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='סה״כ הכנסות')),
                ('instructor_costs', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='עלויות מדריכים')),
                ('profit', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='רווח')),
                ('active_courses_count', models.PositiveIntegerField(default=0, verbose_name='חוגים פעילים')),
                ('is_finalized', models.BooleanField(default=False, verbose_name='חודש סופי')),
                ('calculated_at', models.DateTimeField(auto_now=True, verbose_name='חושב בתאריך')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='monthly_snapshots', to='core.branch', verbose_name='סניף')),
            ],
            options={
                'verbose_name': 'צילום חודשי - סניף',
                'verbose_name_plural': 'צילומים חודשיים - סניפים',
                'db_table': 'branch_monthly_snapshots',
                'ordering': ['-month', 'branch'],
                'unique_together': {('branch', 'month')},
            },
        ),
            ],
            database_operations=[
                # No database operations needed - tables already exist
            ],
        ),
    ]
