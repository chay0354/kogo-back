"""
Invisible direction marks, and text that is safe to draw.

A phone number or a name copied from a chat or a contact card often travels
with marks nobody sees: the left-to-right / right-to-left marks, the embeddings
and the isolates. They are not the customer's data. On a printed document they
do two kinds of harm: the font has no glyph for them, and python-bidi stops on
an isolate whose partner is missing from the line ("PDI not allowed here") —
which took down every PDF of a customer whose phone had been pasted that way
(7.10.2026, two tax invoices that could not be opened).

So text is cleaned twice: once where it is drawn — every PDF reorders its lines
through `visual_order` — and once where it is kept, so a card is stored without
them (`strip_direction_marks`).
"""
from __future__ import annotations

from bidi.algorithm import get_display

# LRM, RLM, ALM; the embeddings and overrides (LRE RLE PDF LRO RLO); the
# isolates (LRI RLI FSI PDI); and the two other invisibles that ride along in a
# paste — the zero-width space and the byte-order mark.
DIRECTION_MARKS = '‎‏؜‪‫‬‭‮⁦⁧⁨⁩​﻿'
_REMOVE = dict.fromkeys(map(ord, DIRECTION_MARKS))


def strip_direction_marks(text) -> str:
    """`text` without the invisible direction marks. Everything a person can see stays."""
    return str(text or '').translate(_REMOVE)


def visual_order(text) -> str:
    """One line in the order it is drawn, right-to-left runs reversed. Never stops on a stray mark."""
    return get_display(strip_direction_marks(text))
