"""Daily prospective paper snapshots, exact opening fills and cached owner views."""

import copy
import json
import re
import time
from datetime import date, datetime

from trabe.advisor import Advisor
from trabe.api import ApiError
from trabe.calendar import NY, USCalendar
from trabe.market_context import SOURCE, baseline, number
from trabe.outlook import OutlookStore, digest, encoded
from trabe.paper_store import PaperStore, money
from trabe.price_endpoints import QUALITY, EndpointStore, checked, enabled


class Paper:
    def __init__(self, store, calendar=None, *, clock=time.time):
        self.store, self.calendar, self.clock = store, calendar or USCalendar(), clock
        self.repo, self.endpoints = PaperStore(store), EndpointStore(store, self.calendar)

    def coverage(self, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.coverage(connection)
        watched = dict(self.store.watchlist(db))
        run = self.repo.current(db)
        positions = self.repo.replay(db, run["id"])["positions"] if run else {}
        held = {t for t, p in positions.items() if p["quantity"]}
        return sorted(set(watched) | held, key=lambda t: (t not in held, watched.get(t, 4), t))

    def _expire(self, db, now):
        db.execute(
            "UPDATE paper_orders SET status='expired',reserved_usd='0.00',last_error='opening_endpoint_expired' WHERE status='pending' AND expires_at<=?",
            (now,),
        )

    def reference(self, db, ticker, session, now):
        previous = self.calendar.previous(date.fromisoformat(session.day))
        exact = self.endpoints.get(ticker, previous.day, "close", db=db)
        if exact and exact["received_at"] <= now:
            return exact
        row = db.execute(
            "SELECT payload,last_seen,available_at FROM signals WHERE kind='market-context' AND event_key=? AND source=? AND last_seen<=? AND available_at<=?",
            (ticker, SOURCE, now, now),
        ).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        if (
            value.get("symbol") != ticker
            or value.get("provider_symbol") != ticker.replace(".", "-")
            or value.get("instrument_type") not in {"EQUITY", "ETF"}
            or value.get("currency") != "USD"
            or value.get("timezone") != "America/New_York"
            or value.get("received_at", now + 1) > now
            or value.get("regular_time", now + 1) > now
        ):
            return None
        price, day = baseline(value, session, self.calendar)
        if price is None:
            return None
        return {
            "symbol": ticker,
            "provider_symbol": value["provider_symbol"],
            "session_date": day,
            "edge": "close",
            "price": str(price),
            "currency": "USD",
            "instrument_type": value["instrument_type"],
            "timezone": value["timezone"],
            "method": "validated-previous-close-v1",
            "source": SOURCE,
            "source_url": value.get("source_url"),
            "received_at": max(value["received_at"], row[1], row[2]),
            "bar_time": value["regular_time"] if value["regular_day"] == previous.day else None,
            "provenance": "validated previous-session baseline; not an exact execution bar",
            "actions": value.get("actions", []),
            "action_coverage": "unverified",
        }

    def inputs(self, db, ticker, session, now, run):
        settings = self.store.settings(db) | {
            "insider_min_value_usd": run["config"]["insider_min_value_usd"]
        }
        inputs = copy.deepcopy(
            Advisor(self.store, self.calendar).evidence_inputs(
                db, ticker, now, session, settings=settings
            )
        )
        for source in inputs["fundamental_sources"]:
            if source.get("succeeded_at") is not None and source["succeeded_at"] > now:
                source["succeeded_at"] = None
        configured = {
            "yahoo-rss" if p == "yahoo" else p for p in settings["news_sources"].split(",")
        }
        inputs["news_sources"] = [s for s in inputs["news_sources"] if s["provider"] in configured]
        insiders = []
        for row in inputs["insiders"]:
            raw = db.execute(
                "SELECT payload FROM signals WHERE kind='insiders' AND event_key=?", (row["key"],)
            ).fetchone()
            facts = json.loads(raw[0]) if raw else {}
            if any(facts.get(k) for k in ("withdrawn", "conflict", "superseded_by")):
                continue
            row["facts"].update({k: facts[k] for k in ("form_type", "economic_key") if k in facts})
            insiders.append(row)
        inputs["insiders"] = insiders
        # Limit copied text, preserving the consumed structured analysis and source provenance.
        for item in inputs["news"]:
            item["snippet"] = item.get("snippet", "")[:480]
            item["headline"] = item.get("headline", "")[:240]
        outlook = OutlookStore(self.store).get(session_date=session.day, db=db)
        if inputs["market_outlook"]:
            inputs["market_outlook"]["gaps"] = outlook.get("gaps", [])
        reference = self.reference(db, ticker, session, now)
        conflict = self.repo.action_conflict(db, run["id"], ticker, now)
        known_actions = conflict["actions"] if conflict else []
        inputs.update(
            ticker=ticker,
            as_of=now,
            session_date=session.day,
            previous_session=self.calendar.previous(date.fromisoformat(session.day)).day,
            next_session=self.calendar.next_session(session.closes).day,
            reference=reference,
            watchlist_member=ticker in dict(self.store.watchlist(db)),
            watchlist_tier=dict(self.store.watchlist(db)).get(ticker),
            insider_min_value_usd=run["config"]["insider_min_value_usd"],
            corporate_actions=known_actions,
        )
        return inputs

    def capture(self, session, *, now):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self.repo.current(db)
            if not run or not enabled(self.store, db, "paper", now):
                return None
            self._expire(db, now)
            inputs = [self.inputs(db, ticker, session, now, run) for ticker in self.coverage(db)]
            value = self.repo.capture_batch(
                inputs, session, now=now, db=db, committed_at=self.clock()
            )
            if self.clock() >= session.opens - 900 + 60:
                raise ValueError("Paper capture completed too late; transaction rolled back.")
            return value

    def schedule(self, insights, now=None):
        now = self.clock() if now is None else now
        with self.store.connection() as db:
            if not enabled(self.store, db, "paper", now):
                return
            run = self.repo.current(db)
        if not run or run["status"] != "active":
            return
        try:
            session = self.calendar.at(now)
            if not session:
                return
            if run["config"]["calendar_version"] != digest(self.calendar.data):
                raise ValueError("Paper calendar version changed.")
        except ValueError:
            self.store.set_state("paper_error", "Paper suspended: calendar unavailable/changed.")
            return
        scheduled = session.opens - 900
        if now < session.opens - 3600:
            return
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not enabled(self.store, db, "paper", now):
                return
            self._expire(db, now)
            db.execute(
                "INSERT OR IGNORE INTO paper_session_state VALUES (?,?,'preparing','')",
                (run["id"], session.day),
            )
            state = db.execute(
                "SELECT state FROM paper_session_state WHERE run_id=? AND session_date=?",
                (run["id"], session.day),
            ).fetchone()[0]
            if state != "preparing":
                return
            if now >= scheduled + 60:
                db.execute(
                    "UPDATE paper_session_state SET state='missed',reason='Decision window missed; no backfill' WHERE run_id=? AND session_date=?",
                    (run["id"], session.day),
                )
                return
        if now < scheduled:
            insights.queue(
                "paper_prepare",
                self.coverage(),
                origin="automatic",
                gate="paper",
                deadline=scheduled,
                session=session.day,
            )
        else:
            self.capture(session, now=now)

    def _demands(self, db, now):
        run = self.repo.current(db)
        if not run:
            return
        session = self.calendar.at(now)
        day = datetime.fromtimestamp(now, NY).date()
        mark_session = (
            session if session and now >= session.closes + 1800 else self.calendar.previous(day)
        )
        for ticker, position in self.repo.replay(db, run["id"])["positions"].items():
            if position["quantity"]:
                self.repo.action_conflict(db, run["id"], ticker, now)
                db.execute(
                    "INSERT OR IGNORE INTO paper_price_inputs VALUES (?,?,'close',?,?,NULL)",
                    (
                        ticker,
                        mark_session.day,
                        mark_session.closes + 1800,
                        mark_session.closes + 7 * 86400,
                    ),
                )

    def run_once(self, http, *, now):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not enabled(self.store, db, "paper", now):
                if not self.store.state("storage_paused"):
                    self.repo.cancel_pending(db, "disabled_or_paused")
                return False
            self._expire(db, now)
            self._demands(db, now)
            due = db.execute(
                "SELECT symbol,session_date,GROUP_CONCAT(edge) FROM paper_price_inputs WHERE endpoint_id IS NULL AND ready_at<=? AND expires_at>? AND (edge='close' OR EXISTS(SELECT 1 FROM paper_orders o JOIN paper_decisions d ON d.id=o.decision_id JOIN paper_batches b ON b.id=d.batch_id WHERE o.status='pending' AND d.ticker=symbol AND b.session_date=paper_price_inputs.session_date)) GROUP BY symbol,session_date ORDER BY MAX(edge='open') DESC,MIN(expires_at),symbol",
                (now, now),
            ).fetchall()
        self._attach_and_fill(now)
        attempts = 0
        for ticker, day, edges in due:
            if attempts >= 2:
                break
            needed = [e for e in edges.split(",") if self.endpoints.get(ticker, day, e) is None]
            if not needed:
                continue
            session = self.calendar.session(date.fromisoformat(day))
            if not session:
                continue
            try:
                attempts += bool(
                    self.endpoints.collect(
                        http, ticker, session, needed, feature="paper", now=now, clock=self.clock
                    )
                )
            except ApiError:
                attempts += 1
            self._attach_and_fill(max(now, self.clock()))
        return bool(attempts)

    def _attach_and_fill(self, now):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not enabled(self.store, db, "paper", now):
                return
            for symbol, day, edge in db.execute(
                "SELECT symbol,session_date,edge FROM paper_price_inputs WHERE endpoint_id IS NULL"
            ).fetchall():
                value = self.endpoints.get(symbol, day, edge, db=db)
                if value and value["received_at"] <= now:
                    endpoint = db.execute(
                        "SELECT id FROM market_price_endpoints WHERE source=? AND symbol=? AND session_date=? AND edge=?",
                        (SOURCE, symbol, day, edge),
                    ).fetchone()[0]
                    db.execute(
                        "UPDATE paper_price_inputs SET endpoint_id=? WHERE symbol=? AND session_date=? AND edge=?",
                        (endpoint, symbol, day, edge),
                    )
            orders = db.execute(
                "SELECT o.decision_id,d.ticker,b.session_date,b.opens FROM paper_orders o JOIN paper_decisions d ON d.id=o.decision_id JOIN paper_batches b ON b.id=d.batch_id WHERE o.status='pending' ORDER BY d.ticker"
            ).fetchall()
            for decision_id, ticker, day, opens in orders:
                if now >= opens + 300 and self.repo.fx(db, opens) is None:
                    self.repo.finish_order(db, decision_id, "fx_unavailable")
                    continue
                endpoint = self.endpoints.get(ticker, day, "open", db=db)
                if endpoint:
                    self.repo.fill_order(decision_id, endpoint, db=db, now=now)

    def prune(self, now, db):
        self._expire(db, now)
        covered = self.coverage(db)
        for symbol, day, edge, expires, endpoint_id in db.execute(
            "SELECT symbol,session_date,edge,expires_at,endpoint_id FROM paper_price_inputs"
        ).fetchall():
            pending = db.execute(
                "SELECT 1 FROM paper_orders o JOIN paper_decisions d ON d.id=o.decision_id JOIN paper_batches b ON b.id=d.batch_id WHERE o.status='pending' AND d.ticker=? AND b.session_date=?",
                (symbol, day),
            ).fetchone()
            newer = db.execute(
                "SELECT 1 FROM paper_price_inputs WHERE symbol=? AND edge='close' AND session_date>? AND endpoint_id IS NOT NULL",
                (symbol, day),
            ).fetchone()
            if not pending and (
                edge == "open"
                or (expires <= now and endpoint_id is None)
                or symbol not in covered
                or (edge == "close" and newer)
            ):
                db.execute(
                    "DELETE FROM paper_price_inputs WHERE symbol=? AND session_date=? AND edge=?",
                    (symbol, day, edge),
                )
        if not db.execute("SELECT 1 FROM paper_orders WHERE status='pending'").fetchone():
            db.execute("DELETE FROM fx_observations WHERE known_at<?", (now - 7 * 86400,))

    def handle(self, args):
        now = self.clock()
        option = args[0] if args else "status"
        if option == "start" and len(args) == 1:
            run = self.repo.start(now=now, calendar_version=digest(self.calendar.data))
            return self.status(now) + (
                "\nUse /paper on to resume this existing experiment."
                if run["status"] == "paused"
                else ""
            )
        if option in {"on", "off"} and len(args) == 1:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                run = self.repo.current(db)
                if not run:
                    raise ValueError("Use /paper start to create the USD 2,000 experiment first.")
                if option == "on":
                    db.execute(
                        "UPDATE paper_run_state SET status='active' WHERE run_id=?", (run["id"],)
                    )
                    db.execute("UPDATE settings SET value='on' WHERE key='paper_enabled'")
            if option == "off":
                self.store.set_setting("paper_enabled", "off")
            return self.status(now)
        if option in {"status", "results"} and len(args) <= 1:
            return self.status(now, results=option == "results")
        if option == "decisions" and len(args) <= 3:
            day = None
            page = 1
            if len(args) >= 2:
                try:
                    day = date.fromisoformat(args[1]).isoformat()
                    if day != args[1]:
                        raise ValueError("Noncanonical date.")
                except ValueError:
                    raise ValueError("Usage: /paper decisions [YYYY-MM-DD] [PAGE]") from None
            if len(args) == 3:
                if not re.fullmatch(r"[1-9][0-9]{0,2}", args[2]):
                    raise ValueError("Page must be a positive integer.")
                page = int(args[2])
            return self.decisions(day, page)
        if option == "decision" and len(args) == 2:
            if not re.fullmatch(r"[0-9a-f]{16}", args[1]):
                raise ValueError("Use an exact saved paper decision ID.")
            with self.store.connection() as db:
                value = checked(
                    db.execute(
                        "SELECT payload,sha256 FROM paper_decisions WHERE id=?", (args[1],)
                    ).fetchone()
                )
                if not value:
                    raise ValueError("Unknown paper decision ID.")
                fill = checked(
                    db.execute(
                        "SELECT payload,sha256 FROM paper_fills WHERE decision_id=?", (args[1],)
                    ).fetchone()
                )
                order = db.execute(
                    "SELECT status,last_error FROM paper_orders WHERE decision_id=?", (args[1],)
                ).fetchone()
            return (
                "Paper decision and consumed evidence\n"
                + encoded(value)
                + "\nExecution: "
                + (encoded(fill) if fill else str(order or "hold; no fee"))
            )
        if option == "trades" and len(args) <= 2:
            from trabe.bot import symbol

            ticker = symbol(args[1]) if len(args) == 2 else None
            with self.store.connection() as db:
                rows = db.execute(
                    "SELECT payload,sha256 FROM paper_fills"
                    + (" WHERE ticker=?" if ticker else "")
                    + " ORDER BY executed_at DESC,id DESC LIMIT 12",
                    (ticker,) if ticker else (),
                ).fetchall()
                values = [checked(r) for r in rows]
            return "Paper fills (latest 12)\n" + (
                "\n".join(
                    f"{v['session_date']} {v['ticker']} {v['side']} {v['quantity']} × ${v['price_usd']} • fee EUR{v['fee_eur']} / ${v['fee_usd']} • decision {v['decision_id']}"
                    for v in values
                )
                or "No simulated trades."
            )
        raise ValueError(
            "Usage: /paper [start|on|off|status|results|decisions [YYYY-MM-DD] [PAGE]|decision ID|trades [TICKER]]"
        )

    def decisions(self, day=None, page=1):
        with self.store.connection() as db:
            if day is None:
                row = db.execute(
                    "SELECT session_date FROM paper_batches ORDER BY opens DESC LIMIT 1"
                ).fetchone()
                day = row[0] if row else None
            rows = db.execute(
                "SELECT d.payload,d.sha256,o.status,o.last_error FROM paper_decisions d JOIN paper_batches b ON b.id=d.batch_id LEFT JOIN paper_orders o ON o.decision_id=d.id WHERE b.session_date=? ORDER BY d.ticker LIMIT 9 OFFSET ?",
                (day, (page - 1) * 8),
            ).fetchall()
            values = [(checked(row[:2]), row[2], row[3]) for row in rows]
        lines = [f"Paper decisions • {day or 'none yet'} • page {page}"]
        for v, state, error in values[:8]:
            lines.append(
                f"{v['ticker']} {v['action']} • {v['tier'] or 'no tier'} ${v['target_usd'] or 'unchanged'} • S={v['score']} • {state or 'hold'} {error or ''}\n{v['id']} • "
                + ", ".join(v["reasons"] + v["gaps"])
            )
        if len(values) > 8:
            lines.append(f"Next: /paper decisions {day} {page + 1}")
        if not values:
            lines.append("No saved decisions in this view.")
        return "\n".join(lines)

    def status(self, now, results=False):
        with self.store.connection() as db:
            run = self.repo.current(db)
            if not run:
                return "Paper trading off. /paper start creates one USD 2,000 simulated experiment. EUR1 per executed buy/sell; whole shares, no slippage."
            portfolio = self.repo.replay(db, run["id"])
            lines = [
                f"Paper experiment {run['id']} • {run['status']} • processing {self.store.settings(db)['paper_enabled']}",
                f"Cash ${portfolio['cash']:.2f} • reserved ${portfolio['reserved']:.2f}",
                f"Invested basis ${sum(p['basis'] for p in portfolio['positions'].values()):.2f} • realized net P&L ${portfolio['realized']:+.2f} • fees ${portfolio['fees']:.2f}",
            ]
            marks = []
            held = {t: p for t, p in portfolio["positions"].items() if p["quantity"]}
            for ticker, position in held.items():
                row = db.execute(
                    "SELECT p.payload,p.sha256 FROM paper_price_inputs i JOIN market_price_endpoints p ON p.id=i.endpoint_id WHERE i.symbol=? AND i.edge='close' AND p.received_at<=? ORDER BY i.session_date DESC LIMIT 1",
                    (ticker, now),
                ).fetchone()
                mark = checked(row)
                conflict = bool(
                    checked(
                        db.execute(
                            "SELECT payload,sha256 FROM paper_action_conflicts WHERE run_id=? AND ticker=?",
                            (run["id"], ticker),
                        ).fetchone()
                    )
                ) or any(
                    checked(r).get("actions")
                    for r in db.execute(
                        "SELECT payload,sha256 FROM market_price_endpoints WHERE symbol=? AND received_at<=?",
                        (ticker, now),
                    )
                )
                if mark and not conflict and not mark.get("actions"):
                    amount = money(position["quantity"] * number(mark["price"]))
                    marks.append((mark["session_date"], amount))
                    text = f"indicative ${amount:.2f} at {mark['session_date']} close"
                else:
                    text = "valuation unavailable" + (
                        "; corporate-action conflict" if conflict else ""
                    )
                lines.append(
                    f"{ticker}: {position['quantity']} shares • basis ${position['basis']:.2f} • {text}"
                )
            if len(marks) == len(held) and len({d for d, _ in marks}) <= 1:
                equity = portfolio["cash"] + sum((v for _, v in marks), money(0))
                lines.append(
                    f"Indicative equity ${equity:.2f} • net return {(equity / money(run['config']['initial_cash_usd']) - 1) * 100:+.2f}%"
                    + (f" • common close {marks[0][0]}" if marks else " • cash only")
                )
            else:
                lines.append("Equity unavailable; holdings lack a common session mark.")
            last = db.execute(
                "SELECT session_date,state,reason FROM paper_session_state WHERE run_id=? ORDER BY session_date DESC LIMIT 1",
                (run["id"],),
            ).fetchone()
            lines.append("Last decision session: " + (" • ".join(last) if last else "none"))
            recent = db.execute(
                "SELECT d.payload,d.sha256 FROM paper_decisions d JOIN paper_batches b ON b.id=d.batch_id WHERE b.run_id=? AND b.session_date=(SELECT MAX(session_date) FROM paper_batches WHERE run_id=?) ORDER BY d.ticker",
                (run["id"], run["id"]),
            ).fetchall()
            gaps = sorted({g for row in recent for g in checked(row).get("gaps", [])})
            lines.append("Source gaps: " + (", ".join(gaps) or "none saved yet"))
        try:
            session = self.calendar.next_session(now + 900 - 60)
            if last and last[0] == session.day and last[1] != "preparing":
                session = self.calendar.next_session(session.closes)
            lines.append(
                f"Next decision: {datetime.fromtimestamp(session.opens - 900, NY).isoformat()} • 15m before US open"
            )
        except ValueError:
            lines.append("Next decision unavailable: calendar expired.")
        if self.store.state("storage_paused"):
            lines.append("Storage paused; unfilled instructions retained until safe expiry.")
        if self.store.automation_paused(now):
            lines.append("Global automation pause; existing holdings preserved.")
        if self.store.state("paper_error"):
            lines.append(self.store.state("paper_error"))
        lines.extend(
            [QUALITY, "Unvalidated simulation. /paper decisions • /paper trades • /paper results"]
        )
        return "\n".join(lines)
