from django.db import migrations


def reset_tour(apps, schema_editor):
    """
    Put every account back to "has not seen the tour", a second time.

    The tour changed again after 0012: the card no longer runs off the bottom
    of a phone, and a day with no lessons now borrows a real one instead of
    showing the attendance steps against an empty screen. Anyone who went
    through it in between met the broken version, so everybody starts over —
    the next sign-in is the mandatory run, then two skippable ones.

    0012 did the same thing and has already been applied, so it will not run
    again; this is a separate migration rather than an edit to that one.

    Only the two tour columns are touched. Nothing else on the profile moves.
    """
    UserProfile = apps.get_model('core', 'UserProfile')
    UserProfile.objects.update(login_count=0, tour_completed_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_linked_user_access'),
    ]

    operations = [
        # Not reversible: the previous counts are not recorded anywhere, and
        # re-showing the tour is harmless where undoing it would not be.
        migrations.RunPython(reset_tour, migrations.RunPython.noop),
    ]
