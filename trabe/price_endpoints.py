"""Exact, immutable regular-minute prices and shared bounded collection claims."""

import hashlib
import json
import math
import re
from datetime import date

from trabe.calendar import USCalendar
from trabe.market_context import SOURCE, chart_url, number
from trabe.outlook import digest, encoded
from trabe.signals import WorkStopped

METHOD = "regular-minute-endpoint-v1"
QUALITY = "Unadjusted indicative price returns; corporate-action coverage unverified."
ACCOUNTING = "price-endpoint-collector"
ENDPOINT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS market_price_endpoints(
        id TEXT PRIMARY KEY,source TEXT NOT NULL,symbol TEXT NOT NULL,session_date TEXT NOT NULL,
        edge TEXT NOT NULL CHECK(edge IN ('open','close')),method TEXT NOT NULL,
        bar_time REAL NOT NULL,received_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,
        UNIQUE(source,symbol,session_date,edge,method))""",
    """CREATE TABLE IF NOT EXISTS market_endpoint_attempts(
        source TEXT NOT NULL,symbol TEXT NOT NULL,session_date TEXT NOT NULL,
        attempted_at REAL NOT NULL,next_attempt_at REAL NOT NULL,last_error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(source,symbol,session_date))""",
    """CREATE TRIGGER IF NOT EXISTS immutable_price_endpoint BEFORE UPDATE ON market_price_endpoints
        BEGIN SELECT RAISE(ABORT,'Price endpoints are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_replace_price_endpoint BEFORE INSERT ON market_price_endpoints
        WHEN EXISTS(SELECT 1 FROM market_price_endpoints WHERE id=NEW.id OR
        (source=NEW.source AND symbol=NEW.symbol AND session_date=NEW.session_date
         AND edge=NEW.edge AND method=NEW.method))
        BEGIN SELECT RAISE(ABORT,'Price endpoints cannot be replaced'); END""",
)


def checked(row):
    if row is None:
        return None
    if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
        raise ValueError("Saved evidence integrity check failed.")
    return json.loads(row[0])


def enabled(store, db, feature, now):
    settings = store.settings(db)
    state = dict(
        db.execute("SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused')")
    )
    return (
        settings.get(feature + "_enabled") == "on"
        and float(state.get("pause_until", "0")) <= now
        and not state.get("storage_paused")
    )


def endpoint_url(symbol):
    return chart_url(symbol) + "&events=div%2Csplits"


def parse_endpoint(raw, *, symbol, session, edge, received_at, calendar=None):
    """No nearest-bar or quote fallback. Receipt and bar times have distinct meanings."""
    try:
        verified = (calendar or USCalendar()).session(date.fromisoformat(session.day))
        if verified != session or edge not in {"open", "close"}:
            raise ValueError("Unverified endpoint session/edge.")
        if not re.fullmatch(r"[A-Z]{1,10}(?:[.-][A-Z])?", symbol):
            raise ValueError("Invalid canonical endpoint symbol.")
        ready = session.opens + 300 if edge == "open" else session.closes + 1800
        if isinstance(received_at, bool) or not math.isfinite(received_at) or received_at < ready:
            raise ValueError("Endpoint not ready.")
        chart = raw["chart"]
        if chart.get("error") or not isinstance(chart["result"], list) or len(chart["result"]) != 1:
            raise ValueError("Unavailable chart.")
        row = chart["result"][0]
        meta = row["meta"]
        alias = symbol.replace(".", "-")
        if (
            meta.get("symbol") != alias
            or meta.get("currency") != "USD"
            or meta.get("instrumentType") not in ({"ETF"} if symbol == "SPY" else {"EQUITY", "ETF"})
            or meta.get("dataGranularity") != "1m"
            or meta.get("exchangeTimezoneName") != "America/New_York"
        ):
            raise ValueError("Mismatched endpoint instrument.")
        times = row["timestamp"]
        quotes = row["indicators"]["quote"]
        if not isinstance(quotes, list) or len(quotes) != 1:
            raise ValueError("Invalid endpoint quotes.")
        bars = quotes[0]
        if (
            not isinstance(times, list)
            or not 0 < len(times) <= 10000
            or any(
                not isinstance(bars[k], list) or len(bars[k]) != len(times)
                for k in ("open", "close")
            )
        ):
            raise ValueError("Invalid endpoint arrays.")
        expected = session.opens if edge == "open" else session.closes - 60
        stamps = [float(number(v)) for v in times]
        if stamps.count(expected) != 1 or any(v > received_at for v in stamps):
            raise ValueError("Exact single endpoint unavailable or future bar.")
        price = number(bars[edge][stamps.index(expected)])
        # Actions are evidence of a conflict, never proof of complete action coverage.
        events = row.get("events", {})
        if not isinstance(events, dict):
            raise ValueError("Invalid corporate-action metadata.")
        splits = events.get("splits", {})
        if not isinstance(splits, dict) or len(splits) > 128:
            raise ValueError("Invalid corporate-action metadata.")
        actions = []
        for event in splits.values():
            stamp = float(number(event["date"]))
            if stamp <= received_at:
                actions.append({"kind": "split", "at": stamp})
        value = {
            "schema_version": 1,
            "method": METHOD,
            "source": SOURCE,
            "symbol": symbol,
            "provider_symbol": alias,
            "instrument_type": meta["instrumentType"],
            "currency": "USD",
            "timezone": "America/New_York",
            "session_date": session.day,
            "edge": edge,
            "bar_time": expected,
            "price": str(price),
            "received_at": received_at,
            "source_url": endpoint_url(symbol),
            "quality": QUALITY,
            "action_coverage": "unverified",
            "actions": actions,
        }
        if len(encoded(value).encode()) > 4096:
            raise ValueError("Endpoint exceeds 4 KiB.")
        return value
    except (KeyError, TypeError, IndexError, OverflowError, AttributeError) as error:
        raise ValueError("Malformed or missing exact endpoint.") from error


class EndpointStore:
    def __init__(self, store, calendar=None):
        self.store, self.calendar = store, calendar or USCalendar()

    def get(self, symbol, session_date, edge, *, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.get(symbol, session_date, edge, db=connection)
        return checked(
            db.execute(
                "SELECT payload,sha256 FROM market_price_endpoints WHERE source=? AND symbol=? AND session_date=? AND edge=? AND method=?",
                (SOURCE, symbol, session_date, edge, METHOD),
            ).fetchone()
        )

    def save(self, value, *, db):
        raw = encoded(value)
        if len(raw.encode()) > 4096:
            raise ValueError("Endpoint exceeds 4 KiB.")
        old = self.get(value["symbol"], value["session_date"], value["edge"], db=db)
        if old:
            if any(old[k] != value[k] for k in ("price", "bar_time", "instrument_type", "actions")):
                db.execute(
                    "UPDATE market_endpoint_attempts SET last_error='Conflicting later endpoint; first observation retained' WHERE source=? AND symbol=? AND session_date=?",
                    (SOURCE, value["symbol"], value["session_date"]),
                )
            return old
        identity = [SOURCE, value["symbol"], value["session_date"], value["edge"], METHOD]
        db.execute(
            "INSERT INTO market_price_endpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                digest(identity)[:32],
                SOURCE,
                value["symbol"],
                value["session_date"],
                value["edge"],
                METHOD,
                value["bar_time"],
                value["received_at"],
                raw,
                digest(value),
            ),
        )
        return value

    def reserve(self, symbol, session_date, feature, now):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not enabled(self.store, db, feature, now):
                raise WorkStopped("Endpoint collection disabled/paused.")
            retry = max(
                float(self.store.state(k, "0"))
                for k in ("yahoo_retry_after", "yahoo-chart_retry_after")
            )
            row = db.execute(
                "SELECT next_attempt_at FROM market_endpoint_attempts WHERE source=? AND symbol=? AND session_date=?",
                (SOURCE, symbol, session_date),
            ).fetchone()
            if retry > now or (row and row[0] > now):
                return False
            if (
                db.execute(
                    "SELECT COUNT(*) FROM signal_http_calls WHERE provider=? AND requested_at>?",
                    (ACCOUNTING, now - 3600),
                ).fetchone()[0]
                >= 12
            ):
                return False
            db.execute(
                "INSERT INTO market_endpoint_attempts VALUES (?,?,?,?,?,'') ON CONFLICT(source,symbol,session_date) DO UPDATE SET attempted_at=excluded.attempted_at,next_attempt_at=excluded.next_attempt_at,last_error=''",
                (SOURCE, symbol, session_date, now, now + 3600),
            )
            db.execute("INSERT INTO signal_http_calls VALUES (?,?)", (ACCOUNTING, now))
            return True

    def collect(self, http, symbol, session, edges, *, feature, now, clock):
        generation = self.store.state(feature + "_generation", "0")
        if hasattr(http, "guard"):
            http.guard()
        if not self.reserve(symbol, session.day, feature, now):
            return False
        try:
            raw = http.json(endpoint_url(symbol), provider=SOURCE)
            received = getattr(http, "received_at", clock())
            values, errors = [], []
            for edge in sorted(set(edges)):
                try:
                    values.append(
                        parse_endpoint(
                            raw,
                            symbol=symbol,
                            session=session,
                            edge=edge,
                            received_at=received,
                            calendar=self.calendar,
                        )
                    )
                except ValueError:
                    errors.append(edge + ": exact endpoint invalid/unavailable")
            if hasattr(http, "guard"):
                http.guard()
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                epoch = db.execute(
                    "SELECT value FROM state WHERE key=?", (feature + "_generation",)
                ).fetchone()
                if (
                    not enabled(self.store, db, feature, clock())
                    or (str(epoch[0]) if epoch else "0") != generation
                ):
                    raise WorkStopped("Endpoint collection cancelled during request.")
                for value in values:
                    self.save(value, db=db)
                if errors:
                    db.execute(
                        "UPDATE market_endpoint_attempts SET last_error=? WHERE source=? AND symbol=? AND session_date=?",
                        ("; ".join(errors), SOURCE, symbol, session.day),
                    )
            return True
        except WorkStopped:
            raise
        except Exception:
            with self.store.connection() as db:
                if enabled(self.store, db, feature, clock()):
                    db.execute(
                        "UPDATE market_endpoint_attempts SET last_error='Endpoint request failed/deferred; cached prices retained' WHERE source=? AND symbol=? AND session_date=?",
                        (SOURCE, symbol, session.day),
                    )
            raise

    def prune(self, now, db):
        db.execute(
            "DELETE FROM market_price_endpoints WHERE NOT EXISTS(SELECT 1 FROM advisor_outcome_inputs a WHERE a.endpoint_id=market_price_endpoints.id OR (a.state='pending' AND a.symbol=market_price_endpoints.symbol AND a.session_date=market_price_endpoints.session_date AND a.edge=market_price_endpoints.edge)) AND NOT EXISTS(SELECT 1 FROM paper_price_inputs p WHERE p.endpoint_id=market_price_endpoints.id OR (p.endpoint_id IS NULL AND p.symbol=market_price_endpoints.symbol AND p.session_date=market_price_endpoints.session_date AND p.edge=market_price_endpoints.edge)) AND (COALESCE(json_array_length(json_extract(payload,'$.actions')),0)=0 OR NOT EXISTS(SELECT 1 FROM paper_run_state WHERE status!='closed') OR EXISTS(SELECT 1 FROM paper_action_conflicts c JOIN paper_run_state s ON s.run_id=c.run_id WHERE c.ticker=market_price_endpoints.symbol AND s.status!='closed'))"
        )
        db.execute(
            "DELETE FROM market_endpoint_attempts WHERE next_attempt_at<=? AND NOT EXISTS(SELECT 1 FROM advisor_outcome_inputs a WHERE a.symbol=market_endpoint_attempts.symbol AND a.session_date=market_endpoint_attempts.session_date AND a.state='pending') AND NOT EXISTS(SELECT 1 FROM paper_price_inputs p WHERE p.symbol=market_endpoint_attempts.symbol AND p.session_date=market_endpoint_attempts.session_date AND p.endpoint_id IS NULL AND p.expires_at>?)",
            (now, now),
        )
