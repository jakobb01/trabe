"""Fixed issuer feeds and resumable lower-priority Massive scans; no strategy writes."""

import json
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree

from trabe.news_event_store import EventStore, generation, state
from trabe.news_events import analyze, digest
from trabe.signals import WorkStopped

# Short feed excerpts and source links only. No republication/full-text archive.
# Source discovery checked 2026-11-14; unavailable sources remain explicit gaps.
ISSUERS = {
    "NVDA": {
        "url": "https://nvidianews.nvidia.com/releases.xml",
        "host": "nvidianews.nvidia.com",
        "prefix": "/news/",
        "checked": "2026-11-14",
        "use": "short factual feed extracts and links",
    },
    "AMD": {
        "url": "https://ir.amd.com/news-events/press-releases/rss",
        "host": "ir.amd.com",
        "prefix": "/news-events/press-releases/detail/",
        "checked": "2026-11-14",
        "use": "short factual feed extracts and links",
    },
    "VST": {
        "url": "https://investor.vistracorp.com/news?pagetemplate=rss",
        "host": "investor.vistracorp.com",
        "prefix": "/20",
        "checked": "2026-11-14",
        "use": "short factual feed extracts and links",
    },
}
UNAVAILABLE = {
    "AMZN": "Official IR request returned HTTP 403; no feed enabled",
    "GOOGL": "Official IR request returned HTTP 403; no feed enabled",
}


def cursor_from_url(url):
    if not url:
        return None
    if not isinstance(url, str) or len(url) > 1800:
        raise ValueError("Invalid continuation cursor.")
    try:
        parsed = urlsplit(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.massive.com"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.path != "/v2/reference/news"
            or parsed.fragment
            or len(pairs) != 1
            or pairs[0][0] != "cursor"
        ):
            raise ValueError
        value = pairs[0][1]
        if not re.fullmatch(r"[A-Za-z0-9_~.+=/-]{1,1024}", value):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise ValueError("Invalid continuation cursor.") from None


def allowed_issuer_url(url, *, pages=False):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            return False
        if any(url == item["url"] for item in ISSUERS.values()):
            return True
        return (
            pages
            and not parsed.query
            and any(
                parsed.hostname == item["host"] and parsed.path.startswith(item["prefix"])
                for item in ISSUERS.values()
            )
        )
    except ValueError:
        return False


def parse_feed(body, ticker, now):
    from trabe.news import canonical_url, text

    if (
        not isinstance(body, bytes)
        or len(body) > 512 * 1024
        or b"<!DOCTYPE" in body.upper()
        or b"<!ENTITY" in body.upper()
    ):
        raise ValueError("Invalid issuer feed.")
    root = ElementTree.fromstring(body)
    if root.tag != "rss":
        raise ValueError("Unsupported issuer feed.")
    items = []
    for row in root.findall("./channel/item")[:100]:
        try:
            date = parsedate_to_datetime(row.findtext("pubDate", ""))
            stamp = date.timestamp() if date.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            continue
        url = canonical_url(row.findtext("link", ""))
        headline = text(row.findtext("title", ""), 512)
        if stamp is None or not 0 <= now - stamp <= 14 * 86400 or not url or not headline:
            continue
        # NVIDIA syndicates its own blog in the feed. Links are displayed, not fetched.
        snippet = text(row.findtext("description", ""), 1500)
        items.append(
            {
                "provider": "issuer-" + ticker,
                "item_id": row.findtext("guid") or digest(url),
                "url": url,
                "headline": headline,
                "snippet": snippet,
                "publisher": ticker + " official feed",
                "role": "issuer",
                "input_quality": "snippet" if snippet else "headline_only",
                "published_at": stamp,
            }
        )
    return items


class EventCollector:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.repo = EventStore(store)

    def guard(self, expected, now, db=None):
        self.http.presentation_generation = expected
        deadline = getattr(self.http, "deadline", None)
        if type(deadline) in (int, float) and time.time() >= deadline:
            return False
        if db is None:
            with self.store.connection() as connection:
                return self.guard(expected, now, connection)
        settings = self.store.settings(db)
        return (
            generation(db) == expected
            and not float(state(db, "pause_until", "0")) > now
            and not state(db, "storage_paused")
            and (settings["news_enabled"] == "on" or settings["briefing_enabled"] == "on")
            and not (getattr(self.http, "stop", None) and self.http.stop.is_set())
        )

    def mark(self, source, ticker, now, expected, **values):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.guard(expected, now, db):
                return self.repo.coverage(source, ticker, now, db=db, **values)
        return False

    def first_page(self, ticker, raw, now, expected):
        """Remember a legacy first page without altering its URL or result semantics."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if generation(db) != expected:
                return
            row = db.execute(
                "SELECT payload FROM event_collection WHERE source='massive-scan' AND ticker=?",
                (ticker,),
            ).fetchone()
            old = json.loads(row[0]) if row else {}
            if old.get("cursor") or old.get("restart"):
                return  # Fresh mirror still runs independently while the fixed backlog resumes.
            try:
                cursor = cursor_from_url(raw.get("next_url"))
            except ValueError:
                self.repo.coverage(
                    "massive-scan",
                    ticker,
                    now,
                    db=db,
                    state="truncated",
                    gap="Rejected continuation metadata",
                )
                return
            lower = max(now - 72 * 3600, old.get("watermark", now - 72 * 3600) - 6 * 3600)
            # The legacy first page covers 72h. Reuse only if that is our fixed interval.
            self.repo.coverage(
                "massive-scan",
                ticker,
                now,
                db=db,
                lower=lower,
                upper=now,
                cursor=cursor,
                seen=[],
                watermark=old.get("watermark", 0) if cursor else now,
                state="truncated" if cursor else "complete response",
                gap="Fixed provider interval only; late items outside overlap may be missed",
            )

    def _massive_items(self, raw, ticker, now):
        from trabe.news import News, text
        from trabe.news_sources import NewsSources

        # Same bounded normalizer, but these rows never enter legacy NewsStore.
        adapter = NewsSources(self.http, News(self.store, self.http)._normalize, text)
        return adapter.massive_items(raw, ticker, now)

    def continuation(self, ticker, now, expected):
        for page_index in range(2):
            if not self.guard(expected, now):
                raise WorkStopped("News presentation collection changed.")
            with self.store.connection() as db:
                row = db.execute(
                    "SELECT payload FROM event_collection WHERE source='massive-scan' AND ticker=?",
                    (ticker,),
                ).fetchone()
            scan = json.loads(row[0]) if row else {}
            cursor = scan.get("cursor")
            if (not cursor and not scan.get("restart")) or (
                page_index == 0 and now - scan.get("attempted_at", 0) < 60
            ):
                return
            try:
                params = (
                    {"cursor": cursor}
                    if cursor
                    else {
                        "ticker": ticker,
                        "published_utc.gte": datetime.fromtimestamp(scan["lower"], UTC).isoformat(),
                        "published_utc.lte": datetime.fromtimestamp(scan["upper"], UTC).isoformat(),
                        "sort": "published_utc",
                        "order": "desc",
                    }
                )
                raw = self.http.news_api("massive", _presentation=True, limit=100, **params)
                received = max(now, getattr(self.http, "received_at", now))
                items = self._massive_items(raw, ticker, received)
                items = [i for i in items if scan["lower"] <= i["published_at"] <= scan["upper"]]
                next_cursor = cursor_from_url(raw.get("next_url"))
                seen = scan.get("seen", [])
                if len(seen) >= 32:
                    self.mark(
                        "massive-scan",
                        ticker,
                        received,
                        expected,
                        state="truncated",
                        gap="Bounded scan history reached; fixed interval remains partial",
                        cursor=None,
                    )
                    return
                if next_cursor and (next_cursor == cursor or digest(next_cursor) in seen):
                    self.mark(
                        "massive-scan",
                        ticker,
                        received,
                        expected,
                        state="truncated",
                        gap="Repeated cursor; scan stopped",
                        cursor=None,
                    )
                    return
                update = scan | {
                    "cursor": next_cursor,
                    "seen": [*seen, digest(cursor or "restart")][-32:],
                    "restart": False,
                    "state": "truncated" if next_cursor else "complete response",
                    "watermark": scan.get("watermark", 0) if next_cursor else scan["upper"],
                }
                if not self.repo.ingest(
                    items,
                    ticker,
                    received,
                    source="massive-scan",
                    expected_generation=expected,
                    guard=lambda db: self.guard(expected, received, db),
                    collection=update,
                ):
                    return
                now = received
            except WorkStopped:
                raise
            except Exception as error:
                code = getattr(error, "code", 0)
                expired = code in (400, 404, 410)
                # A failed page never advances the watermark or commits its evidence.
                self.mark(
                    "massive-scan",
                    ticker,
                    now,
                    expected,
                    state="quota-deferred" if code == 429 else "truncated",
                    gap="Cursor expired; fixed interval will restart"
                    if expired
                    else "Continuation unavailable/deferred",
                    **({"cursor": None, "restart": True} if expired else {}),
                )
                return

    def official(self, ticker, now, expected):
        if ticker in UNAVAILABLE:
            self.mark(
                "official",
                ticker,
                now,
                expected,
                state="unsupported source",
                gap=UNAVAILABLE[ticker],
            )
            return
        entry = ISSUERS.get(ticker)
        if not entry:
            self.mark(
                "official",
                ticker,
                now,
                expected,
                state="unsupported source",
                gap="No reviewed official feed",
            )
            return
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.guard(expected, now, db):
                return
            row = db.execute(
                "SELECT payload FROM event_collection WHERE source='official' AND ticker=?",
                (ticker,),
            ).fetchone()
            old = json.loads(row[0]) if row else {}
            if now - old.get("attempted_at", 0) < 3600:
                return
            self.repo.coverage(
                "official", ticker, now, db=db, state="partial", gap="Feed attempt in progress"
            )
        try:
            body, validators = self.http.issuer(entry["url"], validators=old.get("validators", {}))
            received = max(now, getattr(self.http, "received_at", now))
            if body is None:
                with self.store.connection() as db:
                    if self.guard(expected, received, db):
                        self.repo.coverage(
                            "official",
                            ticker,
                            received,
                            db=db,
                            state="complete response",
                            gap="Not modified; feed window only",
                            validators=validators,
                            succeeded_at=received,
                        )
                return
            items = parse_feed(body, ticker, received)
            # RSS headline/description is sufficient. Optional public issuer metadata
            # extraction uses at most two pages and is independent of Yahoo settings.
            candidates = sorted(
                items,
                key=lambda i: (
                    -{"high": 3, "medium": 2, "low": 1}[analyze(i, ticker)["materiality"]],
                    -i["published_at"],
                ),
            )
            page_count = 0
            for item in candidates:
                if page_count >= 2:
                    break
                if item["snippet"] or not allowed_issuer_url(item["url"], pages=True):
                    continue
                page_count += 1
                try:
                    from trabe.articles import parse_document
                    from trabe.news import text

                    page, _ = self.http.issuer(item["url"], pages=True)
                    document = parse_document(page, received)
                    if document and abs(document["published_at"] - item["published_at"]) < 86400:
                        # Only complete issuer-bearing sentences; no stored document cache.
                        snippets = re.split(r"(?<=[.!?])\s+", text(document["text"], 4096))[:120]
                        selected = [
                            s
                            for s in snippets
                            if analyze(item | {"headline": s, "snippet": ""}, ticker)["issuer"]
                            and len(s) <= 280
                        ][:2]
                        if selected:
                            item["snippet"] = " ".join(selected)
                            item["input_quality"] = "article"
                except WorkStopped:
                    raise
                except Exception:
                    pass
            self.repo.ingest(
                items,
                ticker,
                received,
                source="official",
                expected_generation=expected,
                guard=lambda db: self.guard(expected, received, db),
                collection={
                    "state": "complete response" if items else "empty response",
                    "gap": "Bounded issuer feed; company statements, not independent verification",
                    "validators": validators,
                    "succeeded_at": received,
                },
            )
        except WorkStopped:
            raise
        except Exception:
            self.mark(
                "official",
                ticker,
                now,
                expected,
                state="unavailable",
                gap="Issuer feed unavailable; cached evidence retained",
            )

    def run_once(self, symbols, now):
        expected = int(self.store.state("news_event_generation", "0"))
        if not symbols or not self.guard(expected, now):
            return
        cursor = int(self.store.state("event_collection_cursor", "0")) % len(symbols)
        # Two symbols per pass; <= 2 feeds + 4 pages + 4 continuations = ten requests.
        for ticker in (symbols + symbols)[cursor : cursor + min(2, len(symbols))]:
            self.official(ticker, now, expected)
            if "massive" in self.store.settings()["news_sources"].split(","):
                self.continuation(ticker, now, expected)
        self.store.set_state("event_collection_cursor", str((cursor + 2) % len(symbols)))
