"""Separate five-session company assessments; forecasts and their bytes never change."""

import re
import time
from datetime import date
from decimal import Decimal, localcontext

from trabe.advisor import RULES
from trabe.api import ApiError
from trabe.calendar import USCalendar
from trabe.market_context import SOURCE, number
from trabe.outlook import digest, encoded
from trabe.price_endpoints import METHOD, QUALITY, EndpointStore, checked, enabled

ASSESSMENT = "company-advisor-assessment-v1"
RESULTS_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS advisor_outcome_inputs(
        advice_id TEXT NOT NULL REFERENCES company_advice(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('company','benchmark')),
        edge TEXT NOT NULL CHECK(edge IN ('open','close')),symbol TEXT NOT NULL,
        session_date TEXT NOT NULL,expected_at REAL NOT NULL,ready_at REAL NOT NULL,expires_at REAL NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','saved','missing','unsupported')),
        endpoint_id TEXT REFERENCES market_price_endpoints(id) ON DELETE RESTRICT,
        last_error TEXT NOT NULL DEFAULT '',PRIMARY KEY(advice_id,role,edge))""",
    "CREATE INDEX IF NOT EXISTS advisor_inputs_due ON advisor_outcome_inputs(state,ready_at,expires_at)",
    "CREATE INDEX IF NOT EXISTS advisor_inputs_endpoint ON advisor_outcome_inputs(endpoint_id)",
    """CREATE TABLE IF NOT EXISTS advisor_evaluations(
        advice_id TEXT PRIMARY KEY REFERENCES company_advice(id) ON DELETE CASCADE,
        evaluated_at REAL NOT NULL,method TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('scored','abstained','unscorable','unsupported')),
        payload TEXT NOT NULL,sha256 TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS immutable_advisor_result BEFORE UPDATE ON advisor_evaluations
        BEGIN SELECT RAISE(ABORT,'Company assessments are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_replace_advisor_result BEFORE INSERT ON advisor_evaluations
        WHEN EXISTS(SELECT 1 FROM advisor_evaluations WHERE advice_id=NEW.advice_id)
        BEGIN SELECT RAISE(ABORT,'Company assessments cannot be replaced'); END""",
)


def record_id(value):
    if not re.fullmatch(r"[0-9a-f]{16}", value):
        raise ValueError("Use an exact saved advisor record ID (16 lowercase hex characters).")
    return value


class AdvisorResults:
    def __init__(self, store, calendar=None, *, clock=time.time):
        self.store, self.calendar, self.clock = store, calendar or USCalendar(), clock
        self.endpoints = EndpointStore(store, self.calendar)

    def _sessions(self, packet):
        try:
            days = packet["sessions"]
            if len(days) != 5 or len(set(days)) != 5:
                raise ValueError("Invalid five-session horizon.")
            sessions = [self.calendar.session(date.fromisoformat(d)) for d in days]
            if any(s is None for s in sessions):
                raise ValueError("Unverified session dates.")
            expected = self.calendar.next_session(packet["as_of"])
            for session in sessions:
                if expected != session:
                    raise ValueError("Contradictory frozen session dates.")
                if session != sessions[-1]:
                    expected = self.calendar.next_session(session.closes)
            if packet["opens"] != sessions[0].opens or packet["closes"] != sessions[-1].closes:
                raise ValueError("Contradictory frozen session boundaries.")
            return sessions
        except (KeyError, TypeError, IndexError, OverflowError) as error:
            raise ValueError("Invalid frozen horizon.") from error

    @staticmethod
    def supported(packet):
        contract = packet.get("assessment", {})
        return (
            packet.get("schema_version") == 1
            and packet.get("rule_version") == RULES
            and contract.get("version") == ASSESSMENT
            and contract.get("benchmark") == "SPY"
            and contract.get("flat_band_percent") == "1"
            and contract.get("start") == "first regular-minute open"
            and contract.get("end") == "fifth-session last regular-minute close"
            and contract.get("return") == "100 * (end / start - 1)"
            and packet.get("forecast", {}).get("direction")
            in {"bullish", "bearish", "mixed", "insufficient"}
        )

    @staticmethod
    def unpack(row):
        packet = checked(row)
        if packet and digest(packet["inputs"]) != packet["inputs_sha256"]:
            raise ValueError("Saved advisor input integrity check failed.")
        return packet

    def ensure_pending(self, *, now, limit=16):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not enabled(self.store, db, "advisor_results", now):
                return 0
            rows = db.execute(
                "SELECT a.payload,a.sha256 FROM company_advice a LEFT JOIN advisor_evaluations e ON e.advice_id=a.id WHERE e.advice_id IS NULL AND (SELECT COUNT(*) FROM advisor_outcome_inputs i WHERE i.advice_id=a.id)!=4 ORDER BY a.as_of,a.id LIMIT ?",
                (min(16, max(1, limit)),),
            ).fetchall()
            for row in rows:
                packet = self.unpack(row)
                try:
                    sessions = self._sessions(packet)
                except ValueError:
                    self._save(db, self.assess(packet, {}, evaluated_at=now))
                    continue
                supported = self.supported(packet) and packet["as_of"] <= now
                for role, symbol in (("company", packet["ticker"]), ("benchmark", "SPY")):
                    for edge, session in (("open", sessions[0]), ("close", sessions[-1])):
                        expected = session.opens if edge == "open" else session.closes - 60
                        ready = session.opens + 300 if edge == "open" else session.closes + 1800
                        db.execute(
                            "INSERT OR IGNORE INTO advisor_outcome_inputs VALUES (?,?,?,?,?,?,?,?,?,NULL,?)",
                            (
                                packet["id"],
                                role,
                                edge,
                                symbol,
                                session.day,
                                expected,
                                ready,
                                session.closes + 7 * 86400,
                                "pending" if supported else "unsupported",
                                "" if supported else "Unsupported/future advice contract",
                            ),
                        )
            return len(rows)

    @staticmethod
    def _save(db, value):
        if db.execute(
            "SELECT 1 FROM advisor_evaluations WHERE advice_id=?", (value["advice_id"],)
        ).fetchone():
            return
        raw = encoded(value)
        if len(raw.encode()) > 16384:
            raise ValueError("Company assessment exceeds 16 KiB.")
        db.execute(
            "INSERT INTO advisor_evaluations VALUES (?,?,?,?,?,?)",
            (
                value["advice_id"],
                value["evaluated_at"],
                ASSESSMENT,
                value["status"],
                raw,
                digest(value),
            ),
        )

    def _advance(self, now):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not enabled(self.store, db, "advisor_results", now):
                return
            refs = db.execute(
                "SELECT advice_id,role,edge,symbol,session_date,expires_at FROM advisor_outcome_inputs WHERE state='pending' ORDER BY expires_at,advice_id,role,edge"
            ).fetchall()
            for advice_id, role, edge, symbol, day, expires in refs:
                value = self.endpoints.get(symbol, day, edge, db=db)
                if value and value["received_at"] <= now:
                    endpoint = db.execute(
                        "SELECT id FROM market_price_endpoints WHERE source=? AND symbol=? AND session_date=? AND edge=? AND method=?",
                        (SOURCE, symbol, day, edge, METHOD),
                    ).fetchone()[0]
                    db.execute(
                        "UPDATE advisor_outcome_inputs SET state='saved',endpoint_id=? WHERE advice_id=? AND role=? AND edge=?",
                        (endpoint, advice_id, role, edge),
                    )
                elif now >= expires:
                    db.execute(
                        "UPDATE advisor_outcome_inputs SET state='missing',last_error='Exact endpoint missing after seven-day retry cutoff' WHERE advice_id=? AND role=? AND edge=?",
                        (advice_id, role, edge),
                    )
            rows = db.execute(
                "SELECT a.payload,a.sha256 FROM company_advice a LEFT JOIN advisor_evaluations e ON e.advice_id=a.id WHERE e.advice_id IS NULL AND a.target_close+1800<=? AND (SELECT COUNT(*) FROM advisor_outcome_inputs i WHERE i.advice_id=a.id)=4 AND NOT EXISTS(SELECT 1 FROM advisor_outcome_inputs i WHERE i.advice_id=a.id AND i.state='pending') ORDER BY a.target_close LIMIT 16",
                (now,),
            ).fetchall()
            for row in rows:
                packet = self.unpack(row)
                observations = {}
                for role, edge, payload, sha, error in db.execute(
                    "SELECT i.role,i.edge,p.payload,p.sha256,i.last_error FROM advisor_outcome_inputs i LEFT JOIN market_price_endpoints p ON p.id=i.endpoint_id WHERE i.advice_id=?",
                    (packet["id"],),
                ):
                    observations[role + "/" + edge] = (
                        checked((payload, sha)) if payload else {"gap": error or "Endpoint missing"}
                    )
                self._save(db, self.assess(packet, observations, evaluated_at=now))

    def run_once(self, http, *, now):
        with self.store.connection() as db:
            if not enabled(self.store, db, "advisor_results", now):
                return False
        self.ensure_pending(now=now)
        self._advance(now)
        with self.store.connection() as db:
            due = db.execute(
                "SELECT symbol,session_date,GROUP_CONCAT(DISTINCT edge) FROM (SELECT * FROM advisor_outcome_inputs WHERE state='pending' AND ready_at<=? AND expires_at>? ORDER BY expires_at,advice_id,role,edge) GROUP BY symbol,session_date ORDER BY MIN(expires_at),MIN(advice_id),symbol",
                (now, now),
            ).fetchall()
        attempts = 0
        for symbol, day, edges in due:
            if attempts >= 2:
                break
            session = self.calendar.session(date.fromisoformat(day))
            if not session:
                continue
            try:
                attempts += bool(
                    self.endpoints.collect(
                        http,
                        symbol,
                        session,
                        edges.split(","),
                        feature="advisor_results",
                        now=now,
                        clock=self.clock,
                    )
                )
            except ApiError:
                attempts += 1
        self._advance(max(now, self.clock()))
        return bool(attempts)

    def assess(self, packet, observations, *, evaluated_at):
        gaps, returns = [], {}
        supported = self.supported(packet)
        try:
            self._sessions(packet)
            if packet["as_of"] > evaluated_at:
                raise ValueError("Future advice.")
        except ValueError:
            supported = False
            gaps.append("Unsupported/contradictory frozen horizon or future advice.")
        used = {}
        for role in ("company", "benchmark"):
            pair = [observations.get(role + "/" + edge) for edge in ("open", "close")]
            usable = True
            for edge, value in zip(("open", "close"), pair):
                key = role + "/" + edge
                if not value or "gap" in value:
                    gaps.append(key + ": " + ((value or {}).get("gap", "missing endpoint")))
                    usable = False
                    continue
                used[key] = {"value": value, "sha256": digest(value)}
                try:
                    number(value.get("price"))
                except ValueError:
                    gaps.append(key + ": invalid price")
                    usable = False
                expected_day = (
                    packet["sessions"][0 if edge == "open" else -1]
                    if packet.get("sessions")
                    else None
                )
                expected_time = (
                    packet.get("opens") if edge == "open" else packet.get("closes", 0) - 60
                )
                if (
                    value.get("symbol") != (packet["ticker"] if role == "company" else "SPY")
                    or value.get("session_date") != expected_day
                    or value.get("edge") != edge
                    or value.get("bar_time") != expected_time
                    or value.get("received_at", evaluated_at + 1) > evaluated_at
                    or value.get("method") != METHOD
                    or value.get("currency") != "USD"
                    or value.get("source") != SOURCE
                    or value.get("provider_symbol") != value.get("symbol", "").replace(".", "-")
                    or value.get("instrument_type")
                    not in ({"ETF"} if role == "benchmark" else {"EQUITY", "ETF"})
                    or value.get("timezone") != "America/New_York"
                    or value.get("received_at", 0)
                    < (
                        packet.get("opens", 0) + 300
                        if edge == "open"
                        else packet.get("closes", 0) + 1800
                    )
                    or any(
                        packet.get("opens", 0) <= a["at"] <= packet.get("closes", 0)
                        for a in value.get("actions", [])
                    )
                ):
                    gaps.append(key + ": identity/time/corporate-action conflict")
                    usable = False
            with localcontext() as ctx:
                ctx.prec = 28
                returns[role] = (
                    100 * (Decimal(pair[1]["price"]) / Decimal(pair[0]["price"]) - 1)
                    if usable
                    else None
                )
        value = returns["company"]
        expected = packet.get("forecast", {}).get("direction")
        status = (
            "unsupported"
            if not supported
            else "abstained"
            if expected == "insufficient"
            else "unscorable"
            if value is None
            else "scored"
        )
        actual = (
            None
            if value is None
            else "bullish"
            if value >= 1
            else "bearish"
            if value <= -1
            else "mixed"
        )
        spy = returns["benchmark"]
        return {
            "schema_version": 1,
            "method": ASSESSMENT,
            "advice_id": packet["id"],
            "evaluated_at": evaluated_at,
            "status": status,
            "rule_version": packet.get("rule_version"),
            "ticker": packet["ticker"],
            "sessions": packet.get("sessions", []),
            "opens": packet.get("opens"),
            "closes": packet.get("closes"),
            "flat_band_percent": "1",
            "expected": expected,
            "realized": actual,
            "matched": expected == actual if status == "scored" else None,
            "company_return": str(value) if value is not None else None,
            "spy_return": str(spy) if spy is not None else None,
            "excess_percentage_points": str(value - spy)
            if value is not None and spy is not None
            else None,
            "observations": used,
            "gaps": gaps,
            "quality": QUALITY,
        }

    def results(self, ticker=None, advice_id=None, *, now=None):
        now = self.clock() if now is None else now
        with self.store.connection() as db:
            if advice_id is not None:
                packet = self.unpack(
                    db.execute(
                        "SELECT payload,sha256 FROM company_advice WHERE id=?",
                        (record_id(advice_id),),
                    ).fetchone()
                )
                if not packet:
                    raise ValueError("Unknown saved advisor record ID.")
                result = checked(
                    db.execute(
                        "SELECT payload,sha256 FROM advisor_evaluations WHERE advice_id=?",
                        (advice_id,),
                    ).fetchone()
                )
                if result:
                    return (
                        f"{packet['ticker']} result {advice_id} • {result['status']}\n"
                        f"{result['sessions']} • expected {result['expected']} • realized {result['realized']}\n"
                        f"Company {result['company_return']}% • SPY {result['spy_return']}% • excess {result['excess_percentage_points']} pp\n"
                        + "\n".join(
                            f"{k}: {v['value']['price']} USD; bar {v['value']['bar_time']}; received {v['value']['received_at']}; {v['value']['source_url']}; hash {v['sha256']}"
                            for k, v in result["observations"].items()
                        )
                        + "\nGaps: "
                        + ("; ".join(result["gaps"]) or "none")
                        + "\n"
                        + QUALITY
                    )
                refs = db.execute(
                    "SELECT role,edge,state,last_error FROM advisor_outcome_inputs WHERE advice_id=? ORDER BY role,edge",
                    (advice_id,),
                ).fetchall()
                return (
                    f"{packet['ticker']} result {advice_id}: pending • horizon closes {packet['closes']}\n"
                    + (
                        "\n".join(f"{r}/{e}: {s} {error}" for r, e, s, error in refs)
                        or "Collection not yet initialized."
                    )
                    + "\n"
                    + self.status(db)
                )
            rows = db.execute(
                "SELECT a.payload,a.sha256,e.payload,e.sha256 FROM company_advice a LEFT JOIN advisor_evaluations e ON e.advice_id=a.id"
                + (" WHERE a.ticker=?" if ticker else "")
                + " ORDER BY a.as_of LIMIT 256",
                (ticker,) if ticker else (),
            ).fetchall()
            counts = dict.fromkeys(
                ("pending", "scored", "abstained", "unscorable", "unsupported"), 0
            )
            eligible, dates, compared = [], [], []
            for raw, sha, result_raw, result_sha in rows:
                packet = self.unpack((raw, sha))
                result = checked((result_raw, result_sha)) if result_raw else None
                if not self.supported(packet):
                    counts["unsupported"] += 1
                    continue
                dates.extend(packet["sessions"])
                counts[result["status"] if result else "pending"] += 1
                if result and result["status"] == "scored":
                    eligible.append(result)
                    if result["spy_return"] is not None:
                        compared.append(result)
            n = len(eligible)

            def ratio(k):
                return f"{k}/{n}" if n else "n/a (0 eligible)"

            lines = [
                f"Company results{(' • ' + ticker) if ticker else ''} • {RULES} • ±1%",
                "Coverage: " + (f"{min(dates)}–{max(dates)}" if dates else "n/a"),
                f"Matched {ratio(sum(r['matched'] for r in eligible))}",
                " • ".join(f"{k} {v}" for k, v in counts.items()),
                f"Same-cohort baselines: always bullish {ratio(sum(r['realized'] == 'bullish' for r in eligible))}; always mixed {ratio(sum(r['realized'] == 'mixed' for r in eligible))}",
                f"SPY comparisons: {len(compared)} paired records; average excess "
                + (
                    f"{sum(Decimal(r['excess_percentage_points']) for r in compared) / len(compared):+.2f} pp"
                    if compared
                    else "n/a"
                ),
                "Overlapping windows are not independent samples; hit rate is not profit.",
                QUALITY,
                self.status(db),
            ]
            return "\n".join(lines)

    def status(self, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.status(connection)
        pending = db.execute(
            "SELECT COUNT(*) FROM advisor_outcome_inputs WHERE state='pending'"
        ).fetchone()[0]
        error = db.execute("SELECT value FROM state WHERE key='advisor_results_error'").fetchone()
        return (
            f"Company outcome collection: {self.store.settings(db)['advisor_results_enabled']} • {pending} pending endpoints"
            + (f" • {error[0]}" if error and error[0] else "")
        )
