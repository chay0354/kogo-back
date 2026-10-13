"""
Where a sum of money in an answer came from — read once, for two readers.

    evals/checks.py   holds every answer of a test conversation against what the
                      system told the bot (`grounded_amounts`), after the fact.
    shadow.py         stops an answer with a sum nobody returned BEFORE it is
                      kept or sent (the money guard; the owner, 14.10.2026 — the
                      real bot wrote ₪360 that no tool returned and the
                      knowledge does not hold).

Both ask `unsourced_amounts`, so the test and the guard cannot disagree about
what a sum is, where one may come from, or what it is to say a customer's own
sum back to him as against confirming it. Nothing here knows of a model, a
contact or a database: text in, numbers out.

A sum has a source when it is

    * in what a tool returned in that run — under a key that names money
      (MONEY_KEYS), or written with a ₪ inside any text a tool returned;
    * written with a ₪ in the knowledge the bot may say (bodies and data, not
      the examples: a "wrong" example holds what must not be said);
    * the customer's own, said back to him. Confirmed ("נכון, ₪99 לחודש") it is
      not his any more: what a customer proposed is not true because he did.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_URL = re.compile(r'https?://\S+')
_N = r'\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?'
# "17:00, ₪260" holds one amount: the minutes of an hour beside a ₪ are not money.
_AMOUNT = re.compile(rf'₪\s?({_N})|(?<![\d:.,])({_N})\s?(?:₪|ש"ח|ש״ח|שקלים|שקל)')
_SHEKEL_IN_TEXT = re.compile(rf'₪\s?({_N})')
# Anything that reads as money at all — what is left to go by when the careful reading itself breaks.
_MONEY_MARK = re.compile(r'₪|ש"ח|ש״ח|שקל')

MONEY_KEYS = {'price_monthly', 'registration_fee', 'price', 'amount', 'trial_price', 'price_before_discounts', 'price_as_second_class'}


# --- reading an answer --------------------------------------------------------------------------------------

def _plain(reply: str) -> str:
    """The answer without its links: a link's letters and digits are not the bot's words."""
    return _URL.sub(' ', reply or '')


def _number(text) -> Decimal | None:
    try:
        return Decimal(str(text).replace(',', '')).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None


def amounts_in(text: str) -> list:
    found = []
    for match in _AMOUNT.finditer(_plain(text)):
        value = _number(match.group(1) or match.group(2))
        if value is not None:
            found.append(value)
    return found


def mentions_money(text: str) -> bool:
    """A sign of money anywhere in the text — coarser than `amounts_in`, and with nothing in it that can fail."""
    return bool(_MONEY_MARK.search(str(text or '')))


def _shekels(value: Decimal) -> str:
    text = f'{value}'
    return '₪' + (text[:-3] if text.endswith('.00') else text)


# --- reading what the tools returned ---------------------------------------------------------------------------

def _leaves(value, key=''):
    if isinstance(value, dict):
        for name, inner in value.items():
            yield from _leaves(inner, str(name))
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _leaves(inner, key)
    else:
        yield key, value


def money_evidence(calls: list, knowledge_text: str = '') -> set:
    """
    Every sum the system told the bot: under a key that names money in what a
    tool returned, and written as a sum — "₪60", "60 ש"ח", "60 שקלים" — in any
    sentence a tool returned or the knowledge holds. (Until 14.10.2026 a
    sentence was read only for "₪60". A sum the owner wrote in the knowledge as
    "60 ש"ח" is the knowledge's all the same — and since the bot's own guard
    reads this too, missing it would cost a customer a right answer.)
    """
    found = set()
    for call in calls:
        for key, value in _leaves(call.get('result')):
            if key in MONEY_KEYS and value not in (None, '') and not isinstance(value, bool):
                number = _number(str(value).lstrip('-'))
                if number is not None:
                    found.add(number)
            if isinstance(value, str):
                found.update(amounts_in(value))
    found.update(amounts_in(knowledge_text))
    return found


def knowledge_text(items, hours=None) -> str:
    """What the knowledge lets the bot say: the bodies and the data. Not the examples — a "wrong" example holds what must not be said."""
    def strings(value):
        if isinstance(value, dict):
            for inner in value.values():
                yield from strings(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                yield from strings(inner)
        elif isinstance(value, str):
            yield value

    parts = []
    for item in items:
        parts.append(item.body or '')
        parts.extend(strings(item.data or {}))
    # The office's hours and special days are read to the bot beside the items: "פתוח עד 18:00" is the knowledge's.
    parts.extend(strings(hours or {}))
    return '\n'.join(part for part in parts if part)


# --- the customer's own words -------------------------------------------------------------------------------------
#
# An hour, a day, a sum or a date the customer wrote is not something the bot made up when it says it back to him
# ("אמור להיות שיעור ב-17:30 — בודקים", "ב-31.12 אין שיעור ניסיון"). It is one, when the bot CONFIRMS it and no
# tool returned it ("כן, אפשר ב-31.12", "נכון, ₪150 לחודש"): what the customer proposed is not true because he
# proposed it. So a value only the customer wrote passes where it is said back, and fails where it is affirmed.

_BARE_NUMBER = re.compile(rf'(?<![\d:.])({_N})(?![\d:])')
_STATEMENT_END = re.compile(r'(?<=[.!?])\s+|\n')
_AFFIRMS = re.compile(
    r'(?<![א-ת])(?:כן|נכון|בדיוק|אפשר|ניתן|פנוי|פנויה|מוזמנים|מוזמנות|נתראה|מתאים|מתקיים|יתקיים|יש שיעור|יש מקום|בואו|תגיעו|קבענו|מחכים לכם'
    r'|המחיר|עולה|העלות|לחודש|תשלמו|משלמים)(?![א-ת])'
)
_DENIES = re.compile(r'(?<![א-ת])(?:לא|אין|אי אפשר|איננו|אינו|אינה|בלי)(?![א-ת])')
# A date said to be taken or closed is the truth about it, not an offer of it.
# ("ב-31.12 אין כרגע שיעור ניסיון פתוח" says it as much as "אין שיעור" does: a word between the two is not an offer.)
_UNAVAILABLE = re.compile(
    r'מלא|אין מקום|לא נשאר|תפוס|לא מתקיים|לא יתקיים|בוטל|מבוטל|לא פתוח|סגור|אי אפשר|לא ניתן|לא אפשרי|לא זמין|חופש|כבר התחיל'
    r'|אין (?:כרגע |עדיין |שם |אצלנו |לנו |בו )*(?:שיעור|ניסיון|תאריך|מועד)'
)


def _numbers_written(text: str) -> set:
    """Every number in a customer's words, as a sum of money would compare to it: he seldom writes the ₪."""
    return {value for value in (_number(found) for found in _BARE_NUMBER.findall(_plain(text))) if value is not None}


def statements(reply: str) -> list:
    """An answer cut into what it says one thing at a time: its lines, and the sentences of a line."""
    return [part.strip() for part in _STATEMENT_END.split(reply or '') if part.strip()]


def affirms(statement: str) -> bool:
    """The statement says that something is so — it is neither a denial of it nor a question about it."""
    return bool(_AFFIRMS.search(statement)) and not _DENIES.search(statement) and not _UNAVAILABLE.search(statement) and '?' not in statement


def _only_the_customers(reply: str, read, known: set, written: set) -> tuple:
    """(invented, affirmed): values of the answer no evidence holds — the ones nobody wrote, and the customer's own that the answer confirms."""
    invented, affirmed = [], []
    for statement in statements(reply):
        for value in read(statement):
            if value in known:
                continue
            if value not in written:
                invented.append(value)
            elif affirms(statement):
                affirmed.append(value)
    return list(dict.fromkeys(invented)), list(dict.fromkeys(affirmed))


# --- the one question both readers ask ---------------------------------------------------------------------------

def unsourced_amounts(reply: str, *, evidence: list, knowledge: str = '', customer: str = '') -> tuple:
    """
    (invented, affirmed) — the sums of an answer that have no source.

    `evidence`   what the system told the bot: [{name, input, result}], the
                 tools' answers and the card of the writer it was handed.
    `knowledge`  the text the knowledge lets the bot say (`knowledge_text`).
    `customer`   everything the customer himself wrote in the conversation.

    `invented` are sums nobody returned and nobody wrote; `affirmed` are sums
    only the customer wrote, in a statement of the answer that confirms them.
    The same sum written twice is one; bold, a comma of thousands and agorot
    read as the number they are.
    """
    known = money_evidence(evidence, knowledge)
    return _only_the_customers(reply, amounts_in, known, _numbers_written(customer))


def shown(values) -> str:
    """Sums in a line of their own, as the checks and the guard write them: "₪360, ₪1250.50"."""
    return ', '.join(_shekels(value) for value in values)
