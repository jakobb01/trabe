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
COMPARISON = "company-advisor-comparison-v1"
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
STORY_DISAGREEMENT = "Company stories disagree; no single directional signal."
DIRECTIONAL = frozenset({"bullish", "bearish"})
CONSENSUS = "non_neutral_consensus"
MEAN_BIAS = "mean_bias"
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


class AdvisorPacketTooLarge(ValueError):
    pass


def bias_category(bias, threshold):
    return "bullish" if bias >= threshold else "bearish" if bias <= -threshold else "mixed"


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
    def eligible_stories(inputs, now):
        return [
            s
            for s in inputs["news"]
            if 0 <= now - s["published_at"] <= PARAMETERS["news_age_seconds"]
            and s["known_at"] <= now
            and s["source_published_at"] <= now
            and s["input_quality"] in ("article", "snippet")
            and s["snippet"].strip()
            and s["analysis"].get("bias") is not None
        ]

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
        eligible = Advisor.eligible_stories(inputs, now)
        if len(eligible) < PARAMETERS["minimum_stories"]:
            gaps.append("Need two distinct recent company stories with extracts.")
        signs = set()
        band = Decimal(PARAMETERS["direction_bias"])
        for story in eligible:
            bias = Decimal(str(story["analysis"]["bias"]))
            signs.add(bias_category(bias, band))
            reasons.append(story["headline"][:200])
        conflict = any(s["conflict"] or s["analysis"].get("mixed") for s in eligible)
        direction = (
            "insufficient"
            if gaps
            else (next(iter(signs)) if len(signs) == 1 and not conflict else "mixed")
        )
        if conflict or len(signs) > 1:
            risks.append(STORY_DISAGREEMENT)
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

    @staticmethod
    def comparison(inputs, now, forecast):
        stories = Advisor.eligible_stories(inputs, now)
        biases = [Decimal(str(story["analysis"]["bias"])) for story in stories]
        conflict = any(story["conflict"] or story["analysis"].get("mixed") for story in stories)
        band = Decimal(PARAMETERS["direction_bias"])
        mean_band = band / 2
        votes = {bias_category(bias, band) for bias in biases} & DIRECTIONAL
        consensus = mean = forecast["direction"]
        if not forecast["gaps"]:
            consensus = next(iter(votes)) if len(votes) == 1 and not conflict else "mixed"
            mean = "mixed" if conflict else bias_category(sum(biases) / len(biases), mean_band)
        return {
            "version": COMPARISON,
            "story_band": str(band),
            "mean_band": str(mean_band),
            "eligible_biases": [str(bias) for bias in biases],
            "story_conflict": conflict,
            "opposing_votes": len(votes) > 1,
            "directions": {CONSENSUS: consensus, MEAN_BIAS: mean},
        }

    def _sessions(self, now):
        sessions = [self.calendar.next_session(now)]
        for _ in range(PARAMETERS["sessions"] - 1):
            sessions.append(self.calendar.next_session(sessions[-1].closes))
        return sessions

    def _check_coverage(self, ticker, db):
        positions = Ledger(self.store).positions(self.store.settings(db)["default_currency"], db)
        if ticker not in dict(self.store.watchlist(db)) and positions.get(ticker, (0,))[0] <= 0:
            raise ValueError("Add this symbol to the watchlist or record a position first.")

    def issue(self, ticker, now=None):
        now = time.time() if now is None else now
        sessions = self._sessions(now)
        first = sessions[0]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.get(ticker, db, first.day)
            if existing:
                return existing
            paused = db.execute("SELECT value FROM state WHERE key='storage_paused'").fetchone()
            if paused and paused[0]:
                raise ValueError("Storage is paused; saved advisor views remain available.")
            self._check_coverage(ticker, db)
            self.prune(now, db)
            if db.execute("SELECT COUNT(*) FROM company_advice").fetchone()[0] >= MAX_RECORDS:
                raise ValueError("Advisor retention limit reached; saved records remain available.")
            inputs = self._inputs(db, ticker, now, first)
            packet = self.freeze(ticker, now, sessions, inputs)
            self.save(db, packet)
            return packet

    def freeze(self, ticker, now, sessions, inputs):
        first, last = sessions[0], sessions[-1]
        forecast = self.infer(inputs, now, last)
        return {
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
            "forecast": forecast,
            "comparison": self.comparison(inputs, now, forecast),
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

    @staticmethod
    def save(db, packet, *, shadow=False):
        raw = encoded(packet)
        if len(raw.encode()) > MAX_PACKET:
            raise AdvisorPacketTooLarge(
                "Advisor evidence exceeds packet limit; no suggestion was saved."
            )
        table = "shadow_company_advice" if shadow else "company_advice"
        db.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
            (
                packet["id"],
                packet["ticker"],
                packet["sessions"][0],
                packet["as_of"],
                packet["closes"],
                raw,
                digest(packet),
            ),
        )

    @staticmethod
    def _forecast_lines(forecast, flat_band):
        direction = forecast["direction"]
        expectation = {
            "mixed": "Mixed • no directional signal",
            "insufficient": "No forecast • required evidence missing (unscored)",
        }.get(direction, direction)
        lines = [f"Expectation: {expectation} • suggestion: {forecast['action']}"]
        if direction == "mixed":
            if STORY_DISAGREEMENT not in forecast["risks"]:
                lines.append("Qualifying company news was neutral under the rules.")
            lines.append(
                f"A saved mixed call matches a five-session return strictly between −{flat_band}% and +{flat_band}%."
            )
        lines.extend(
            [
                "Reasons: "
                + ("; ".join(forecast["reasons"]) or "No qualifying company-news evidence."),
                "Gaps: " + (" ".join(forecast["gaps"]) or "No required-input gaps."),
                "Risks: " + " ".join(forecast["risks"]),
                "Unvalidated research baseline; confidence uncalibrated. No trade is placed.",
            ]
        )
        return lines

    def preview(self, ticker, now=None):
        now = time.time() if now is None else now
        sessions = self._sessions(now)
        first, last = sessions[0], sessions[-1]
        with self.store.connection() as db:
            self._check_coverage(ticker, db)
            existing = self.get(ticker, db, first.day)
            inputs = self._inputs(db, ticker, now, first)
        forecast = self.infer(inputs, now, last)
        lines = [
            f"🔎 {ticker} preview (rules) • current cached evidence",
            f"Target US sessions: {first.day}–{last.day}",
            "Preview only. It creates no record and is never scored.",
            *self._forecast_lines(forecast, PARAMETERS["outcome_flat_band_percent"]),
        ]
        if existing:
            lines.append(
                f"This window already has a frozen record. /advisor record {existing['id']}"
            )
        elif not forecast["gaps"]:
            lines.append(f"/advisor {ticker} explicitly saves a record for this window.")
        if forecast["gaps"]:
            if any("news" in gap.lower() or "stories" in gap for gap in forecast["gaps"]):
                lines.append(
                    f"Refresh sources with /news refresh {ticker}, then check the preview."
                )
            if any("price reference" in gap for gap in forecast["gaps"]):
                lines.append(f"Check Finnhub quote availability with /prices or /quote {ticker}.")
        return "\n".join(lines)

    def report(self, packet, details=False, *, now=None):
        zone = ZoneInfo(self.store.settings()["timezone"])
        stamp = datetime.fromtimestamp(packet["as_of"], zone).strftime("%d %b %H:%M %Z")
        lines = [
            f"🔎 {packet['ticker']} research (rules) • issued {stamp}",
            f"Target US sessions: {packet['sessions'][0]}–{packet['sessions'][-1]}",
        ]
        if now is not None:
            if now >= packet["closes"]:
                lines.append("Target window complete • historical forecast")
            elif now >= packet["opens"]:
                lines.append("Target window underway • frozen forecast")
            else:
                lines.append("Target window upcoming • frozen forecast")
        flat_band = packet["assessment"]["flat_band_percent"]
        lines.extend(self._forecast_lines(packet["forecast"], flat_band))
        if details:
            lines.extend(
                [
                    f"Record {packet['id']} • {packet['rule_version']}",
                    packet["assessment_contract"],
                    f"Input hash: {packet['inputs_sha256']}",
                ]
            )
            comparison = packet.get("comparison")
            if comparison and comparison["version"] == COMPARISON:
                directions = comparison["directions"]
                lines.append(
                    f"Frozen comparison: non-neutral consensus {directions[CONSENSUS]}, "
                    f"mean story bias {directions[MEAN_BIAS]}."
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
            lines.append(f"/advisor preview {packet['ticker']} checks the next eligible window.")
        return "\n".join(lines)

    def handle(self, args):
        from trabe.advisor_comparison import comparison_report
        from trabe.advisor_results import AdvisorResults, record_id
        from trabe.advisor_shadow import AdvisorShadow
        from trabe.bot import symbol

        results = AdvisorResults(self.store, self.calendar)
        option = args[0].lower() if args else ""
        if option == "results" and len(args) in (1, 2):
            return results.results(symbol(args[1]) if len(args) == 2 else None)
        if option == "result" and len(args) == 2:
            return results.results(advice_id=args[1])
        if option == "compare" and len(args) in (1, 2):
            return comparison_report(
                self.store, self.calendar, symbol(args[1]) if len(args) == 2 else None
            )
        if option == "shadow" and len(args) in (1, 2):
            if len(args) == 2:
                self.store.set_setting("advisor_shadow_enabled", args[1])
            return AdvisorShadow(self.store, self.calendar).status()
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
        views = {"cached", "evidence", "preview"}
        if option in {"results", "result", "record", "track", "compare", "shadow"} | views and not (
            len(args) == 2 and option in views
        ):
            raise ValueError(
                "Usage: /advisor results [TICKER] | result ID | record ID | track on|off | "
                "compare [TICKER] | shadow [on|off] | cached TICKER | evidence TICKER | preview TICKER"
            )
        if not args:
            return (
                "/advisor TICKER saves a five-session research suggestion from cached inputs. "
                "One immutable record per ticker/first target session, including abstentions. "
                "/advisor preview TICKER checks cached evidence before saving. "
                "/advisor cached TICKER shows the last saved record or a preview; /advisor evidence TICKER shows saved sources. "
                "/advisor results [TICKER] and result ID read outcomes; record ID reads the original. "
                "/advisor compare [TICKER] compares frozen methods; /advisor shadow [on|off] shows or controls automatic sampling. "
                "/advisor track on|off controls silent collection. Records retained three calendar months (256 maximum)."
            )
        if len(args) == 1:
            now = time.time()
            return self.report(self.issue(symbol(args[0]), now), now=now)
        if len(args) == 2 and option in views:
            ticker = symbol(args[1])
            if option == "preview":
                return self.preview(ticker)
            packet = self.get(ticker)
            if packet:
                return self.report(
                    packet,
                    details=option == "evidence",
                    now=time.time() if option == "cached" else None,
                )
            if option == "cached":
                return f"No saved company research for {ticker}.\n" + self.preview(ticker)
            return f"No saved company research for {ticker}. /advisor preview {ticker} checks cached evidence."
        raise ValueError("Usage: /advisor [TICKER|cached TICKER|evidence TICKER|preview TICKER]")
