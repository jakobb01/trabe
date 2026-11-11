"""Versioned pre-open expectations, frozen inputs and separate later outcomes."""

import hashlib
import json
import math
import re
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trabe import __version__
from trabe.calendar import NY, USCalendar, retention_cutoff
from trabe.market_context import (
    BENCHMARKS,
    SOURCE,
    baseline,
    chart_url,
    number,
    preopen,
    target_session,
)
from trabe.market_news import relevant
from trabe.news_store import NewsStore
from trabe.signals import SignalStore, WorkStopped

RULES = "market-outlook-rules-v1"
ASSESSMENT = "market-outlook-assessment-v1"
BAND = Decimal("0.2")
# Cboe's closing calculation follows the equity close by 15 minutes.
VIX_CLOSE_OFFSET = 15 * 60
MAX_PACKET = 64 * 1024
OUTLOOK_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS market_outlooks(
        id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('manual','scheduled')),
        session_date TEXT NOT NULL,as_of REAL NOT NULL,opens REAL NOT NULL,closes REAL NOT NULL,
        request_key TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,sha256 TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_daily_outlook ON market_outlooks(session_date) WHERE kind='scheduled'",
    """CREATE TABLE IF NOT EXISTS outlook_evaluations(
        outlook_id TEXT PRIMARY KEY REFERENCES market_outlooks(id) ON DELETE CASCADE,
        evaluated_at REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS outlook_runs(
        session_date TEXT PRIMARY KEY,scheduled_for REAL NOT NULL,status TEXT NOT NULL,
        prepared_at REAL,last_error TEXT NOT NULL DEFAULT '')""",
    """CREATE TRIGGER IF NOT EXISTS immutable_market_outlook BEFORE UPDATE ON market_outlooks
        BEGIN SELECT RAISE(ABORT,'Issued outlooks are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_replace_market_outlook BEFORE INSERT ON market_outlooks
        WHEN EXISTS(SELECT 1 FROM market_outlooks WHERE id=NEW.id OR request_key=NEW.request_key
        OR (NEW.kind='scheduled' AND kind='scheduled' AND session_date=NEW.session_date))
        BEGIN SELECT RAISE(ABORT,'Issued outlooks cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_outlook_evaluation BEFORE UPDATE ON outlook_evaluations
        BEGIN SELECT RAISE(ABORT,'Outlook evaluations are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_replace_outlook_evaluation BEFORE INSERT ON outlook_evaluations
        WHEN EXISTS(SELECT 1 FROM outlook_evaluations WHERE outlook_id=NEW.outlook_id)
        BEGIN SELECT RAISE(ABORT,'Outlook evaluations cannot be replaced'); END""",
)
CATALYSTS = (
    ("inflation data", r"\b(?:inflation|consumer prices?|cpi|ppi)\b"),
    ("Fed/rate news", r"\b(?:federal reserve|fed|interest rates?)\b"),
    ("jobs data", r"\b(?:payrolls|jobs report|unemployment)\b"),
    ("trade policy", r"\b(?:tariffs?|trade war)\b"),
    ("bond yields", r"\b(?:treasury yields?|bond yields?)\b"),
    ("earnings", r"\bearnings\b"),
)


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def direction(changes, band=BAND):
    """Mixed means flat/divergent benchmarks, not missing evidence."""
    if len(changes) != len(BENCHMARKS) or any(v is None for v in changes):
        return "insufficient"
    if all(v >= band for v in changes):
        return "bullish"
    if all(v <= -band for v in changes):
        return "bearish"
    return "mixed"


class OutlookStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def unpack(row):
        if not row:
            return None
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("Saved outlook integrity check failed.")
        return json.loads(row[0])

    def get(self, *, session_date=None, request_key=None, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.get(session_date=session_date, request_key=request_key, db=connection)
        where, args = (
            (" WHERE request_key=?", (request_key,))
            if request_key
            else ((" WHERE session_date=?", (session_date,)) if session_date else ("", ()))
        )
        return self.unpack(
            db.execute(
                "SELECT payload,sha256 FROM market_outlooks"
                + where
                + " ORDER BY as_of DESC,rowid DESC LIMIT 1",
                args,
            ).fetchone()
        )

    def prune(self, now, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.prune(now, connection)
        cutoff = retention_cutoff(datetime.fromtimestamp(now, NY).date()).isoformat()
        db.execute(
            "DELETE FROM market_outlooks WHERE kind='scheduled' AND session_date<=?", (cutoff,)
        )
        db.execute("DELETE FROM outlook_runs WHERE session_date<=?", (cutoff,))
        db.execute(
            "DELETE FROM market_outlooks WHERE kind='manual' AND (as_of<? OR id NOT IN (SELECT id FROM market_outlooks WHERE kind='manual' ORDER BY as_of DESC,rowid DESC LIMIT 32))",
            (now - 7 * 86400,),
        )


def outcome_chart(raw, symbol, session, received):
    """Exact first/last regular minute endpoints; never substitute a nearby day."""
    chart = raw["chart"]
    if chart.get("error") or len(chart["result"]) != 1 or received < session.closes + 1800:
        raise ValueError("Outcome not ready.")
    row = chart["result"][0]
    meta = row["meta"]
    if (
        meta.get("symbol") != symbol
        or meta.get("currency") != "USD"
        or meta.get("instrumentType") != "ETF"
        or meta.get("dataGranularity") != "1m"
        or meta.get("exchangeTimezoneName") != "America/New_York"
    ):
        raise ValueError("Mismatched outcome instrument.")
    times = row["timestamp"]
    bars = row["indicators"]["quote"][0]
    if (
        not 0 < len(times) <= 10000
        or len(times) != len(bars["open"])
        or len(times) != len(bars["close"])
    ):
        raise ValueError("Invalid outcome bars.")
    endpoints = {}
    for index, stamp in enumerate(times):
        stamp = float(number(stamp))
        if stamp > received:
            raise ValueError("Future outcome bar.")
        if stamp in (session.opens, session.closes - 60):
            if stamp in endpoints:
                raise ValueError("Duplicate outcome endpoint.")
            endpoints[stamp] = index
    first, last = endpoints[session.opens], endpoints[session.closes - 60]
    return {
        "symbol": symbol,
        "session_date": session.day,
        "source": SOURCE,
        "source_url": chart_url(symbol),
        "received_at": received,
        "open": str(number(bars["open"][first])),
        "close": str(number(bars["close"][last])),
        "open_time": session.opens,
        "close_bar_time": session.closes - 60,
        "quality": "indicative first/last regular minute bars; not official auction prices or total returns",
    }


class OutlookService:
    def __init__(self, store, calendar=None):
        self.store = store
        self.calendar = calendar or USCalendar()
        self.repo = OutlookStore(store)

    @staticmethod
    def _signal(db, kind, key, now):
        row = db.execute(
            "SELECT payload,last_seen,source FROM signals WHERE kind=? AND event_key=? AND last_seen<=? AND available_at<=?",
            (kind, key, now, now),
        ).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        if value.get("received_at", now + 1) > now:
            return None
        return {"value": value, "known_at": row[1], "source": row[2]}

    def _inputs(self, db, session, now, settings):
        inputs = {"market": {}, "market_news": None, "company_news": [], "source_health": []}
        gaps, changes = [], []
        for symbol in (*BENCHMARKS, "^VIX"):
            observed = self._signal(db, "market-context", symbol, now)
            inputs["market"][symbol] = observed
            value = observed["value"] if observed and observed["source"] == SOURCE else {}
            if symbol == "^VIX":
                usable = False
                try:
                    previous = self.calendar.previous(date.fromisoformat(session.day))
                    stamp = value["regular_time"]
                    usable = (
                        value["symbol"] == symbol
                        and value["currency"] == "points"
                        and stamp <= now
                        and (
                            (value["regular_day"] == session.day and now - stamp <= 1800)
                            or (
                                value["regular_day"] == previous.day
                                and abs(stamp - (previous.closes + VIX_CLOSE_OFFSET)) <= 120
                            )
                        )
                        and 0 <= now - value["received_at"] <= 3600
                    )
                    if usable:
                        number(value["regular_price"])
                except (KeyError, TypeError, ValueError):
                    usable = False
                if observed:
                    observed["usable"] = usable
                    observed["quality"] = (
                        "prior session"
                        if value.get("regular_day") != session.day
                        else "15m delayed"
                    )
                if not usable:
                    gaps.append("VIX context unavailable/stale")
                continue
            change = None
            try:
                close, day = baseline(value, session, self.calendar)
                stamp = value["premarket_time"]
                if (
                    close
                    and value["symbol"] == symbol
                    and value["currency"] == "USD"
                    and value["premarket_session"] == session.day
                    and preopen(session) <= stamp <= now < session.opens
                    and now - stamp <= 1200
                    and 0 <= now - value["received_at"] <= 1200
                ):
                    change = (number(value["premarket_price"]) / close - 1) * 100
                    if abs(change) > 25:
                        change = None
                    observed["baseline"] = {"price": str(close), "session_date": day}
            except (KeyError, TypeError, ValueError):
                pass
            if observed:
                observed["change_percent"] = str(change) if change is not None else None
            if change is None:
                gaps.append(symbol + " current premarket/baseline unavailable or stale")
            changes.append(change)
        row = self._signal(db, "market-news", "latest", now)
        if row and row["source"] in (
            "finnhub" if "finnhub" in settings["news_sources"].split(",") else "",
            "yahoo-rss" if "yahoo" in settings["news_sources"].split(",") else "",
        ):
            row["value"]["items"] = [
                item
                for item in row["value"]["items"]
                if 0 <= now - item["published_at"] <= 86400 and relevant(item)
            ][:5]
            inputs["market_news"] = row
        news = inputs["market_news"]
        if not news or not news["value"]["items"] or now - news["value"]["received_at"] > 10800:
            gaps.append("Fresh broad-market news unavailable")
        watched = {r[0] for r in db.execute("SELECT ticker FROM watchlist")}
        from trabe.ledger import Ledger

        held = Ledger._held(db)
        for ticker in (sorted(held) + sorted(watched - held))[:8]:
            view = NewsStore(self.store).view(ticker, db, now)
            if not view or not 0 <= now - view["as_of"] <= 10800:
                continue
            evidence = view.get("evidence", [])[:3]
            if any(
                e.get("known_at", now + 1) > now
                or e.get("published_at", now + 1) > now
                or any(v.get("known_at", now + 1) > now for v in e.get("vendor_scores", []))
                for e in evidence
            ):
                continue
            evidence = [e for e in evidence if 0 <= now - e.get("published_at", 0) <= 86400]
            if not evidence:
                continue
            saved = []
            for item in evidence:
                saved.append(
                    {
                        k: item[k]
                        for k in (
                            "id",
                            "group_id",
                            "url",
                            "published_at",
                            "known_at",
                            "input_quality",
                        )
                        if k in item
                    }
                    | {
                        "headline": item.get("headline", "")[:240],
                        "snippet": item.get("snippet", "")[:480],
                        "analysis": {
                            k: item["analysis"][k]
                            for k in ("event_type", "method", "input_hash")
                            if k in item.get("analysis", {})
                        },
                    }
                )
            inputs["company_news"].append(
                {
                    "symbol": ticker,
                    "as_of": view["as_of"],
                    "evidence": saved,
                }
            )
        for kind, source, symbol, attempted, succeeded, error in db.execute(
            "SELECT kind,source,symbol,attempted_at,succeeded_at,error FROM signal_sources WHERE kind IN ('market-context','market-news') AND attempted_at<=?",
            (now,),
        ):
            if symbol not in (*BENCHMARKS, "^VIX", "*"):
                continue
            inputs["source_health"].append(
                {
                    "kind": kind,
                    "source": source,
                    "symbol": symbol,
                    "attempted_at": attempted,
                    "succeeded_at": succeeded if succeeded <= now else None,
                    "error": error,
                }
            )
            if error:
                gaps.append(kind + ": " + symbol + " source failed/deferred")
        return inputs, gaps, changes

    @staticmethod
    def _forecast(inputs, gaps, changes):
        opening = direction(changes)
        news = inputs["market_news"]
        vix = inputs["market"]["^VIX"]
        complete = (
            opening != "insufficient"
            and "Fresh broad-market news unavailable" not in gaps
            and bool(vix and vix.get("usable"))
            and not any(
                s["error"] and s["kind"] == "market-news" and news and s["source"] == news["source"]
                for s in inputs["source_health"]
            )
        )
        session = opening if complete else "insufficient"
        macro_text = (
            " ".join(
                item["headline"] + " " + item.get("snippet", "") for item in news["value"]["items"]
            )
            if news
            else ""
        )
        catalysts = [label for label, pattern in CATALYSTS if re.search(pattern, macro_text, re.I)][
            :2
        ]
        company_event = any(
            e.get("analysis", {}).get("event_type") == "earnings"
            for company in inputs["company_news"]
            for e in company["evidence"]
        )
        if company_event and "earnings" not in catalysts:
            catalysts = (catalysts + ["company earnings"])[:2]
        if opening == "insufficient":
            first = "Insufficient current premarket evidence for an opening or session direction."
        else:
            moves = ", ".join(f"{s} {v:+.2f}%" for s, v in zip(BENCHMARKS, changes))
            bias = (
                f"{session} session bias"
                if session != "insufficient"
                else "session evidence insufficient"
            )
            first = f"{opening.capitalize()} opening expectation; {bias}, with {moves} indicative premarket."
        risk = (
            "/".join(catalysts) + " news could change the tone"
            if catalysts
            else "new headlines could change the tone"
        )
        if vix and vix.get("usable"):
            level = number(vix["value"]["regular_price"])
            risk += f"; VIX {level:.2f} ({vix['quality']})"
            if level >= 25:
                risk += " suggests elevated swing risk"
        elif opening != "insufficient":
            risk += "; VIX context is missing"
        if opening != "insufficient" and "Fresh broad-market news unavailable" in gaps:
            risk += "; fresh market news is missing"
        if opening != "insufficient" and any("failed/deferred" in gap for gap in gaps):
            risk += "; some feeds are deferred"
        if opening == "insufficient":
            risk = "current SPY/QQQ bars and verified prior closes are required"
        return {
            "opening": opening,
            "session": session,
            "catalysts": catalysts,
            "text": first + " Risk: " + risk + ".",
        }

    def issue(self, kind="manual", *, now=None, session_date=None, request_key=None, owner_id=None):
        if kind not in {"manual", "scheduled"}:
            raise ValueError("Unknown outlook kind.")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time() if now is None else now
            if not math.isfinite(now):
                raise ValueError("Invalid outlook time.")
            settings = self.store.settings(db)
            session = (
                self.calendar.session(date.fromisoformat(session_date))
                if session_date
                else target_session(self.calendar, now)
            )
            if not session:
                raise ValueError("Outlook requires a verified US session.")
            key = request_key or (
                "scheduled:" + session.day if kind == "scheduled" else uuid.uuid4().hex
            )
            old = self.repo.get(request_key=key, db=db)
            if old:
                return old
            if now >= session.opens:
                raise ValueError(
                    "Pre-open issuance has closed; use /outlook cached for the saved expectation."
                )
            state = dict(
                db.execute(
                    "SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused')"
                )
            )
            if state.get("storage_paused"):
                raise ValueError("Outlook creation paused by storage health.")
            if kind == "scheduled" and (
                settings["outlook_enabled"] != "on" or float(state.get("pause_until", "0")) > now
            ):
                return None
            if kind == "scheduled" and not 0 <= now - (session.opens - 1800) <= 300:
                raise ValueError("Outlook capture window missed; no backfill.")
            if (
                kind == "manual"
                and db.execute(
                    "SELECT 1 FROM market_outlooks WHERE kind='manual' AND as_of>?", (now - 60,)
                ).fetchone()
            ):
                raise ValueError(
                    "Wait one minute before issuing another outlook; /outlook cached is available."
                )
            inputs, gaps, changes = self._inputs(db, session, now, settings)
            forecast = self._forecast(inputs, gaps, changes)
            packet = {
                "id": uuid.uuid4().hex[:16],
                "kind": kind,
                "schema_version": 1,
                "app_version": __version__,
                "rule_version": RULES,
                "rule_parameters": {
                    "direction_band_percent": str(BAND),
                    "premarket_max_age_seconds": 1200,
                    "vix_receipt_max_age_seconds": 3600,
                    "macro_publication_max_age_seconds": 86400,
                    "macro_receipt_max_age_seconds": 10800,
                    "vix_elevated_swing_context_level": "25",
                    "vix_close_after_equities_seconds": VIX_CLOSE_OFFSET,
                },
                "as_of": now,
                "session_date": session.day,
                "opens": session.opens,
                "closes": session.closes,
                "scheduled_for": session.opens - 1800 if kind == "scheduled" else None,
                "benchmarks": list(BENCHMARKS),
                "settings": {k: settings[k] for k in ("timezone", "news_sources")},
                "assessment": {
                    "version": ASSESSMENT,
                    "band_percent": str(BAND),
                    "opening": "first regular minute open vs frozen previous-session close",
                    "session": "last regular minute close vs first regular minute open",
                    "mixed": "benchmarks flat within band or divergent",
                    "price_basis": "unadjusted indicative minute bars, excluding distributions/costs",
                },
                "inputs": inputs,
                "inputs_sha256": digest(inputs),
                "gaps": gaps,
                "forecast": forecast,
            }
            raw = encoded(packet)
            if len(raw.encode()) > MAX_PACKET:
                raise ValueError("Outlook exceeds 64 KiB; nothing was issued.")
            db.execute(
                "INSERT INTO market_outlooks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    packet["id"],
                    kind,
                    session.day,
                    now,
                    session.opens,
                    session.closes,
                    key,
                    raw,
                    hashlib.sha256(raw.encode()).hexdigest(),
                ),
            )
            if owner_id is not None:
                from trabe.outbox import Outbox

                Outbox(self.store).send(
                    owner_id,
                    self.report(packet),
                    kind="outlook" if kind == "scheduled" else "reply",
                    db=db,
                )
            if kind == "scheduled":
                db.execute(
                    "UPDATE outlook_runs SET status='issued',last_error='' WHERE session_date=?",
                    (session.day,),
                )
            self.repo.prune(now, db)
            return packet

    def report(self, packet):
        zone = ZoneInfo(self.store.settings()["timezone"])
        stamp = datetime.fromtimestamp(packet["as_of"], zone).strftime("%d %b %H:%M %Z")
        return f"🔭 Outlook (rules) • {packet['session_date']} • as of {stamp}\n{packet['forecast']['text']}"

    def briefing(self, now):
        try:
            session = target_session(self.calendar, now)
            packet = self.repo.get(session_date=session.day)
            if packet and packet["as_of"] <= now:
                return self.report(packet)
            local = datetime.fromtimestamp(
                session.opens - 1800, ZoneInfo(self.store.settings()["timezone"])
            ).strftime("%d %b %H:%M %Z")
            return (
                "Pre-open outlook: "
                + (
                    "scheduled " + local
                    if self.store.settings()["outlook_enabled"] == "on"
                    else "off; /outlook now requests one"
                )
                + "."
            )
        except ValueError:
            return "Pre-open outlook: calendar unavailable/expired."

    def schedule(self, insights, now=None):
        fixed_time = now is not None
        now = time.time() if now is None else now
        if (
            self.store.settings()["outlook_enabled"] != "on"
            or self.store.automation_paused(now)
            or self.store.state("storage_paused")
        ):
            return
        try:
            today = self.calendar.at(now)
            if not today:
                return
            session = today
        except ValueError:
            self.store.set_state("outlook_error", "Outlook suspended: exchange calendar expired.")
            return
        scheduled = session.opens - 1800
        if now < scheduled - 120:
            return
        self.store.set_state("outlook_error", "")
        with self.store.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO outlook_runs(session_date,scheduled_for,status) VALUES (?,?,'pending')",
                (session.day, scheduled),
            )
            status, prepared = db.execute(
                "SELECT status,prepared_at FROM outlook_runs WHERE session_date=?", (session.day,)
            ).fetchone()
            if status != "pending":
                return
            if now > scheduled + 300:
                db.execute(
                    "UPDATE outlook_runs SET status='missed',last_error='Capture window passed; no backfill' WHERE session_date=?",
                    (session.day,),
                )
                return
        if now < scheduled and prepared is None:
            job = insights.queue(
                "outlook",
                origin="automatic",
                gate="outlook",
                deadline=scheduled,
                session=session.day,
            )
            if isinstance(job, int):
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE outlook_runs SET prepared_at=? WHERE session_date=?",
                        (now, session.day),
                    )
        if now >= scheduled:
            try:
                self.issue(
                    "scheduled",
                    now=now if fixed_time else None,
                    session_date=session.day,
                    owner_id=insights.config.owner_id,
                )
            except ValueError:
                self.store.set_state(
                    "outlook_error",
                    "Outlook could not be saved/delivered; retry within the capture window.",
                )

    def evaluate(self, http, *, now):
        """At most one session per pass; source cooldowns bound retries."""
        if (
            self.store.settings()["outlook_enabled"] != "on"
            or self.store.automation_paused(now)
            or self.store.state("storage_paused")
        ):
            return
        with self.store.connection() as db:
            days = db.execute(
                "SELECT DISTINCT m.session_date,m.closes FROM market_outlooks m LEFT JOIN outlook_evaluations e ON e.outlook_id=m.id WHERE e.outlook_id IS NULL AND m.closes<=? ORDER BY m.closes LIMIT 96",
                (now - 1800,),
            ).fetchall()
        repo = SignalStore(self.store)
        selected = None
        for day, closes in days:
            cached = {
                symbol: repo.get("outlook-outcome", day + ":" + symbol) for symbol in BENCHMARKS
            }
            values = {
                symbol: row["payload"]
                for symbol, row in cached.items()
                if row and row["last_seen"] <= now and row["payload"]["received_at"] <= now
            }
            expired = now - closes > 7 * 86400
            if (
                expired
                or len(values) == 2
                or any(
                    symbol not in values
                    and repo.due("outlook-outcome", SOURCE, day + ":" + symbol, 3600, now)
                    for symbol in BENCHMARKS
                )
            ):
                selected = (day, values, expired)
                break
        if selected is None:
            return
        day, values, expired = selected
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT m.payload,m.sha256 FROM market_outlooks m LEFT JOIN outlook_evaluations e ON e.outlook_id=m.id WHERE e.outlook_id IS NULL AND m.session_date=? ORDER BY m.as_of LIMIT 33",
                (day,),
            ).fetchall()
        packets = [self.repo.unpack(row) for row in rows]
        session = self.calendar.session(date.fromisoformat(day))
        for symbol in BENCHMARKS:
            key = day + ":" + symbol
            if symbol in values:
                continue
            if expired or not repo.due("outlook-outcome", SOURCE, key, 3600, now):
                continue
            repo.attempt("outlook-outcome", SOURCE, key, now)
            try:
                raw = http.json(chart_url(symbol), provider=SOURCE)
                received = max(now, getattr(http, "received_at", now))
                value = outcome_chart(raw, symbol, session, received)
                if hasattr(http, "guard"):
                    http.guard()
                repo.put(
                    "outlook-outcome",
                    key,
                    symbol,
                    SOURCE,
                    value,
                    now=received,
                    event_time=session.closes,
                )
                repo.ok("outlook-outcome", SOURCE, key, received)
                values[symbol] = value
            except WorkStopped:
                raise
            except Exception:
                repo.fail(
                    "outlook-outcome",
                    SOURCE,
                    key,
                    "Exact regular-session outcome endpoints unavailable/deferred.",
                    now,
                )
        if len(values) < 2 and not expired:
            return
        for packet in packets:
            opening, regular, result = [], [], {}
            for symbol in BENCHMARKS:
                value = values.get(symbol)
                prior = packet["inputs"]["market"].get(symbol)
                close = prior.get("baseline", {}).get("price") if prior else None
                o = ((number(value["open"]) / number(close) - 1) * 100) if value and close else None
                r = ((number(value["close"]) / number(value["open"]) - 1) * 100) if value else None
                opening.append(o)
                regular.append(r)
                result[symbol] = {
                    "opening_percent": str(o) if o is not None else None,
                    "session_percent": str(r) if r is not None else None,
                }
            band = Decimal(packet["assessment"]["band_percent"])
            realized = {"opening": direction(opening, band), "session": direction(regular, band)}
            assessment = {
                h: {
                    "expected": packet["forecast"][h],
                    "realized": realized[h],
                    "matched": packet["forecast"][h] == realized[h]
                    if "insufficient" not in (packet["forecast"][h], realized[h])
                    else None,
                }
                for h in realized
            }
            record = {
                "schema_version": 1,
                "method": ASSESSMENT,
                "outlook_id": packet["id"],
                "evaluated_at": max([now] + [v["received_at"] for v in values.values()]),
                "session_date": day,
                "observations": values,
                "returns": result,
                "assessment": assessment,
                "gap": "Outcome endpoints missing after seven days; unscorable"
                if len(values) < 2
                else "",
            }
            raw = encoded(record)
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                state = dict(
                    db.execute(
                        "SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused')"
                    )
                )
                if (
                    self.store.settings(db)["outlook_enabled"] != "on"
                    or float(state.get("pause_until", "0")) > record["evaluated_at"]
                    or state.get("storage_paused")
                ):
                    return
                if db.execute(
                    "SELECT 1 FROM outlook_evaluations WHERE outlook_id=?", (packet["id"],)
                ).fetchone():
                    continue
                db.execute(
                    "INSERT INTO outlook_evaluations VALUES (?,?,?,?)",
                    (
                        packet["id"],
                        record["evaluated_at"],
                        raw,
                        hashlib.sha256(raw.encode()).hexdigest(),
                    ),
                )

    def results(self):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT e.payload,e.sha256 FROM outlook_evaluations e JOIN market_outlooks m ON m.id=e.outlook_id WHERE m.kind='scheduled' AND json_extract(m.payload,'$.rule_version')=? AND json_extract(e.payload,'$.method')=? ORDER BY m.session_date DESC LIMIT 64",
                (RULES, ASSESSMENT),
            ).fetchall()
        records = [self.repo.unpack(row) for row in rows]
        if not records:
            return "No assessed daily outlooks yet. Results use SPY/QQQ minute-bar opening gaps and open-to-close changes, separately from trading returns."
        lines = [f"Daily outlook results • {len(records)} assessed sessions • ±{BAND}% flat band"]
        for horizon in ("opening", "session"):
            eligible = [
                r["assessment"][horizon]
                for r in records
                if r["assessment"][horizon]["matched"] is not None
            ]
            matches = sum(r["matched"] for r in eligible)
            bull = sum(r["realized"] == "bullish" for r in eligible)
            mixed = sum(r["realized"] == "mixed" for r in eligible)
            lines.append(
                f"{horizon.capitalize()}: {matches}/{len(eligible)} matched • {len(records) - len(eligible)} unscored • always bullish {bull}/{len(eligible)}, always mixed {mixed}/{len(eligible)}"
            )
        latest = records[0]
        moves = [
            f"{s} session {Decimal(v['session_percent']):+.2f}%"
            for s, v in latest["returns"].items()
            if v["session_percent"] is not None
        ]
        lines.append(latest["session_date"] + ": " + (", ".join(moves) or "outcome unavailable"))
        lines.append("Indicative minute-bar endpoints; no simulated trades or total-return claim.")
        return "\n".join(lines)

    def status(self):
        text = f"Daily outlook: {self.store.settings()['outlook_enabled']} • 30m before US open"
        with self.store.connection() as db:
            run = db.execute(
                "SELECT session_date,status FROM outlook_runs ORDER BY session_date DESC LIMIT 1"
            ).fetchone()
        if run:
            text += f" • {run[0]} {run[1]}"
        if self.store.state("outlook_error"):
            text += " • " + self.store.state("outlook_error")
        return text
