"""The signed HTML as plain paragraphs — for the office's screen and the PDF."""
from __future__ import annotations

from html.parser import HTMLParser

# Tags that start or end a paragraph. Everything else (strong, em, span, a…)
# is inline and only loses its markup.
_BLOCK_TAGS = frozenset({
    'p', 'div', 'br', 'li', 'ul', 'ol', 'tr', 'table', 'blockquote', 'section',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr',
})
_SKIPPED_TAGS = frozenset({'script', 'style'})
_BULLET = '•'


class _ParagraphCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._current: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED_TAGS:
            self._skipping += 1
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag == 'li':
            self._current.append(f'{_BULLET} ')

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIPPED_TAGS:
            self._skipping = max(0, self._skipping - 1)
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skipping:
            self._current.append(data)

    def close(self):
        super().close()
        self._flush()

    def _flush(self):
        text = ' '.join(''.join(self._current).split())
        if text and text != _BULLET:
            self.paragraphs.append(text)
        self._current = []


def html_to_paragraphs(html: str) -> list[str]:
    """Tags stripped, entities decoded, whitespace collapsed, one string per block."""
    collector = _ParagraphCollector()
    collector.feed(html or '')
    collector.close()
    return collector.paragraphs
