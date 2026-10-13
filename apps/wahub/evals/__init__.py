"""
"בדיקות הבוט" — the bot checks itself (docs/WAHUB-CONTRACT-BOT-EVALS.md;
docs/WAHUB-MASTER-PLAN-2026-10-12.md, 1ד).

Written conversations — a parent who asks "do you have classes?", a
grandmother, a child too young, an external branch, a recording nobody heard —
are played against the bot turn by turn, and every answer is held against two
kinds of check:

    hard     in code (checks.py). What was called and in which order, what
             must and must not be in the text, and — for every answer of every
             conversation — grounding: an amount, a class, a day, an hour, a
             phone or a link the bot wrote must have come from the tools or
             the knowledge of that conversation.
    rubric   short Hebrew sentences ("the bot asked for the field before it
             offered a class") a second model judges (judge.py) — only where
             there is a key.

A conversation is played in memory (shadow.scripted_context): no contact, no
message and no shadow reply is made, and nothing is sent. What is kept is the
run itself (BotEvalRun).

The scripts assume no data. Their words — a city, a field, a child's age — and
the facts their checks expect — the price of that class's trial — are read
from the system when a run begins (world.py), so the same script runs on a
developer's invented catalogue (fixtures.py) and on the server against the real
one; a script the system has nothing for is skipped, and says what is missing.

    scenarios.py   the conversations
    rules.py       the rules they cover (the table of the audit, by id)
    world.py       the words and the facts, read from the system
    fixtures.py    an invented catalogue, for a machine with no data
    checks.py      the hard checks (what a sum of money is and where one may come
                   from is read in apps/wahub/grounding.py, which the bot's own
                   guard reads too: shadow.py stops such an answer before it leaves)
    judge.py       the rubric — the judge reads what the bot read: what the tools
                   returned and the knowledge behind every answer
    runner.py      playing a conversation, keeping the run, the JSON of the contract
"""
