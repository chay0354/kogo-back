from django.db import migrations, models

from apps.courses.models import _next_course_display_id


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0015_lessonpriceoption_age_range'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "CREATE SEQUENCE course_display_id_seq;\n"
                        "ALTER TABLE courses ADD COLUMN display_id integer NOT NULL "
                        "DEFAULT nextval('course_display_id_seq');\n"
                        "ALTER SEQUENCE course_display_id_seq OWNED BY courses.display_id;\n"
                        "ALTER TABLE courses ADD CONSTRAINT courses_display_id_key UNIQUE (display_id);\n"
                    ),
                    reverse_sql=(
                        "ALTER TABLE courses DROP CONSTRAINT courses_display_id_key;\n"
                        "ALTER TABLE courses DROP COLUMN display_id;\n"
                        "DROP SEQUENCE IF EXISTS course_display_id_seq;\n"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='course',
                    name='display_id',
                    field=models.PositiveIntegerField(
                        default=_next_course_display_id,
                        editable=False,
                        unique=True,
                        verbose_name='מספר קבוצה',
                        help_text='מספר סידורי קצר לזיהוי הקבוצה (לא מפתח ראשי), מוצג לצד שם הקבוצה בממשק.',
                    ),
                ),
            ],
        ),
    ]
