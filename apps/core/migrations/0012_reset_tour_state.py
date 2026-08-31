from django.db import migrations


def reset_tour(apps, schema_editor):
    """
    Put every account back to "has not seen the tour".

    The tour gained the walk-in and dashboard steps, so people who went through
    the earlier version never saw them. Clearing the counter makes the next
    sign-in the mandatory run again, followed by two skippable ones — the same
    sequence a new instructor gets.

    Only the two tour columns are touched. Nothing else on the profile moves.
    """
    UserProfile = apps.get_model('core', 'UserProfile')
    UserProfile.objects.update(login_count=0, tour_completed_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_user_profile_tour_state'),
    ]

    operations = [
        # Not reversible: the previous counts are not recorded anywhere, and
        # re-showing the tour is harmless where undoing it would not be.
        migrations.RunPython(reset_tour, migrations.RunPython.noop),
    ]
