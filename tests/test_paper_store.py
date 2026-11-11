"""Money, reservations, transactional fills, immutable replay and experiment limits."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_paper_rules import earnings, inputs_for, insider
from test_price_endpoints import chart

from trabe.calendar import USCalendar
from trabe.ledger import Ledger
from trabe.maintenance import Maintenance
from trabe.outlook import digest
from trabe.paper import Paper
from trabe.paper_store import PaperStore
from trabe.price_endpoints import EndpointStore, checked, parse_endpoint
from trabe.store import Store


class PaperStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "db")
        self.calendar = USCalendar()
        self.session = self.calendar.session(date(2026, 11, 10))
        self.now = self.session.opens - 900
        self.repo = PaperStore(self.store)
        self.run = self.repo.start(now=self.now - 2000, calendar_version=digest(self.calendar.data))

    def capture(self, inputs=None, session=None):
        session = session or self.session
        inputs = inputs or [inputs_for(session)]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self.repo.capture_batch(inputs, session, now=session.opens - 900, db=db)
            return batch

    def rate(self, session=None, *, rate="1.10", known=None):
        session = session or self.session
        with self.store.connection() as db:
            return self.repo.observe_fx(
                db,
                session.day,
                rate,
                session.day,
                "Frankfurter/ECB",
                session.opens - 2000 if known is None else known,
            )

    def decision(self, ticker="VST", session=None):
        from trabe.price_endpoints import checked

        with self.store.connection() as db:
            return checked(
                db.execute(
                    "SELECT d.payload,d.sha256 FROM paper_decisions d JOIN paper_batches b ON b.id=d.batch_id WHERE d.ticker=? AND b.session_date=?",
                    (ticker, (session or self.session).day),
                ).fetchone()
            )

    def fill(self, decision=None, price="50", *, now=None, db=None):
        decision = decision or self.decision()
        session = self.calendar.session(date.fromisoformat(decision["session_date"]))
        now = session.opens + 300 if now is None else now
        endpoint = parse_endpoint(
            chart(decision["ticker"], session, opening=price, edges=("open",)),
            symbol=decision["ticker"],
            session=session,
            edge="open",
            received_at=session.opens + 300,
        )
        if db is None:
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.fill(decision, price, now=now, db=connection)
        saved = EndpointStore(self.store).save(endpoint, db=db)
        return self.repo.fill_order(decision["id"], saved, db=db, now=now)

    def replay(self):
        with self.store.connection() as db:
            return self.repo.replay(db, self.run["id"])

    def test_P01_P06_round_trip_cash_fee_and_realized_profit(self):
        self.assertEqual(self.replay()["cash"], Decimal("2000.00"))
        self.rate()
        self.capture()
        value = self.fill()
        self.assertEqual(value["quantity"], 4)
        self.assertEqual(value["fee_usd"], "1.10")
        self.assertEqual(self.replay()["cash"], Decimal("1798.90"))
        self.assertEqual(self.replay()["positions"]["VST"]["basis"], Decimal("201.10"))
        next_session = self.calendar.next_session(self.session.closes)
        self.rate(next_session)
        self.capture([inputs_for(next_session, bias="-0.4")], next_session)
        sale = self.fill(self.decision(session=next_session), "55")
        self.assertEqual(sale["side"], "sell")
        self.assertEqual(self.replay()["cash"], Decimal("2017.80"))
        self.assertEqual(self.replay()["realized"], Decimal("17.80"))
        self.assertEqual(self.replay()["fees"], Decimal("2.20"))
        self.assertEqual(self.replay()["positions"]["VST"]["quantity"], 0)

    def test_P02_fx_known_boundary_receipt_and_historical_fee(self):
        old = self.rate(rate="1.10")
        self.rate(rate="1.20", known=self.session.opens + 1)
        with self.store.connection() as db:
            self.assertEqual(self.repo.fx(db, self.session.opens), old)
        self.capture()
        self.assertEqual(self.fill()["fee_usd"], "1.10")
        self.rate(rate="1.50", known=self.session.opens + 500)
        self.assertEqual(self.replay()["fees"], Decimal("1.10"))

    def test_P02_future_effective_requested_stale_and_invalid_fx(self):
        with self.store.connection() as db:
            for rate in (True, "NaN", 0, -1):
                with self.assertRaises(ValueError):
                    self.repo.observe_fx(
                        db, self.session.day, rate, self.session.day, "Frankfurter/ECB", self.now
                    )
            self.repo.observe_fx(db, "2026-11-11", "1.1", "2026-11-11", "Frankfurter/ECB", self.now)
            self.repo.observe_fx(db, "2026-11-01", "1.1", "2026-11-01", "Frankfurter/ECB", self.now)
            self.assertIsNone(self.repo.fx(db, self.session.opens))
        self.rate()
        self.capture()
        with self.store.connection() as db:
            db.execute("DELETE FROM fx_observations")
        self.assertIsNone(self.fill())
        self.assertEqual(self.replay()["fees"], 0)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT last_error FROM paper_orders").fetchone()[0], "fx_unavailable"
            )

    def test_P04_same_target_is_hold_and_lower_target_reduces(self):
        self.rate()
        inputs = inputs_for(self.session)
        earnings(inputs)
        insider(inputs)
        self.capture([inputs])
        self.assertEqual(self.fill()["quantity"], 12)
        next_session = self.calendar.next_session(self.session.closes)
        self.rate(next_session)
        self.capture([inputs_for(next_session)], next_session)
        self.assertEqual(self.fill(self.decision(session=next_session))["quantity"], 8)
        self.assertEqual(self.replay()["positions"]["VST"]["quantity"], 4)
        self.assertEqual(self.replay()["positions"]["VST"]["basis"], Decimal("200.37"))
        third = self.calendar.next_session(next_session.closes)
        self.rate(third)
        self.capture([inputs_for(third)], third)
        self.assertEqual(self.decision(session=third)["action"], "hold")
        self.assertIsNone(self.fill(self.decision(session=third)))
        self.assertEqual(self.replay()["fees"], Decimal("2.20"))

    def test_P05_P20_concurrent_repeated_execution_and_failure_atomicity(self):
        self.rate()
        self.capture()
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self.fill(db=db)
                raise RuntimeError("injected before commit")
        self.assertEqual(self.replay()["cash"], 2000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.fill(), range(2)))
        self.assertEqual(values[0], values[1])
        self.assertEqual(self.replay()["fees"], Decimal("1.10"))
        restarted = PaperStore(Store(self.store.path))
        with self.store.connection() as db:
            self.assertEqual(restarted.replay(db, self.run["id"]), self.replay())

    def test_P05_ticker_session_fill_is_unique_across_experiment_ids(self):
        self.rate()
        self.capture()
        self.fill()
        with self.store.connection() as db:
            # No public close/reset command exists; exercise the DB's future-run invariant.
            db.execute("UPDATE paper_run_state SET status='closed'")
        newer = self.repo.start(now=self.now, calendar_version=digest(self.calendar.data))
        self.assertNotEqual(newer["id"], self.run["id"])
        self.capture()
        with self.store.connection() as db:
            decision = checked(
                db.execute(
                    "SELECT d.payload,d.sha256 FROM paper_decisions d JOIN paper_batches b ON b.id=d.batch_id WHERE b.run_id=?",
                    (newer["id"],),
                ).fetchone()
            )
        self.assertIsNone(self.fill(decision))
        with self.store.connection() as db:
            self.assertEqual(self.repo.replay(db, newer["id"])["cash"], Decimal("2000.00"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0], 1)

    def test_P07_P08_stable_reservations_cannot_overspend_or_use_sale_proceeds(self):
        self.rate()
        values = []
        for ticker in ("ZZZ", "AAA", "CCC", "DDD", "EEE"):
            inputs = inputs_for(self.session, ticker=ticker)
            earnings(inputs)
            insider(inputs)
            values.append(inputs)
        self.capture(list(reversed(values)))
        decisions = [self.decision(v["ticker"]) for v in values]
        self.assertEqual(
            [d["ticker"] for d in sorted(decisions, key=lambda d: -Decimal(d["reservation_usd"]))][
                :3
            ],
            ["AAA", "CCC", "DDD"],
        )
        for decision in reversed(decisions):
            self.fill(decision)
        state = self.replay()
        self.assertGreaterEqual(state["cash"], 0)
        self.assertEqual(state["reserved"], 0)
        self.assertEqual(
            state["positions"]["ZZZ"]["quantity"] if "ZZZ" in state["positions"] else 0, 0
        )

    def test_P09_opening_gap_shrinks_or_cancels_without_fee_or_direction_flip(self):
        self.rate()
        self.capture()
        self.assertEqual(self.fill(price="60")["quantity"], 3)
        second = self.calendar.next_session(self.session.closes)
        self.rate(second)
        self.capture([inputs_for(second, ticker="GOOGL")], second)
        self.assertIsNone(self.fill(self.decision("GOOGL", second), "250"))
        self.assertEqual(self.replay()["fees"], Decimal("1.10"))
        self.assertEqual(self.replay()["reserved"], 0)

    def exhaust_cash(self):
        self.rate()
        values = [inputs_for(self.session, ticker=ticker) for ticker in ("AAA", "BBB", "CCC")]
        for value in values:
            earnings(value)
            insider(value)
        values.append(inputs_for(self.session, ticker="ZZZ", price="65.2001"))
        self.capture(values)
        # Funding is frozen, so filling the last reservation first cannot change allocations.
        last = self.fill(self.decision("ZZZ"), "65.2001")
        self.assertEqual(last["quantity"], 3)  # rounded gross 195.60 fits the 196.70 reservation
        for ticker in ("AAA", "BBB", "CCC"):
            self.fill(self.decision(ticker))
        self.assertEqual(self.replay()["cash"], Decimal("0.00"))

    def test_P09_sell_direction_cannot_flip_after_opening_gap(self):
        self.rate()
        self.capture()
        self.fill()
        second = self.calendar.next_session(self.session.closes)
        self.rate(second)
        self.capture([inputs_for(second, price="100")], second)
        decision = self.decision(session=second)
        self.assertEqual(decision["action"], "sell")
        self.assertIsNone(self.fill(decision, "25"))
        self.assertEqual(self.replay()["positions"]["VST"]["quantity"], 4)
        self.assertEqual(self.replay()["fees"], Decimal("1.10"))

    def test_P07_P08_cent_accurate_quantity_and_no_same_open_sale_funding(self):
        self.exhaust_cash()
        next_session = self.calendar.next_session(self.session.closes)
        self.rate(next_session)
        self.capture(
            [
                inputs_for(next_session, ticker="AAA", bias="-0.4"),
                inputs_for(next_session, ticker="NEW"),
            ],
            next_session,
        )
        self.assertIn("insufficient_cash", self.decision("NEW", next_session)["reasons"])
        self.fill(self.decision("AAA", next_session))
        self.assertIsNone(self.fill(self.decision("NEW", next_session)))
        self.assertGreater(self.replay()["cash"], 0)

    def test_P07_sale_fee_cannot_make_empty_cash_negative(self):
        self.exhaust_cash()
        next_session = self.calendar.next_session(self.session.closes)
        self.rate(next_session)
        self.capture([inputs_for(next_session, ticker="AAA", bias="-0.4")], next_session)
        before = self.replay()
        self.assertIsNone(self.fill(self.decision("AAA", next_session), "0.01"))
        self.assertEqual(self.replay()["cash"], before["cash"])
        self.assertEqual(self.replay()["positions"], before["positions"])
        self.assertEqual(self.replay()["fees"], before["fees"])

    def test_P19_pause_cancels_reservations_and_resume_cannot_revive(self):
        self.rate()
        self.capture()
        self.assertGreater(self.replay()["reserved"], 0)
        self.store.pause_automation(self.session.opens + 600)
        self.assertEqual(self.replay()["reserved"], 0)
        self.store.pause_automation(0)
        self.assertIsNone(self.fill())
        self.assertEqual(self.replay()["cash"], 2000)
        self.assertIsNone(self.capture())

    def test_P21_immutable_run_batch_decision_fill_and_fx_and_hash_failure(self):
        self.rate()
        self.capture()
        self.fill()
        endpoint = parse_endpoint(
            chart("VST", self.session),
            symbol="VST",
            session=self.session,
            edge="close",
            received_at=self.session.closes + 1800,
        )
        endpoint["actions"] = [{"kind": "split", "at": self.session.opens}]
        with self.store.connection() as db:
            EndpointStore(self.store).save(endpoint, db=db)
            self.repo.action_conflict(db, self.run["id"], "VST", self.session.closes + 1800)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE paper_action_conflicts SET ticker=ticker")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT OR REPLACE INTO paper_action_conflicts SELECT * FROM paper_action_conflicts"
                )
            for table in (
                "paper_runs",
                "paper_batches",
                "paper_decisions",
                "paper_fills",
                "fx_observations",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f"UPDATE {table} SET id=id")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
            db.execute("DROP TRIGGER immutable_paper_fills")
            db.execute("UPDATE paper_fills SET sha256='corrupt'")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.replay()

    def test_P22_P27_migration_real_ledger_advice_retention_and_caps(self):
        Ledger(self.store).record("VST", "buy", "2", "100", "USD", "2026-11-03")
        self.rate()
        self.capture()
        self.fill()
        before = self.replay()
        with self.store.connection() as db:
            db.execute("PRAGMA user_version=9")
        Store(self.store.path)
        Maintenance(self.store).expire(self.session.closes + 130 * 86400)
        self.assertEqual(self.replay(), before)
        self.assertEqual(Ledger(self.store).positions("USD")["VST"][0], 2)
        second = self.calendar.next_session(self.session.closes)
        with patch("trabe.paper_store.MAX_DECISIONS", 1):
            self.assertIsNone(self.capture([inputs_for(second)], second))
        with self.store.connection() as db:
            self.assertEqual(self.repo.current(db)["status"], "paused")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_batches").fetchone()[0], 1)
        self.assertEqual(self.replay(), before)

    def test_P28_start_pause_resume_is_one_experiment_no_fifth_day_exit(self):
        self.rate()
        self.capture()
        self.fill()
        paper = Paper(self.store, clock=lambda: self.now)
        paper.handle(["off"])
        second = self.repo.start(now=self.now + 1, calendar_version=digest(self.calendar.data))
        self.assertEqual(second["id"], self.run["id"])
        self.assertEqual(second["status"], "paused")
        paper.handle(["on"])
        self.assertEqual(self.replay()["cash"], Decimal("1798.90"))
        self.assertEqual(self.replay()["positions"]["VST"]["quantity"], 4)

    def test_P27_aggregate_json_cap_pauses_before_partial_batch(self):
        self.rate()
        with patch("trabe.paper_store.MAX_RUN_BYTES", 1):
            self.assertIsNone(self.capture())
        with self.store.connection() as db:
            self.assertEqual(self.repo.current(db)["status"], "paused")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_decisions").fetchone()[0], 0)
        self.assertEqual(self.replay()["cash"], 2000)
