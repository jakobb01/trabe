"""Bounded, immutable five-session research suggestions from cached evidence."""

import json
import time
import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trabe import __version__
from trabe.calendar import NY, USCalendar, retention_cutoff
from trabe.ledger import Ledger
from trabe.news_quality import VERSION, evidence, group_events
from trabe.news_store import NewsStore
from trabe.outlook import OutlookStore, digest, encoded
from trabe.snapshots import SnapshotService

RULES = "company-advisor-rules-v1"
PARAMETERS = {
    "sessions": 5,
    "news_age_seconds": 72 * 3600,
    "refresh_age_seconds": 10800,
    "minimum_stories": 2,
    "direction_bias": "0.2",
    "outcome_flat_band_percent": "1",
}
MAX_RECORDS = 256
MAX_PACKET = 64 * 1024
ADVISOR_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS company_advice(
        id TEXT PRIMARY KEY,ticker TEXT NOT NULL,session_date TEXT NOT NULL,
        as_of REAL NOT NULL,target_close REAL NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,
        UNIQUE(ticker,session_date))""",
    """CREATE TRIGGER IF NOT EXISTS immutable_company_advice BEFORE UPDATE ON company_advice
        BEGIN SELECT RAISE(ABORT,'Issued advice is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_replace_company_advice BEFORE INSERT ON company_advice
        WHEN EXISTS(SELECT 1 FROM company_advice WHERE id=NEW.id
        OR (ticker=NEW.ticker AND session_date=NEW.session_date))
        BEGIN SELECT RAISE(ABORT,'Issued advice cannot be replaced'); END""",
)


class Advisor:
    def __init__(self, store, calendar=None):
        self.store = store
        self.calendar = calendar or USCalendar()

    def get(self, ticker, db=None, session_date=None):
        if db is None:
            with self.store.connection() as connection:
                return self.get(ticker, connection, session_date)
        where = "ticker=?" + (" AND session_date=?" if session_date else "")
        args = (ticker, session_date) if session_date else (ticker,)
        row = db.execute(
            "SELECT payload,sha256 FROM company_advice WHERE "
            + where
            + " ORDER BY as_of DESC LIMIT 1",
            args,
        ).fetchone()
        if not row:
            return None
        packet = json.loads(row[0])
        if digest(packet) != row[1] or digest(packet["inputs"]) != packet["inputs_sha256"]:
            raise ValueError("Saved advisor integrity check failed.")
        return packet

    def prune(self, now, db):
        cutoff = retention_cutoff(datetime.fromtimestamp(now, NY).date()).isoformat()
        db.execute(
            "DELETE FROM company_advice WHERE session_date<=? AND target_close<?", (cutoff, now)
        )

    def evidence_inputs(self, db, ticker, now, first, *, settings=None):
        settings = self.store.settings(db) if settings is None else settings
        snapshots = SnapshotService(self.store, self.calendar)
        groups = group_events(NewsStore(self.store).records(ticker, now, db), ticker, now)
        groups.sort(key=lambda g: g["published_at"], reverse=True)
        stories = []
        for group in groups:
            row = group["representative"]
            if row["analysis"].get("relevance") != "direct":
                continue
            item = evidence(group)
            item.update(
                conflict=group["conflict"], headline=row["headline"], snippet=row["snippet"]
            )
            stories.append(item)
            if len(stories) == 5:
                break
        outlook = OutlookStore(self.store).get(session_date=first.day, db=db)
        if outlook and outlook["as_of"] > now:
            outlook = None
        return {
            "news": stories,
            "news_truncated": len(groups) > len(stories),
            "news_quality_version": VERSION,
            "settings": {
                key: settings[key]
                for key in ("max_source_age_seconds", "insider_min_value_usd", "news_sources")
            },
            "news_sources": snapshots._news_sources(db, ticker, now, settings),
            "earnings": snapshots._facts(db, ticker, "earnings", now, settings),
            "insiders": snapshots._facts(db, ticker, "insiders", now, settings),
            "fundamental_sources": [
                dict(zip(("kind", "source", "attempted_at", "succeeded_at", "error"), row))
                for row in db.execute(
                    "SELECT kind,source,attempted_at,succeeded_at,error FROM signal_sources WHERE kind IN ('earnings','insiders') AND symbol=? AND attempted_at<=?",
                    (ticker, now),
                )
            ],
            "market_outlook": {
                k: outlook[k] for k in ("id", "as_of", "session_date", "forecast", "inputs_sha256")
            }
            if outlook
            else None,
        }

    def _inputs(self, db, ticker, now, first):
        inputs = self.evidence_inputs(db, ticker, now, first)
        settings = self.store.settings(db)
        positions = Ledger(self.store).positions(settings["default_currency"], db)
        inputs["position_quantity"] = str(positions.get(ticker, (Decimal(0),))[0])
        inputs["price"] = SnapshotService(self.store, self.calendar)._price(
            self.store.cached_live("finnhub", ticker, db),
            now,
            "research_reference",
            max_age=int(settings["max_source_age_seconds"]),
        )
        return inputs

    @staticmethod
    def infer(inputs, now, last):
        gaps, risks, reasons = [], [], []
        price = inputs["price"]
        if not price or price["currency"] != "USD" or price["quality"]:
            gaps.append("Usable company price reference missing/stale or flagged.")
        sources = inputs["news_sources"]
        if not any(
            s["succeeded_at"] is not None
            and 0 <= now - s["succeeded_at"] <= PARAMETERS["refresh_age_seconds"]
            for s in sources
        ):
            gaps.append("Company news has no successful refresh within three hours.")
        if any(s["error"] for s in sources):
            gaps.append("Latest company-news collection has source failures.")
        eligible = [
            s
            for s in inputs["news"]
            if 0 <= now - s["published_at"] <= PARAMETERS["news_age_seconds"]
            and s["known_at"] <= now
            and s["source_published_at"] <= now
            and s["input_quality"] in ("article", "snippet")
            and s["snippet"].strip()
            and s["analysis"].get("bias") is not None
        ]
        if len(eligible) < PARAMETERS["minimum_stories"]:
            gaps.append("Need two distinct recent company stories with extracts.")
        signs = set()
        for story in eligible:
            bias = Decimal(str(story["analysis"]["bias"]))
            band = Decimal(PARAMETERS["direction_bias"])
            signs.add("bullish" if bias >= band else "bearish" if bias <= -band else "mixed")
            reasons.append(story["headline"][:200])
        conflict = any(s["conflict"] or s["analysis"].get("mixed") for s in eligible)
        direction = (
            "insufficient"
            if gaps
            else (next(iter(signs)) if len(signs) == 1 and not conflict else "mixed")
        )
        if conflict or len(signs) > 1:
            risks.append("Company stories disagree; no single directional signal.")
        for row in inputs["earnings"]:
            day = row["facts"].get("date")
            if day and datetime.fromtimestamp(now, NY).date().isoformat() <= day <= last.day:
                risks.append(
                    f"Earnings dated {day} can change the thesis; timing/estimates are provider evidence."
                )
        for kind in ("earnings", "insiders"):
            statuses = [s for s in inputs["fundamental_sources"] if s["kind"] == kind]
            if (
                not statuses
                or any(s["error"] for s in statuses)
                or not any(0 <= now - s["succeeded_at"] <= 86400 for s in statuses)
            ):
                risks.append(f"{kind.capitalize()} coverage missing/stale or failed.")
        if inputs["insiders"]:
            risks.append(
                "Qualified insider purchases are background context, not a five-session timing signal."
            )
        outlook = inputs["market_outlook"]
        if outlook:
            market_bias = outlook["forecast"]["session"]
            risks.append(
                f"Saved first-session market bias: {market_bias}; it does not cover this five-session horizon."
            )
        else:
            risks.append("No saved market outlook for the first target session.")
        if inputs["news_truncated"]:
            risks.append("News review is bounded to five direct company story groups.")
        held = Decimal(inputs["position_quantity"]) > 0
        action = (
            "research adding"
            if direction == "bullish"
            else "review reducing"
            if direction == "bearish" and held
            else "avoid adding"
            if direction == "bearish"
            else "wait for evidence"
            if gaps
            else "hold/watch"
        )
        if any(r.startswith("Earnings dated") for r in risks) and direction == "bullish":
            action = "wait for earnings"
        return {
            "direction": direction,
            "action": action,
            "reasons": reasons[:3],
            "risks": risks,
            "gaps": gaps,
            "confidence": None,
        }

    def issue(self, ticker, now=None):
        now = time.time() if now is None else now
        sessions = [self.calendar.next_session(now)]
        for _ in range(PARAMETERS["sessions"] - 1):
            sessions.append(self.calendar.next_session(sessions[-1].closes))
        first, last = sessions[0], sessions[-1]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.get(ticker, db, first.day)
            if existing:
                return existing
            paused = db.execute("SELECT value FROM state WHERE key='storage_paused'").fetchone()
            if paused and paused[0]:
                raise ValueError("Storage is paused; saved advisor views remain available.")
            positions = Ledger(self.store).positions(
                self.store.settings(db)["default_currency"], db
            )
            if ticker not in dict(self.store.watchlist(db)) and positions.get(ticker, (0,))[0] <= 0:
                raise ValueError("Add this symbol to the watchlist or record a position first.")
            self.prune(now, db)
            if db.execute("SELECT COUNT(*) FROM company_advice").fetchone()[0] >= MAX_RECORDS:
                raise ValueError("Advisor retention limit reached; saved records remain available.")
            inputs = self._inputs(db, ticker, now, first)
            packet = {
                "id": uuid.uuid4().hex[:16],
                "ticker": ticker,
                "as_of": now,
                "schema_version": 1,
                "app_version": __version__,
                "rule_version": RULES,
                "parameters": dict(PARAMETERS),
                "calendar_version": self.calendar.data.get("version"),
                "sessions": [s.day for s in sessions],
                "opens": first.opens,
                "closes": last.closes,
                "inputs": inputs,
                "inputs_sha256": digest(inputs),
                "forecast": self.infer(inputs, now, last),
                "assessment_contract": "Company first regular-minute open to fifth-session last regular-minute close; ±1% flat band. SPY over identical endpoints is the benchmark. Missing endpoints/abstentions unscored; no fees, dividends or simulated fills.",
                "assessment": {
                    "version": "company-advisor-assessment-v1",
                    "benchmark": "SPY",
                    "start": "first regular-minute open",
                    "end": "fifth-session last regular-minute close",
                    "flat_band_percent": "1",
                    "return": "100 * (end / start - 1)",
                    "bullish": ">= 1%",
                    "bearish": "<= -1%",
                    "mixed": "between -1% and 1%",
                    "missing": "unscored",
                    "insufficient": "unscored",
                },
            }
            raw = encoded(packet)
            if len(raw.encode()) > MAX_PACKET:
                raise ValueError("Advisor evidence exceeds packet limit; no suggestion was saved.")
            db.execute(
                "INSERT INTO company_advice VALUES (?,?,?,?,?,?,?)",
                (packet["id"], ticker, first.day, now, last.closes, raw, digest(packet)),
            )
            return packet

    def report(self, packet, details=False):
        zone = ZoneInfo(self.store.settings()["timezone"])
        stamp = datetime.fromtimestamp(packet["as_of"], zone).strftime("%d %b %H:%M %Z")
        forecast = packet["forecast"]
        lines = [
            f"🔎 {packet['ticker']} research (rules) • issued {stamp}",
            f"Next five US sessions: {packet['sessions'][0]}–{packet['sessions'][-1]}",
            f"Expectation: {forecast['direction']} • suggestion: {forecast['action']}",
            "Reasons: "
            + ("; ".join(forecast["reasons"]) or "No qualifying company-news evidence."),
            "Gaps: " + (" ".join(forecast["gaps"]) or "No required-input gaps."),
            "Risks: " + " ".join(forecast["risks"]),
            "Unvalidated research baseline; confidence uncalibrated. No trade is placed.",
        ]
        if details:
            lines.extend(
                [
                    f"Record {packet['id']} • {packet['rule_version']}",
                    packet["assessment_contract"],
                    f"Input hash: {packet['inputs_sha256']}",
                ]
            )
            price = packet["inputs"]["price"]
            if price:
                lines.append(
                    f"Reference: {price['price']} {price['currency']} • {price['provider']} • {price['kind']} • source time {price['source_time']}"
                )
            for story in packet["inputs"]["news"]:
                published = datetime.fromtimestamp(story["published_at"], zone).isoformat(
                    timespec="minutes"
                )
                lines.extend(
                    [
                        f"{story['headline']} • {published} • {story['input_quality']}",
                        story["snippet"][:800],
                        story["url"],
                    ]
                )
        else:
            lines.append(f"Saved evidence: /advisor evidence {packet['ticker']}")
        return "\n".join(lines)

    def handle(self, args):
        from trabe.advisor_results import AdvisorResults, record_id
        from trabe.bot import symbol

        results = AdvisorResults(self.store, self.calendar)
        option = args[0].lower() if args else ""
        if option == "results" and len(args) in (1, 2):
            return results.results(symbol(args[1]) if len(args) == 2 else None)
        if option == "result" and len(args) == 2:
            return results.results(advice_id=args[1])
        if option == "record" and len(args) == 2:
            with self.store.connection() as db:
                packet = results.unpack(
                    db.execute(
                        "SELECT payload,sha256 FROM company_advice WHERE id=?",
                        (record_id(args[1]),),
                    ).fetchone()
                )
            if not packet:
                raise ValueError("Unknown saved advisor record ID.")
            return self.report(packet, details=True)
        if option == "track" and len(args) == 2 and args[1] in ("on", "off"):
            self.store.set_setting("advisor_results_enabled", args[1])
            return results.status()
        if option in {"results", "result", "record", "track", "cached", "evidence"} and not (
            len(args) == 2 and option in {"cached", "evidence"}
        ):
            raise ValueError(
                "Usage: /advisor results [TICKER] | result ID | record ID | track on|off | cached TICKER | evidence TICKER"
            )
        if not args:
            return (
                "/advisor TICKER saves a five-session research suggestion from cached inputs. "
                "One immutable suggestion per ticker/first target session; refresh /news TICKER before issuing if needed. "
                "/advisor cached TICKER reads it; /advisor evidence TICKER shows sources. "
                "/advisor results [TICKER] and result ID read outcomes; record ID reads the original. "
                "/advisor track on|off controls silent collection. Records retained three calendar months (256 maximum)."
            )
        if len(args) == 1:
            return self.report(self.issue(symbol(args[0])))
        if len(args) == 2 and args[0].lower() in ("cached", "evidence"):
            packet = self.get(symbol(args[1]))
            return (
                self.report(packet, args[0].lower() == "evidence")
                if packet
                else "No saved company research for this symbol."
            )
        raise ValueError("Usage: /advisor [TICKER|cached TICKER|evidence TICKER]")
