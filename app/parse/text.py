"""Text cleanup: HTML to plain text and whitespace collapsing."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_WS_RE = re.compile(r"\s+")
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})
_SKIP_TAGS = frozenset({"script", "style"})


def collapse_ws(s: str | None) -> str | None:
    """Collapse every run of whitespace (including nbsp, tabs, CR/LF) to one space and strip.

    Returns None for None or a string that is empty after collapsing.
    """
    if s is None:
        return None
    collapsed = _WS_RE.sub(" ", s.replace("\xa0", " ")).strip()
    return collapsed or None


class _TextExtractor(HTMLParser):
    """Collects text nodes, turning block tags into breaks and dropping script/style bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(raw: str | None) -> str | None:
    """Strip tags, decode entities (also double-encoded ones), drop script/style, collapse whitespace."""
    if raw is None:
        return None
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    text = html.unescape("".join(parser.parts))
    return collapse_ws(text)
