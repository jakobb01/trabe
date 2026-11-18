"""Bounded pre-open company research samples kept apart from owner-issued advice."""

import time
from bisect import bisect_right
from datetime import datetime, timedelta

from trabe import __version__
from trabe.advisor import ADVISOR_SCHEMA, COMPARISON, RULES, Advisor, AdvisorPacketTooLarge
from trabe.calendar import NY, USCalendar, retention_cutoff
from trabe.ledger import Ledger
from trabe.outlook import digest, encoded
from trabe.price_endpoints import checked

FEATURE = "advisor_shadow"
SAMPLING = "company-shadow-sampling-v1"
DAILY_CAP = 5
RECORD_CAP = 384
START_BEFORE_OPEN = 600
END_BEFORE_OPEN = 60
SHADOW_SCHEMA = (
    *(sql.replace("company_advice", "shadow_company_advice") for sql in ADVISOR_SCHEMA),
    """CREATE TABLE IF NOT EXISTS advisor_shadow_sessions(
        session_date TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)""",
)


class AdvisorShadow:
    def __init__(self, store, calendar=None):
        self.store, self.calendar = store, calendar or USCalendar()
        self.advisor = Advisor(store, self.calendar)

    @staticmethod
    def prune(now, db):
        cutoff = retention_cutoff(datetime.fromtimestamp(now, NY).date()).isoformat()
        db.execute(
            "DELETE FROM shadow_company_advice WHERE session_date<=? AND target_close<?",
            (cutoff, now),
        )
        db.execute("DELETE FROM advisor_shadow_sessions WHERE session_date<=?", (cutoff,))

    def capture(self, *, now=None):
        with self.store.connection() as db:
            state = dict(
                db.execute(
                    "SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused','advisor_shadow_checked')"
                )
            )
            if self.store.settings(db)[FEATURE + "_enabled"] != "on":
                return
            now = time.time() if now is None else now
            heartbeat = str(int(now // 300) * 300)
            if state.get("advisor_shadow_checked") != heartbeat:
                db.execute(
                    "INSERT OR REPLACE INTO state VALUES ('advisor_shadow_checked',?)", (heartbeat,)
                )
            if state.get("storage_paused") or float(state.get("pause_until", "0")) > now:
                return
            session = self.calendar.at(now)
            if not session or not (
                session.opens - START_BEFORE_OPEN <= now < session.opens - END_BEFORE_OPEN
            ):
                return
            if db.execute(
                "SELECT 1 FROM advisor_shadow_sessions WHERE session_date=?", (session.day,)
            ).fetchone():
                return
            horizon = self.advisor._sessions(now)
            symbols = sorted(Ledger(self.store).coverage(db))
            prior = self._checkpoints(db, limit=1)
            last_symbol = prior[0]["selected"][-1] if prior and prior[0]["selected"] else ""
            offset = bisect_right(symbols, last_symbol)
            selected = (symbols[offset:] + symbols[:offset])[:DAILY_CAP]
            self.prune(now, db)
            remaining = (
                RECORD_CAP - db.execute("SELECT COUNT(*) FROM shadow_company_advice").fetchone()[0]
            )
            captured, omitted = [], {}
            for ticker in selected:
                if remaining <= 0:
                    omitted[ticker] = "retention limit"
                    continue
                inputs = self.advisor._inputs(db, ticker, now, session)
                packet = self.advisor.freeze(ticker, now, horizon, inputs)
                packet["sampling_version"] = SAMPLING
                try:
                    self.advisor.save(db, packet, shadow=True)
                except AdvisorPacketTooLarge:
                    omitted[ticker] = "packet limit"
                    continue
                captured.append(ticker)
                remaining -= 1
            checkpoint = {
                "version": SAMPLING,
                "app_version": __version__,
                "rule_version": RULES,
                "comparison_version": COMPARISON,
                "as_of": now,
                "session": session.day,
                "covered": len(symbols),
                "selected": selected,
                "captured": captured,
                "omitted": omitted,
            }
            db.execute(
                "INSERT INTO advisor_shadow_sessions VALUES (?,?,?)",
                (session.day, encoded(checkpoint), digest(checkpoint)),
            )
            return checkpoint

    @staticmethod
    def _checkpoints(db, *, limit=96):
        return [
            checked(row)
            for row in db.execute(
                "SELECT payload,sha256 FROM advisor_shadow_sessions ORDER BY session_date DESC LIMIT ?",
                (limit,),
            )
        ]

    def _missed(self, enrolled, now, recorded):
        day = datetime.fromtimestamp(now, NY).date()
        start = max(
            retention_cutoff(day) + timedelta(days=1), datetime.fromtimestamp(enrolled, NY).date()
        )
        missed = 0
        while start <= day:
            session = self.calendar.session(start)
            if (
                session
                and enrolled < session.opens - END_BEFORE_OPEN <= now
                and session.day not in recorded
            ):
                missed += 1
            start += timedelta(days=1)
        return missed

    def status(self, db=None, *, now=None):
        now = time.time() if now is None else now
        if db is None:
            with self.store.connection() as connection:
                return self.status(connection, now=now)
        is_on = self.store.settings(db)[FEATURE + "_enabled"] == "on"
        state = dict(
            db.execute(
                "SELECT key,value FROM state WHERE key IN ('pause_until','storage_paused',"
                "'advisor_shadow_enabled_at','advisor_shadow_checked','advisor_shadow_error')"
            )
        )
        checkpoints = self._checkpoints(db)
        recorded = {row["session"] for row in checkpoints}
        rows = [
            f"Automatic forecast sampling: {'on' if is_on else 'off'} • /advisor shadow on|off",
            f"Retained checkpoints: {len(checkpoints)} sessions • "
            f"{sum(len(row['captured']) for row in checkpoints)}/"
            f"{sum(len(row['selected']) for row in checkpoints)} selected companies captured "
            f"({sum(row['covered'] for row in checkpoints)} covered company-days).",
        ]
        if checkpoints:
            last = checkpoints[0]
            rows.append(
                f"Last checkpoint {last['session']}: {len(last['captured'])}/"
                f"{len(last['selected'])} selected • {last['covered']} covered."
                + (
                    " Omissions: " + ", ".join(sorted(set(last["omitted"].values()))) + "."
                    if last["omitted"]
                    else ""
                )
            )
        if is_on:
            try:
                session = self.calendar.next_session(now + END_BEFORE_OPEN)
                if session.day in recorded:
                    session = self.calendar.next_session(session.closes)
                paused_until = float(state.get("pause_until", "0"))
                if state.get("storage_paused"):
                    rows.append("Capture paused by the storage guard.")
                elif paused_until >= session.opens - END_BEFORE_OPEN:
                    session = self.calendar.next_session(paused_until + END_BEFORE_OPEN)
                stamp = datetime.fromtimestamp(session.opens - START_BEFORE_OPEN, NY)
                ends = datetime.fromtimestamp(session.opens - END_BEFORE_OPEN, NY)
                rows.append(f"Next unchecked window: {stamp:%Y-%m-%d %H:%M %Z} to {ends:%H:%M}.")
                if enrolled := state.get("advisor_shadow_enabled_at"):
                    missed = self._missed(float(enrolled), now, recorded)
                    rows.append(
                        f"Elapsed sessions without a checkpoint since enrollment, within retention: "
                        f"{missed}. Company coverage is unknown for those sessions."
                    )
            except ValueError:
                rows.append("Next sampling window unavailable from the saved US calendar.")
            if checked_at := state.get("advisor_shadow_checked"):
                stamp = datetime.fromtimestamp(float(checked_at), NY)
                rows.append(f"Last worker check (5-min bucket): {stamp:%Y-%m-%d %H:%M %Z}.")
            if state.get("advisor_shadow_error"):
                rows.append(state["advisor_shadow_error"])
        return "\n".join(rows)
