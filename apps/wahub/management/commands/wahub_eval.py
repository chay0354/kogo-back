from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.wahub.evals import fixtures, runner, scenarios
from apps.wahub.models import EVAL_ERROR, EVAL_FAILED, EVAL_PASSED, EVAL_SKIPPED


class _Rollback(Exception):
    """Raised to undo the invented catalogue of --fixtures."""


class Command(BaseCommand):
    help = (
        'Plays the written conversations of the bot\'s self-checks (apps/wahub/evals/) against the bot and prints what '
        'passed and what failed. Each conversation is played in memory: no contact and no message is made and nothing is '
        'sent. With an Anthropic key the bot is Claude, and a failed script fails the command. Without one the stub '
        'answers: that checks the scripts and the tools, not the bot, so its failures are printed and the command still '
        'ends well (--strict fails it). --fixtures builds an invented catalogue inside a transaction and rolls it back.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--only', default='', help='Scenario ids, comma separated.')
        parser.add_argument('--group', default='', help=f'One group: {", ".join(group for group, _title in scenarios.GROUPS)}.')
        parser.add_argument('--fixtures', action='store_true', help='Run on an invented catalogue; nothing is kept in the database.')
        parser.add_argument('--no-judge', action='store_true', help='Skip the rubric (the second model).')
        parser.add_argument('--show', action='store_true', help='Print every conversation, not only the failures.')
        parser.add_argument('--list', action='store_true', help='Only list the scenarios.')
        parser.add_argument('--strict', action='store_true', help='Fail the command on a failed script even when the stub answered.')

    def handle(self, *args, **options):
        wanted = [part.strip() for part in options['only'].split(',') if part.strip()]
        unknown = [name for name in wanted if name not in scenarios.BY_ID]
        if unknown:
            raise CommandError(f'אין תרחיש כזה: {", ".join(unknown)}')
        if options['group'] and options['group'] not in scenarios.GROUP_TITLES:
            raise CommandError(f'אין קבוצה כזאת: {options["group"]}')
        chosen = [
            scenario for scenario in scenarios.SCENARIOS
            if (not wanted or scenario.id in wanted) and (not options['group'] or scenario.group == options['group'])
        ]
        if options['list']:
            for scenario in chosen:
                self.stdout.write(f'{scenario.id}  [{scenarios.GROUP_TITLES[scenario.group]}]  {scenario.title} ({len(scenario.turns)} תורות)')
            return

        if options['fixtures']:
            failed = 0
            try:
                with transaction.atomic():
                    fixtures.build()
                    failed = self._run(chosen, options)
                    raise _Rollback()
            except _Rollback:
                self.stdout.write('הקטלוג המומצא והריצות בוטלו; מסד הנתונים נשאר כפי שהיה.')
        else:
            # Each run is kept as it ends, and shows on the screen under "בדיקות הבוט".
            failed = self._run(chosen, options)
        if failed and not runner.engine()['configured']:
            self.stdout.write(self.style.WARNING(
                'ענה הבוט המדומה (אין מפתח של Claude): הכישלונות האלה הם שלו, לא של הבוט. '
                'מה שנבדק כאן הוא התרחישים, הכלים וסדר השיחה; את הבוט עצמו בודקים בשרת, במסך "בדיקות הבוט".'
            ))
        if failed and (options['strict'] or runner.engine()['configured']):
            raise CommandError(f'{failed} תרחישים נכשלו.')

    def _run(self, chosen, options) -> int:
        engine = runner.engine()
        self.stdout.write(f'מי עונה: {engine["label"]} ({engine["model"]}); שיפוט לפי מחוון: {"כן" if engine["judge"] and not options["no_judge"] else "לא"}')
        totals = {EVAL_PASSED: 0, EVAL_FAILED: 0, EVAL_SKIPPED: 0, EVAL_ERROR: 0}
        for scenario in chosen:
            run = runner.run_to_end(scenario.id, with_judge=not options['no_judge'])
            totals[run.status] += 1
            payload = runner.run_payload(run)
            style = {EVAL_PASSED: self.style.SUCCESS, EVAL_FAILED: self.style.ERROR}.get(run.status, self.style.WARNING)
            self.stdout.write(style(f'{payload["status_label"]}  {scenario.id} — {scenario.title}'))
            if run.status in (EVAL_SKIPPED, EVAL_ERROR):
                self.stdout.write(f'    {run.detail}')
            if options['show'] or run.status == EVAL_FAILED:
                for turn in payload['conversation']:
                    for message in turn['customer']:
                        self.stdout.write(f'    לקוח: {"[" + message["kind"] + "] " if message["kind"] != "text" else ""}{message["text"]}')
                    for tool in turn['tools']:
                        self.stdout.write(f'      ({tool["name"]}: {tool["summary"]})')
                    for line in (turn['bot'] or '').split('\n'):
                        self.stdout.write(f'    בוט:  {line}')
            for row in payload['failures']:
                self.stdout.write(self.style.ERROR(f'    ✗ תור {row["turn"]} · {row["title"]}: {row["explain"]}'))
        self.stdout.write(
            f'סה"כ {len(chosen)}: עברו {totals[EVAL_PASSED]}, נכשלו {totals[EVAL_FAILED]}, '
            f'דולגו {totals[EVAL_SKIPPED]}, לא רצו {totals[EVAL_ERROR]}.'
        )
        return totals[EVAL_FAILED]
