"""Telegram styling for plain text, including persisted replies from older versions.

Use native entities rather than parsing provider text as HTML or Markdown.
Offsets are relative to each delivered chunk and measured in UTF-16 units.
"""

import re

SECTIONS = {
    "Positions",
    "Watchlist prices (cached observations)",
    "Earnings",
    "Insider purchases",
    "News",
    "News sentiment",
    "Recent quarters • EPS (actual vs expected)",
    "Earnings / insiders / news / briefing:",
}
NOTES = (
    "Source:",
    "Observed:",
    "Finnhub:",
    "FX:",
    "Dates may change.",
    "Sentiment is heuristic",
    "Prices and summaries",
    "Cached observations;",
    "Observed feed price",
    "End-of-day data",
    "Alpha Vantage end-of-day",
    "Qualification uses",
    "Last observed:",
    "Last checked:",
    "More:",
    "Reported purchases;",
)
STOCK = re.compile(
    r"^(?:[•🔻🟢🔴⚪⚠️]+\s*)?([A-Z][A-Z0-9.-]{0,19}(?:, [A-Z][A-Z0-9.-]{0,19})*)(?=[: •])"
)
LABEL = re.compile(
    r"(?:^|•\s)((?:Bullish|Neutral|Bearish)(?: \([+-]?\d+(?:\.\d+)?\))?)(?=\s|$)|"
    r"^(Cost basis|Market value|Unrealized P/L|Realized P/L|Total|Expected|Daily change|Value|Traded|Filed|Held):|"
    r"\b(HIT|MET|MISS)\b"
)
COMMAND = re.compile(r"(?<![\w/])/[a-z][a-z_]*(?:@[A-Za-z0-9_]+)?")


def message_entities(text):
    """Apply a small, consistent hierarchy without changing any message content."""
    spans = []

    def add(kind, start, end):
        if start >= end or any(start < right and end > left for _, left, right in spans):
            return
        if len(spans) < 90:
            spans.append((kind, start, end))

    offset = 0
    for index, raw in enumerate(text.splitlines(keepends=True)):
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        start = offset + len(line) - len(line.lstrip())
        end = offset + len(line.rstrip())
        if stripped in SECTIONS or (
            index == 0
            and "\n" in text
            and len(stripped) <= 120
            and not stripped.startswith(("/", "http://", "https://"))
        ):
            add("bold", start, end)
        elif stripped.startswith(NOTES):
            add("italic", start, end)
        else:
            stock = STOCK.match(stripped)
            if stock:
                add("bold", start + stock.start(1), start + stock.end(1))
            for match in LABEL.finditer(stripped):
                group = match.lastindex
                add("bold", start + match.start(group), start + match.end(group))
        offset += len(raw)
    for match in COMMAND.finditer(text):
        add("code", match.start(), match.end())
    return [
        {
            "type": kind,
            "offset": len(text[:start].encode("utf-16-le")) // 2,
            "length": len(text[start:end].encode("utf-16-le")) // 2,
        }
        for kind, start, end in sorted(spans, key=lambda span: span[1])
    ]
