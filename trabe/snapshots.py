"""Immutable, bounded decision inputs captured from one committed SQLite view."""

import hashlib
import json
import math
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from trabe import __version__
from trabe.calendar import NY, USCalendar, retention_cutoff
from trabe.news_analysis import METHOD
from trabe.news_quality import VERSION
from trabe.news_store import NewsStore, encoded
from trabe.portfolio import Portfolio

MAX_PACKET = 256 * 1024
SNAPSHOT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS decision_snapshots(
        id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('manual','scheduled')),
        session_date TEXT NOT NULL,scheduled_for REAL,as_of REAL NOT NULL,
        app_version TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS decision_session_once ON decision_snapshots(session_date) WHERE kind='scheduled'",
    """CREATE TABLE IF NOT EXISTS snapshot_runs(
        session_date TEXT PRIMARY KEY,scheduled_for REAL NOT NULL,status TEXT NOT NULL,
        snapshot_id TEXT,prepared_at REAL,last_error TEXT NOT NULL DEFAULT '')""",
    """CREATE TRIGGER IF NOT EXISTS immutable_decision_snapshot
        BEFORE UPDATE ON decision_snapshots BEGIN
        SELECT RAISE(ABORT,'Decision snapshots are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_replace_decision_snapshot
        BEFORE INSERT ON decision_snapshots WHEN EXISTS(SELECT 1 FROM decision_snapshots
        WHERE id=NEW.id OR (NEW.kind='scheduled' AND kind='scheduled' AND session_date=NEW.session_date))
        BEGIN SELECT RAISE(ABORT,'Decision snapshots cannot be replaced'); END""",
)
EARNINGS_FIELDS = {
    "date",
    "period",
    "year",
    "quarter",
    "hour",
    "epsEstimate",
    "epsActual",
    "revenueEstimate",
    "revenueActual",
    "reported",
    "comparisons",
    "withdrawn_results",
    "result_seen",
    "source_conflicts",
    "superseded_by",
    "checked_at",
    "date_status",
    "precision",
}
INSIDER_FIELDS = {
    "filing_id",
    "accession",
    "owner",
    "title",
    "roles",
    "shares",
    "price",
    "value_usd",
    "currency",
    "transaction_code",
    "transaction_date",
    "published_at",
    "publication_precision",
    "derivative_status",
    "derivative_source",
    "eligibility_errors",
    "amendment",
    "flags",
    "source_url",
    "attribution_url",
    "purchase_classification",
}


def json_money(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {k: json_money(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_money(v) for v in value]
    return value


class SnapshotStore:
    def __init__(self, store):
        self.store = store

    def get(self, *, session_date=None, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.get(session_date=session_date, db=connection)
        if session_date:
            row = db.execute(
                "SELECT payload,sha256 FROM decision_snapshots WHERE kind='scheduled' AND session_date=?",
                (session_date,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT payload,sha256 FROM decision_snapshots ORDER BY as_of DESC,rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("Saved snapshot integrity check failed.")
        return json.loads(row[0])

    def prune(self, now, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.prune(now, connection)
        cutoff = retention_cutoff(datetime.fromtimestamp(now, NY).date()).isoformat()
        db.execute(
            "DELETE FROM decision_snapshots WHERE kind='scheduled' AND session_date<=?", (cutoff,)
        )
        db.execute("DELETE FROM snapshot_runs WHERE session_date<=?", (cutoff,))
        db.execute(
            "DELETE FROM decision_snapshots WHERE kind='manual' AND (as_of<? OR id NOT IN (SELECT id FROM decision_snapshots WHERE kind='manual' ORDER BY as_of DESC,rowid DESC LIMIT 32))",
            (now - 7 * 86400,),
        )


class SnapshotService:
    def __init__(self, store, calendar=None):
        self.store = store
        self.calendar = calendar or USCalendar()
        self.repo = SnapshotStore(store)

    def _price(self, raw, now, role, received=None, max_age=120):
        if not raw:
            return None
        receipt = raw.get("received_at", received)
        source = raw.get("source_time")
        if receipt is None or receipt > now or (source is not None and source > now):
            return None
        price = Decimal(str(raw.get("price", 0)))
        if not price.is_finite() or price <= 0:
            return None
        day = raw.get("trading_day")
        if not day and source:
            day = datetime.fromtimestamp(source, NY).date().isoformat()
        quality = []
        kind = "cached_reference"
        try:
            session = self.calendar.at(now)
            previous = self.calendar.previous(datetime.fromtimestamp(now, NY).date())
            if source is not None and session and session.contains(now):
                kind = "intraday_observation"
                if source is None or now - source > max_age:
                    quality.append("stale intraday observation")
            elif day == previous.day:
                kind = "previous_session_reference"
            elif session and now >= session.closes and day == session.day:
                kind = "completed_session_reference"
            elif source is None:
                kind = "end_of_day_reference"
                quality.append("older or unverified session reference")
            else:
                quality.append("older or unverified session reference")
        except ValueError:
            quality.append("calendar unavailable")
        if not raw.get("baseline_valid", True):
            quality.append("baseline flagged")
        return {
            "role": role,
            "provider": raw.get("provider", "alphavantage"),
            "feed": raw.get("feed", "end-of-day" if source is None else "unspecified"),
            "price": str(price),
            "previous_close": str(raw["previous_close"])
            if raw.get("previous_close") is not None
            else None,
            "change_percent": str(raw["change_percent"])
            if raw.get("change_percent") is not None
            else None,
            "currency": raw.get("currency"),
            "source_time": source,
            "source_precision": "timestamp" if source is not None else "session_date",
            "received_at": receipt,
            "session_date": day,
            "kind": kind,
            "quality": quality,
        }

    @staticmethod
    def _facts(db, ticker, kind, now, settings):
        rows = db.execute(
            "SELECT event_key,source,payload,event_time,available_at,last_seen FROM signals WHERE kind=? AND symbol=? AND available_at<=? AND observed_at<=? AND last_seen<=? ORDER BY event_time DESC LIMIT ?",
            (kind, ticker, now, now, now, 500 if kind == "insiders" else 100),
        ).fetchall()
        result = []
        for key, source, raw, event_time, available, observed in rows:
            payload = json.loads(raw)
            if kind == "earnings" and payload.get("superseded_by"):
                continue
            if kind == "insiders":
                if (
                    "eligibility_errors" not in payload
                    or payload["eligibility_errors"]
                    or Decimal(payload.get("value_usd", "0"))
                    < Decimal(settings["insider_min_value_usd"])
                    or event_time > now
                ):
                    continue
            fields = EARNINGS_FIELDS if kind == "earnings" else INSIDER_FIELDS
            facts = {k: v for k, v in payload.items() if k in fields}
            # Non-money timestamps remain numbers; API money is normally decimal strings.
            for field in ("epsEstimate", "epsActual", "revenueEstimate", "revenueActual"):
                if facts.get(field) is not None:
                    facts[field] = str(facts[field])
            for comparison in facts.get("comparisons", {}).values():
                for field in ("actual", "estimate"):
                    if comparison.get(field) is not None:
                        comparison[field] = str(comparison[field])
            result.append(
                {
                    "key": key,
                    "source": source,
                    "event_time": event_time,
                    "known_at": max(available, observed),
                    "facts": facts,
                }
            )
        if kind == "earnings":
            result.sort(key=lambda r: (r["event_time"] < now, abs(r["event_time"] - now)))
        return result[:3]

    @staticmethod
    def _news_sources(db, ticker, now, settings):
        statuses = {
            r[0]: r[1:]
            for r in db.execute(
                "SELECT source,attempted_at,succeeded_at,error FROM signal_sources WHERE kind='news' AND symbol=? AND attempted_at<=?",
                (ticker, now),
            )
        }
        providers = set(statuses) | {
            "yahoo-rss" if p == "yahoo" else p for p in settings["news_sources"].split(",")
        }
        result = []
        for provider in sorted(providers):
            attempted, succeeded, error = statuses.get(provider, (None, None, ""))
            raw = db.execute(
                "SELECT value FROM state WHERE key=?", ("news_source_" + provider + "_" + ticker,)
            ).fetchone()
            health = json.loads(raw[0]) if raw else None
            if health and health.get("checked_at", now + 1) > now:
                health = None
            result.append(
                {
                    "provider": provider,
                    "attempted_at": attempted,
                    "succeeded_at": succeeded
                    if succeeded is not None and succeeded <= now
                    else None,
                    "error": error,
                    "coverage": health or {"state": "not attempted"},
                }
            )
        return result

    @staticmethod
    def _earnings_history(db, ticker, now):
        row = db.execute(
            "SELECT payload,source,last_seen FROM signals WHERE kind='earnings-history' AND symbol=? AND available_at<=? AND observed_at<=? AND last_seen<=? LIMIT 1",
            (ticker, now, now, now),
        ).fetchone()
        if not row:
            return None
        periods = json.loads(row[0]).get("rows", [])[:4]
        for period in periods:
            for field in ("actual", "estimate"):
                if period.get(field) is not None:
                    period[field] = str(period[field])
        return {"quarters": periods, "source": row[1], "known_at": row[2]}

    def _packet(self, db, now, kind, session_date, scheduled_for):
        settings = self.store.settings(db)
        currency, fx, positions = Portfolio(self.store).calculate(now=now, db=db)
        watched = self.store.watchlist(db)
        position_rows = {r["ticker"]: r for r in positions}
        symbols = sorted(set(dict(watched)) | set(position_rows))
        history = hashlib.sha256()
        for row in db.execute("SELECT * FROM trades ORDER BY id"):
            history.update(encoded(row).encode())
        packet = {
            "id": uuid.uuid4().hex[:16],
            "kind": kind,
            "session_date": session_date,
            "scheduled_for": scheduled_for,
            "as_of": now,
            "lateness_seconds": max(0, now - scheduled_for) if scheduled_for is not None else 0,
            "app_version": __version__,
            "schema_version": 1,
            "db_schema_version": db.execute("PRAGMA user_version").fetchone()[0],
            "algorithm_versions": {
                "snapshot": "decision-v1",
                "news_rules": METHOD,
                "news_quality": VERSION,
            },
            "ledger_hash": history.hexdigest(),
            "configuration_hash": hashlib.sha256(encoded(settings).encode()).hexdigest(),
            "settings": settings,
            "watchlist": watched,
            "currency": currency,
            "fx": {
                "usd_per_eur": str(fx[0]),
                "effective_date": fx[1],
                "provider": fx[2],
                "received_at": fx[3],
                "direction": "EUR to USD",
            }
            if fx
            else None,
            "symbols": {},
            "gaps": [],
            "truncated": [],
        }
        try:
            session = self.calendar.session(date.fromisoformat(session_date))
            packet["exchange_session"] = (
                {"day": session.day, "opens": session.opens, "closes": session.closes}
                if session
                else None
            )
        except ValueError:
            packet["exchange_session"] = None
            packet["gaps"].append("US exchange calendar unavailable")
        packet["calendar_version"] = getattr(self.calendar, "data", {}).get("version", "unknown")
        alpha = {
            ticker: (json.loads(raw), received)
            for ticker, raw, received in db.execute(
                "SELECT ticker,payload,fetched_at FROM quote_cache WHERE fetched_at<=?", (now,)
            )
        }
        news = NewsStore(self.store)
        for ticker in symbols:
            row = position_rows.get(ticker)
            raw_live = self.store.cached_live("finnhub", ticker, db)
            live = self._price(
                raw_live,
                now,
                "portfolio_valuation",
                max_age=int(settings["max_source_age_seconds"]),
            )
            if live:
                session = live["session_date"]
                baseline = db.execute(
                    "SELECT accepted,reason,source_time,previous_close,previous_date,open_price FROM baselines WHERE ticker=? AND provider='finnhub' AND session_date=? AND source_time<=?",
                    (ticker, session, now),
                ).fetchone()
                live["baseline"] = (
                    dict(
                        zip(
                            (
                                "accepted",
                                "reason",
                                "source_time",
                                "previous_close",
                                "previous_date",
                                "open_price",
                            ),
                            baseline,
                        )
                    )
                    if baseline
                    else None
                )
                if live["baseline"]:
                    for field in ("previous_close", "open_price"):
                        live["baseline"][field] = str(live["baseline"][field])
                if baseline and not baseline[0] and "baseline flagged" not in live["quality"]:
                    live["quality"].append("baseline flagged")
                coverage = db.execute(
                    "SELECT d.observed_minutes,d.expected_minutes,d.quality_flags FROM daily_summaries d JOIN instruments i ON i.id=d.instrument_id JOIN feeds f ON f.id=d.feed_id WHERE i.symbol=? AND f.provider='finnhub' AND d.session_date=? LIMIT 1",
                    (ticker, session),
                ).fetchone()
                live["coverage"] = (
                    dict(zip(("observed_minutes", "expected_minutes", "quality_flags"), coverage))
                    if coverage
                    else None
                )
                if live["kind"] in {
                    "previous_session_reference",
                    "completed_session_reference",
                } and (not coverage or coverage[2]):
                    live["quality"].append("partial or unverified previous-session coverage")
            if settings["market_provider"] == "finnhub":
                selected = dict(live, role="watchlist_reference") if live else None
            else:
                cached, received = alpha.get(ticker, (None, None))
                selected = self._price(
                    cached,
                    now,
                    "watchlist_reference",
                    received,
                    max_age=int(settings["max_source_age_seconds"]),
                )
            view = news.view(ticker, db, now)
            if view:
                view["stale"] = now - view["as_of"] > 10800
                if any(
                    e.get("known_at", now + 1) > now
                    or any(v.get("known_at", now + 1) > now for v in e.get("vendor_scores", []))
                    for e in view.get("evidence", [])
                ):
                    view = None
            sources = self._news_sources(db, ticker, now, settings)
            position = (
                {
                    k: row[k]
                    for k in ("quantity", "basis", "realized", "converted", "value", "profit")
                }
                if row
                else None
            )
            packet["symbols"][ticker] = {
                "position": json_money(position),
                "valuation_price": live if row else None,
                "reference_price": selected,
                "news": view,
                "news_sources": sources,
                "earnings": self._facts(db, ticker, "earnings", now, settings),
                "earnings_history": self._earnings_history(db, ticker, now),
                "insiders": self._facts(db, ticker, "insiders", now, settings),
                "insider_qualification": "existing-0.7-rules",
            }
            if not selected:
                packet["gaps"].append(ticker + ": reference price unavailable")
            if row and row["value"] is None:
                packet["gaps"].append(ticker + ": portfolio value unavailable")
            if row and row["basis"] is None:
                packet["gaps"].append(ticker + ": historical FX/cost basis unavailable")
            statuses = []
            for feature, provider, attempted, succeeded, error in db.execute(
                "SELECT kind,source,attempted_at,succeeded_at,error FROM signal_sources WHERE kind IN ('earnings','insiders') AND symbol=? AND attempted_at<=?",
                (ticker, now),
            ):
                statuses.append(
                    {
                        "feature": feature,
                        "provider": provider,
                        "attempted_at": attempted,
                        "succeeded_at": succeeded if succeeded <= now else None,
                        "error": error,
                    }
                )
                if error:
                    packet["gaps"].append(ticker + ": " + feature + " source unavailable")
            packet["symbols"][ticker]["fundamental_sources"] = statuses
            for feature in ("earnings", "insiders"):
                if not any(s["feature"] == feature for s in statuses):
                    packet["gaps"].append(ticker + ": " + feature + " not collected")
            if not view or view.get("bias") is None:
                packet["gaps"].append(ticker + ": news signal unknown")
            elif view["stale"]:
                packet["gaps"].append(ticker + ": news aggregate stale")
        if not fx and currency == "EUR":
            packet["gaps"].append("Current reporting FX unavailable")
        return packet

    @staticmethod
    def _bounded(packet):
        raw = encoded(packet)
        if len(raw.encode()) > MAX_PACKET:
            for row in packet["symbols"].values():
                if row["news"]:
                    row["news"]["evidence"] = row["news"]["evidence"][:1]
                    for evidence in row["news"]["evidence"]:
                        evidence.pop("snippet", None)
                        evidence["analysis"]["supporting_spans"] = evidence["analysis"].get(
                            "supporting_spans", []
                        )[:3]
                        for vendor in evidence.get("vendor_scores", []):
                            vendor["reason"] = vendor.get("reason", "")[:160]
                            vendor["highlights"] = vendor.get("highlights", [])[:1]
                row["earnings"] = row["earnings"][:1]
                row["insiders"] = row["insiders"][:1]
            packet["truncated"].append(
                "Optional evidence reduced to one item per section/company; supporting spans and vendor explanations shortened"
            )
            raw = encoded(packet)
        if len(raw.encode()) > MAX_PACKET:
            raise ValueError("Snapshot exceeds 256 KiB; holdings were not discarded.")
        return raw

    def capture(self, kind="manual", session_date=None, scheduled_for=None, *, now=None):
        if kind not in {"manual", "scheduled"}:
            raise ValueError("Unknown snapshot kind.")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time() if now is None else now
            settings = self.store.settings(db)
            state = dict(
                db.execute(
                    "SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused')"
                )
            )
            if not math.isfinite(now) or state.get("storage_paused"):
                raise ValueError("Snapshot creation paused by storage health.")
            day = session_date or datetime.fromtimestamp(now, NY).date().isoformat()
            if kind == "scheduled":
                if (
                    settings["snapshot_enabled"] != "on"
                    or float(state.get("pause_until", "0")) > now
                ):
                    return None
                if scheduled_for is None or not 0 <= now - scheduled_for <= 300:
                    raise ValueError("Scheduled snapshot missed its capture window.")
                session = self.calendar.session(date.fromisoformat(day))
                if not session:
                    raise ValueError("Snapshot requires a verified US session.")
                if self.when(session, settings) != scheduled_for:
                    return None
                existing = self.repo.get(session_date=day, db=db)
                if existing:
                    return existing
            elif db.execute(
                "SELECT 1 FROM decision_snapshots WHERE kind='manual' AND as_of>?", (now - 60,)
            ).fetchone():
                raise ValueError("Wait one minute before another manual snapshot.")
            packet = self._packet(db, now, kind, day, scheduled_for)
            raw = self._bounded(packet)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            db.execute(
                "INSERT INTO decision_snapshots VALUES (?,?,?,?,?,?,?,?)",
                (packet["id"], kind, day, scheduled_for, now, __version__, raw, digest),
            )
            if kind == "scheduled":
                db.execute(
                    "INSERT INTO snapshot_runs(session_date,scheduled_for,status,snapshot_id) VALUES (?,?,'completed',?) ON CONFLICT(session_date) DO UPDATE SET status='completed',snapshot_id=excluded.snapshot_id,last_error=''",
                    (day, scheduled_for, packet["id"]),
                )
            self.repo.prune(now, db)
            return json.loads(raw)

    def next_session(self, now, settings=None):
        settings = settings or self.store.settings()
        today = datetime.fromtimestamp(now, NY).date()
        self.calendar.session(today)  # Verify current coverage even on a holiday.
        candidates = []
        for offset in range(-2, 11):
            try:
                session = self.calendar.session(today + timedelta(days=offset))
            except ValueError:
                continue
            if not session:
                continue
            stamp = self.when(session, settings)
            candidates.append((session.day, stamp))
        recent = [c for c in candidates if 0 <= now - c[1] < 86400]
        if not candidates:
            raise ValueError("Exchange calendar unavailable for this snapshot date.")
        future = min((c for c in candidates if c[1] > now), key=lambda c: c[1], default=None)
        if future and future[1] - now <= 600:
            return future
        return max(recent, key=lambda c: c[1]) if recent else future

    @staticmethod
    def when(session, settings):
        if settings["snapshot_time"] == "preopen-30m":
            return session.opens - 1800
        zone = ZoneInfo(settings["timezone"])
        local_day = datetime.fromtimestamp(session.opens, zone).date()
        return datetime.combine(
            local_day, datetime.strptime(settings["snapshot_time"], "%H:%M").time(), zone
        ).timestamp()

    def schedule(self, insights, now=None):
        fixed_time = now is not None
        now = time.time() if now is None else now
        settings = self.store.settings()
        if (
            settings["snapshot_enabled"] != "on"
            or self.store.automation_paused(now)
            or self.store.state("storage_paused")
        ):
            return
        try:
            candidate = self.next_session(now, settings)
        except ValueError:
            self.store.set_state(
                "snapshot_error", "Snapshot suspended: exchange calendar unavailable."
            )
            return
        if not candidate:
            return
        self.store.set_state("snapshot_error", "")
        day, scheduled = candidate
        if now < scheduled - 600:
            return
        with self.store.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO snapshot_runs(session_date,scheduled_for,status) VALUES (?,?,'pending')",
                (day, scheduled),
            )
            db.execute(
                "UPDATE snapshot_runs SET scheduled_for=? WHERE session_date=? AND status='pending'",
                (scheduled, day),
            )
            run = db.execute(
                "SELECT status,prepared_at FROM snapshot_runs WHERE session_date=?", (day,)
            ).fetchone()
        if run[0] in {"completed", "missed", "failed"}:
            return
        if now > scheduled + 300:
            with self.store.connection() as db:
                db.execute(
                    "UPDATE snapshot_runs SET status='missed',last_error='Capture deadline passed; no backfill' WHERE session_date=?",
                    (day,),
                )
            return
        if now < scheduled and run[1] is None:
            job = insights.queue(
                "news", origin="automatic", gate="snapshot", deadline=scheduled, session=day
            )
            if job is not None:
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE snapshot_runs SET prepared_at=? WHERE session_date=?", (now, day)
                    )
        if now >= scheduled:
            try:
                self.capture("scheduled", day, scheduled, now=now if fixed_time else None)
            except ValueError:
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE snapshot_runs SET status='failed',last_error='Capture failed; inspect snapshot/storage status' WHERE session_date=?",
                        (day,),
                    )

    def status(self):
        latest = self.repo.get()
        stamp = latest["session_date"] + " #" + latest["id"][:8] if latest else "none saved"
        settings = self.store.settings()
        text = f"Daily snapshots: {settings['snapshot_enabled']} • {settings['snapshot_time']} • latest {stamp}"
        with self.store.connection() as db:
            run = db.execute(
                "SELECT session_date,status,last_error FROM snapshot_runs ORDER BY session_date DESC LIMIT 1"
            ).fetchone()
        if run and run[1] in {"missed", "failed"}:
            text += f" • {run[0]} {run[1]}"
        if self.store.state("snapshot_error"):
            text += " • " + self.store.state("snapshot_error")
        return text

    def report(self, packet):
        zone = ZoneInfo(self.store.settings()["timezone"])
        when = datetime.fromtimestamp(packet["as_of"], zone).isoformat(timespec="seconds")
        lines = [
            f"📸 Snapshot #{packet['id'][:8]} • {packet['session_date']}",
            f"Captured: {when} • {packet['kind']}",
            f"Reporting currency: {packet['currency']} • {len(packet['symbols'])} companies",
        ]
        if packet["lateness_seconds"]:
            lines.append(f"Captured {int(packet['lateness_seconds'])} seconds late.")
        if packet["scheduled_for"] is not None:
            lines.append(
                "Scheduled for: "
                + datetime.fromtimestamp(packet["scheduled_for"], zone).isoformat(
                    timespec="seconds"
                )
            )
        for ticker, row in list(packet["symbols"].items())[:8]:
            news = row.get("news")
            text = (
                "news unknown"
                if not news or news.get("bias") is None
                else f"news {news['bias']:+.1f}"
            )
            if news and news.get("stale"):
                text += " (stale)"
            position = row.get("position")
            if position:
                text += f" • {position['quantity']} shares • value {position['value'] or 'unavailable'} {packet['currency']}"
            lines.append(f"• {ticker}: {text}")
        lines.append(
            f"Data gaps: {len(packet['gaps'])}"
            + (" • " + "; ".join(packet["gaps"][:3]) if packet["gaps"] else "")
        )
        if packet["truncated"]:
            lines.append("Optional evidence was trimmed to the storage limit.")
        lines.append("Frozen inputs; later data does not change this record.")
        return "\n".join(lines)

    def handle(self, args):
        if len(args) > 1:
            raise ValueError("Usage: /snapshot [latest|now|YYYY-MM-DD|on|off]")
        option = args[0].lower() if args else "latest"
        if option in {"on", "off"}:
            self.store.set_setting("snapshot_enabled", option)
            if option == "off":
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE signal_jobs SET status='cancelled' WHERE status IN ('pending','running') AND json_extract(symbols,'$.gate')='snapshot'"
                    )
            return f"Daily snapshots {option} • {self.store.settings()['snapshot_time']}. Enabling also prepares news ten minutes before capture. Automatic captures are silent."
        if option == "now":
            return self.report(self.capture())
        if option == "latest":
            packet = self.repo.get()
        else:
            try:
                parsed = date.fromisoformat(option)
                if parsed.isoformat() != option:
                    raise ValueError
            except ValueError:
                raise ValueError("Use latest, now, on, off or YYYY-MM-DD.") from None
            packet = self.repo.get(session_date=option)
            if not packet:
                with self.store.connection() as db:
                    row = db.execute(
                        "SELECT status,last_error FROM snapshot_runs WHERE session_date=?",
                        (option,),
                    ).fetchone()
                return (
                    f"Snapshot {option}: {row[0]}" + (f" • {row[1]}" if row and row[1] else "")
                    if row
                    else f"No saved automatic snapshot for {option} (not captured or expired)."
                )
        return (
            self.report(packet) + "\n" + self.status()
            if packet
            else self.status() + "\nNo decision snapshots yet. Use /snapshot now or /snapshot on."
        )
