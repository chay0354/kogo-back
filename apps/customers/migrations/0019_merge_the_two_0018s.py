"""
Two migrations took the number 0018 on the same day, on two branches that
never saw each other: `0018_card_replacement` (the card-replacement table and
two reminder columns) and `0018_own_outstanding_model_edits` (the index rename
and the help_text no-op). Each was clean against main when it was merged;
together they left `customers` with two leaf nodes, and Django refuses to run
`migrate` at all in that state — which is the whole of the production build.

This is the merge node and nothing else. It carries no operations. Production
already has `0018_card_replacement` applied; the next build applies the other
0018 (one `ALTER INDEX ... RENAME`) and then this.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0018_card_replacement'),
        ('customers', '0018_own_outstanding_model_edits'),
    ]

    operations = [
    ]
