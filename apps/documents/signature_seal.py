"""The round seal on a signed original.

A picture of what the file already proves: the document is signed with a
secured electronic signature (apps/documents/signing). It is drawn only on a
file that is signed and stored — the one original, and the archive copy of a
document issued before signing existed, whose centre then says "העתק לארכיון"
instead of "מסמך ממוחשב" — never on an office copy, which is not signed. It
proves nothing by itself; the signature in the file does.

Every letter is drawn as its outline, read from the TrueType font reportlab has
already loaded, and not as text. Text set glyph by glyph around a circle lands
in the PDF's text layer as loose letters, which then turn up inside a search,
a copy-paste or an accounting tool reading the small print beside the seal.
Hebrew letters never join, so a letter's outline is the letter.
"""
from __future__ import annotations

import math
import struct
from functools import lru_cache

from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Flowable

TOP_TEXT = 'חתימה אלקטרונית מאובטחת'
CENTRE_TEXT = 'מסמך ממוחשב'
# The centre line sits inside the inner ring (radius 31 on the 92pt grid), a
# little below the middle; wider than this and a longer text would touch it.
CENTRE_MAX_WIDTH = 50.0
CENTRE_SIZE = 6.6
CENTRE_TRACKING = 0.2


class _Outlines:
    """Glyph outlines of one registered TrueType font, as quadratic contours in font units."""

    def __init__(self, font_name: str):
        face = pdfmetrics.getFont(font_name).face
        self.units_per_em = face.unitsPerEm
        self.char_to_glyph = face.charToGlyph
        self.char_widths = face.charWidths          # per 1000 units of the em
        self.default_width = face.defaultWidth
        head = face.get_table('head')
        long_offsets = struct.unpack('>h', head[50:52])[0] == 1
        loca = face.get_table('loca')
        if long_offsets:
            self.offsets = struct.unpack(f'>{len(loca) // 4}I', loca)
        else:
            self.offsets = [2 * value for value in struct.unpack(f'>{len(loca) // 2}H', loca)]
        self.glyf = face.get_table('glyf')

    def advance(self, char: str, size: float) -> float:
        return self.char_widths.get(ord(char), self.default_width) * size / 1000.0

    def contours(self, char: str) -> list[list[tuple[float, float, bool]]]:
        glyph = self.char_to_glyph.get(ord(char))
        return self._glyph(glyph, 0) if glyph is not None else []

    def _glyph(self, glyph: int, depth: int) -> list[list[tuple[float, float, bool]]]:
        if depth > 4 or glyph + 1 >= len(self.offsets):
            return []
        start, end = self.offsets[glyph], self.offsets[glyph + 1]
        if start >= end:
            return []                                  # a space: no outline
        data = self.glyf[start:end]
        count = struct.unpack('>h', data[0:2])[0]
        return self._simple(data, count) if count >= 0 else self._composite(data, depth)

    @staticmethod
    def _simple(data: bytes, contour_count: int) -> list[list[tuple[float, float, bool]]]:
        ends = struct.unpack(f'>{contour_count}H', data[10:10 + 2 * contour_count])
        pos = 10 + 2 * contour_count
        pos += 2 + struct.unpack('>H', data[pos:pos + 2])[0]    # skip the hinting instructions
        points = ends[-1] + 1 if ends else 0
        flags: list[int] = []
        while len(flags) < points:
            flag = data[pos]
            pos += 1
            flags.append(flag)
            if flag & 0x08:                                     # repeated
                flags.extend([flag] * data[pos])
                pos += 1
        flags = flags[:points]

        def coordinates(short_bit: int, same_bit: int) -> list[int]:
            nonlocal pos
            values, value = [], 0
            for flag in flags:
                if flag & short_bit:
                    step = data[pos]
                    pos += 1
                    value += step if flag & same_bit else -step
                elif not flag & same_bit:
                    value += struct.unpack('>h', data[pos:pos + 2])[0]
                    pos += 2
                values.append(value)
            return values

        xs = coordinates(0x02, 0x10)
        ys = coordinates(0x04, 0x20)
        contours, first = [], 0
        for last in ends:
            contours.append([(xs[i], ys[i], bool(flags[i] & 0x01)) for i in range(first, last + 1)])
            first = last + 1
        return contours

    def _composite(self, data: bytes, depth: int) -> list[list[tuple[float, float, bool]]]:
        def f2dot14(raw: bytes) -> float:
            return struct.unpack('>h', raw)[0] / 16384.0

        out, pos = [], 10
        while True:
            flags, child = struct.unpack('>HH', data[pos:pos + 4])
            pos += 4
            if flags & 0x0001:
                dx, dy = struct.unpack('>hh', data[pos:pos + 4])
                pos += 4
            else:
                dx, dy = struct.unpack('>bb', data[pos:pos + 2])
                pos += 2
            if not flags & 0x0002:                      # point-matched placement: not used here
                dx = dy = 0
            a, b, c, d = 1.0, 0.0, 0.0, 1.0
            if flags & 0x0008:
                a = d = f2dot14(data[pos:pos + 2])
                pos += 2
            elif flags & 0x0040:
                a, d = f2dot14(data[pos:pos + 2]), f2dot14(data[pos + 2:pos + 4])
                pos += 4
            elif flags & 0x0080:
                a, b = f2dot14(data[pos:pos + 2]), f2dot14(data[pos + 2:pos + 4])
                c, d = f2dot14(data[pos + 4:pos + 6]), f2dot14(data[pos + 6:pos + 8])
                pos += 8
            for contour in self._glyph(child, depth + 1):
                out.append([(a * x + c * y + dx, b * x + d * y + dy, on) for x, y, on in contour])
            if not flags & 0x0020:                      # no more components
                return out


@lru_cache(maxsize=4)
def _outlines(font_name: str) -> _Outlines:
    return _Outlines(font_name)


def _quad(path, start, control, end) -> None:
    """A TrueType quadratic segment as the cubic a PDF path takes."""
    (x0, y0), (qx, qy), (x3, y3) = start, control, end
    path.curveTo(
        x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0),
        x3 + 2 / 3 * (qx - x3), y3 + 2 / 3 * (qy - y3),
        x3, y3,
    )


def _add_contour(path, points: list[tuple[float, float, bool]]) -> None:
    if not points:
        return
    first_on = next((i for i, point in enumerate(points) if point[2]), None)
    if first_on is None:                               # only control points: start between two
        x = (points[-1][0] + points[0][0]) / 2
        y = (points[-1][1] + points[0][1]) / 2
        points = [(x, y, True)] + list(points)
    else:
        points = list(points[first_on:]) + list(points[:first_on])
    current = (points[0][0], points[0][1])
    path.moveTo(*current)
    control = None
    for x, y, on_curve in points[1:] + points[:1]:
        if on_curve:
            if control is None:
                path.lineTo(x, y)
            else:
                _quad(path, current, control, (x, y))
                control = None
            current = (x, y)
        elif control is None:
            control = (x, y)
        else:                                          # two controls in a row: the point between is on the curve
            middle = ((control[0] + x) / 2, (control[1] + y) / 2)
            _quad(path, current, control, middle)
            current, control = middle, (x, y)
    path.close()


def _draw_glyph(canvas, outlines: _Outlines, char: str, size: float) -> None:
    """One letter with its baseline's left end at the origin."""
    contours = outlines.contours(char)
    if not contours:
        return
    canvas.saveState()
    scale = size / outlines.units_per_em
    canvas.scale(scale, scale)
    path = canvas.beginPath()
    for contour in contours:
        _add_contour(path, contour)
    canvas.drawPath(path, stroke=0, fill=1, fillMode=1)    # TrueType outlines fill non-zero
    canvas.restoreState()


def draw_line(canvas, outlines: _Outlines, text: str, size: float, x: float, y: float,
              tracking: float = 0.0) -> None:
    """A straight line of text as outlines, centred on (x, y)."""
    visual = get_display(text)
    widths = [outlines.advance(char, size) for char in visual]
    left = x - (sum(widths) + tracking * (len(visual) - 1)) / 2
    for char, width in zip(visual, widths):
        canvas.saveState()
        canvas.translate(left, y)
        _draw_glyph(canvas, outlines, char, size)
        canvas.restoreState()
        left += width + tracking


def draw_arc(canvas, outlines: _Outlines, text: str, size: float, radius: float, *,
             top: bool, tracking: float = 0.0) -> None:
    """
    Text round the seal, centred on the top or the bottom.

    Over the top the letters stand outward from `radius`; under the bottom they
    stand inward from it, so both read upright. Either way the visual order
    runs left to right, which is what the bidi algorithm hands back.
    """
    visual = get_display(text)
    widths = [outlines.advance(char, size) for char in visual]
    span = (sum(widths) + tracking * (len(visual) - 1)) / radius
    angle = math.pi / 2 + span / 2 if top else 3 * math.pi / 2 - span / 2
    for char, width in zip(visual, widths):
        half = width / 2 / radius
        middle = angle - half if top else angle + half
        canvas.saveState()
        canvas.translate(radius * math.cos(middle), radius * math.sin(middle))
        canvas.rotate(math.degrees(middle) - 90 if top else math.degrees(middle) + 90)
        canvas.translate(-width / 2, 0)
        _draw_glyph(canvas, outlines, char, size)
        canvas.restoreState()
        step = (width + tracking) / radius
        angle = angle - step if top else angle + step


class SignatureSeal(Flowable):
    """
    The seal, `diameter` points across, at the left of its cell.

    Drawing it must never cost a document: whatever goes wrong, the page comes
    out without the seal rather than not at all.
    """

    def __init__(self, diameter: float, *, bold_font: str, regular_font: str,
                 ink, accent, fill, bottom_text: str, company_number: str,
                 centre_text: str = CENTRE_TEXT):
        super().__init__()
        self.centre_text = centre_text or CENTRE_TEXT
        self.diameter = diameter
        self.bold_font = bold_font
        self.regular_font = regular_font
        self.ink = ink
        self.accent = accent
        self.fill = fill
        self.bottom_text = bottom_text
        self.company_number = company_number

    def wrap(self, available_width, available_height):
        return self.diameter, self.diameter

    def draw(self) -> None:
        canvas = self.canv
        canvas.saveState()
        try:
            canvas.translate(self.diameter / 2, self.diameter / 2)
            canvas.scale(self.diameter / 92.0, self.diameter / 92.0)     # drawn on a 92pt grid
            self._draw_on_grid(canvas)
        except Exception:
            pass
        finally:
            canvas.restoreState()

    def _draw_on_grid(self, canvas) -> None:
        bold = _outlines(self.bold_font)
        regular = _outlines(self.regular_font)

        canvas.setFillColor(self.fill)
        canvas.setStrokeColor(self.ink)
        canvas.circle(0, 0, 45.2, stroke=0, fill=1)
        canvas.setLineWidth(1.5)
        canvas.circle(0, 0, 45.2, stroke=1, fill=0)
        canvas.setLineWidth(0.5)
        canvas.circle(0, 0, 42.6, stroke=1, fill=0)
        canvas.setLineWidth(0.8)
        canvas.circle(0, 0, 31.0, stroke=1, fill=0)

        canvas.setFillColor(self.ink)
        draw_arc(canvas, bold, TOP_TEXT, 7.0, 34.2, top=True, tracking=0.55)
        draw_arc(canvas, bold, self.bottom_text, 7.0, 39.4, top=False, tracking=0.75)

        # The band's two joints, in the yellow of the page's corner ring.
        canvas.setFillColor(self.accent)
        for x in (-36.8, 36.8):
            canvas.circle(x, 0, 1.55, stroke=0, fill=1)

        self._draw_tick(canvas)
        canvas.setFillColor(self.ink)
        draw_line(canvas, bold, self.centre_text, self._centre_size(bold), 0, -11.5, tracking=CENTRE_TRACKING)
        draw_line(canvas, regular, f'ח.פ. {self.company_number}', 5.4, 0, -19.6, tracking=0.15)

    def _centre_size(self, outlines: _Outlines) -> float:
        """The centre line's size: 6.6, or smaller when a longer text would not fit inside the ring."""
        text = get_display(self.centre_text)
        width = sum(outlines.advance(char, CENTRE_SIZE) for char in text) + CENTRE_TRACKING * (len(text) - 1)
        if width <= CENTRE_MAX_WIDTH:
            return CENTRE_SIZE
        return CENTRE_SIZE * CENTRE_MAX_WIDTH / width

    def _draw_tick(self, canvas) -> None:
        canvas.setStrokeColor(self.ink)
        canvas.setLineWidth(2.6)
        canvas.setLineCap(1)
        canvas.setLineJoin(1)
        tick = canvas.beginPath()
        tick.moveTo(-7.0, 10.6)
        tick.lineTo(-2.2, 5.8)
        tick.lineTo(7.4, 16.2)
        canvas.drawPath(tick, stroke=1, fill=0)
