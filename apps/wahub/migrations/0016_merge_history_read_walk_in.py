"""
Two migrations numbered 0015 reached the production database on 14.10.2026 — the moment a copied conversation was
read again (0015_history_read, applied by a Preview build of its branch) and the walk-ins' source
(0015_contact_source_walk_in, from main). Nothing to do but join them.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('wahub', '0015_contact_source_walk_in'),
        ('wahub', '0015_history_read'),
    ]

    operations = []
