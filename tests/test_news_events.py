"""Presentation attribution, immutable identity and legacy-input isolation gates."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trabe.news_digest import NewsDigest, card, units
from trabe.news_event_store import EventStore
from trabe.news_events import analyze
from trabe.store import SCHEMA_VERSION, Store


class EventTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "events.db")
        self.repo = EventStore(self.store)
        self.now = 1794657600.0

    def item(self, headline="NVIDIA announces CEO transition", **overrides):
        return {
            "provider": "fixture",
            "item_id": "one",
            "url": "https://example.com/one",
            "headline": headline,
            "snippet": "",
            "publisher": "Fixture",
            "published_at": self.now - 60,
            "input_quality": "headline_only",
            **overrides,
        }

    def test_fixed_corpus_and_heldout_source_supported_render(self):
        corpus = json.loads((Path(__file__).parent / "fixtures/news_events_v1.json").read_text())
        heldout = [c for c in corpus["cases"] if c["split"] == "heldout"]
        self.assertGreaterEqual(len(corpus["cases"]), 60)
        self.assertGreaterEqual(len(heldout), 20)
        for case in corpus["cases"]:
            with self.subTest(case=case["id"]):
                item = self.item(case["headline"], snippet=case["snippet"])
                result = analyze(item, case["ticker"])
                self.assertEqual(result["materiality"], case["expected_material"])
                self.assertEqual(result["direction"], case["expected_direction"])
                self.assertEqual(result["direct"], case["expected_direct"])
                for fact in result["facts"]:
                    self.assertIn(fact["text"], [item["headline"], item["snippet"]])

    def test_cross_provider_copy_reopen_and_changed_facts(self):
        original = self.item()
        self.repo.ingest([original], "NVDA", self.now)
        first = self.repo.events(["NVDA"], self.now)[0]
        copy = original | {
            "provider": "second",
            "item_id": "two",
            "url": "https://other.example/two",
            "published_at": self.now,
        }
        self.repo.ingest([copy], "NVDA", self.now + 1)
        reopened = EventStore(Store(self.store.path))
        second = reopened.events(["NVDA"], self.now + 2)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["revision"], first["revision"])
        self.assertEqual(second[0]["known_at"], first["known_at"])
        correction = original | {
            "headline": "Correction: NVIDIA cuts full-year guidance",
            "correction_of": "one",
        }
        reopened.ingest([correction], "NVDA", self.now + 3)
        corrected = reopened.events(["NVDA"], self.now + 4)[0]
        self.assertEqual(corrected["id"], first["id"])
        self.assertNotEqual(corrected["revision"], first["revision"])
        self.assertTrue(corrected["correction"])
        with self.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE event_revisions SET payload='{}'")

    def test_material_leadership_outranks_preview_and_reads_write_nothing(self):
        items = [
            self.item(
                "NVIDIA earnings preview: what to expect",
                item_id="preview",
                url="https://example.com/preview",
            ),
            self.item(),
        ]
        self.repo.ingest(items, "NVDA", self.now)
        before = self.store.path.stat().st_size
        result = self.repo.events(["NVDA"], self.now)
        self.assertEqual(result[0]["type"], "leadership")
        self.assertEqual(result[0]["direction"], "unknown")
        report = NewsDigest(self.store).report(["NVDA"], self.now)
        self.assertNotIn("https://", report)
        self.assertIn("Fixture", report)
        self.assertNotIn("Coverage is partial", report)
        self.assertEqual(before, self.store.path.stat().st_size)
        self.assertLessEqual(units(report), 3500)

    def test_disabled_generation_rejects_commit_and_long_sentence_is_not_cut(self):
        self.store.set_state("news_event_generation", "2")
        self.assertFalse(self.repo.ingest([self.item()], "NVDA", self.now, expected_generation=1))
        item = self.item(snippet="NVIDIA says " + "qualified " * 200 + "claim.")
        self.repo.ingest([item], "NVDA", self.now, expected_generation=2)
        event = self.repo.events(["NVDA"], self.now)[0]
        rendered = card(event)
        self.assertIn(item["headline"], rendered)
        self.assertNotIn("qualified", rendered)

    def test_relationships_are_one_hop_unknown_is_not_neutral(self):
        result = analyze(self.item("ROCm adds a compiler"), "AMD")
        self.assertEqual(result["issuer"], "AMD")
        self.assertFalse(result["direct"])
        self.assertFalse(result["push_eligible"])
        self.assertIsNone(analyze(self.item("AMD raises guidance"), "NVDA")["issuer"])
        self.assertEqual(
            analyze(self.item("They are leaving next week"), "NVDA")["direction"], "unknown"
        )

    def test_shared_database_access_serializes_workers_and_skips_idle_state_writes(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        other = Store(self.store.path)
        self.assertIs(self.store._connection_lock, other._connection_lock)
        connect = sqlite3.connect
        started = threading.Event()

        def quick(*args, **kwargs):
            return connect(*args, **(kwargs | {"timeout": 0.005}))

        def writer():
            started.set()
            other.set_state("worker-fixture", "ready")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch("trabe.store.sqlite3.connect", side_effect=quick):
                with self.store.connection() as holder:
                    holder.execute("BEGIN IMMEDIATE")
                    future = pool.submit(writer)
                    self.assertTrue(started.wait(1))
                    threading.Event().wait(0.03)
                    self.assertFalse(future.done())
                future.result(timeout=1)
        self.assertEqual(self.store.state("worker-fixture"), "ready")
        with self.store.connection() as reader:
            before = reader.execute("PRAGMA data_version").fetchone()[0]
            other.set_state("worker-fixture", "ready")
            self.assertEqual(reader.execute("PRAGMA data_version").fetchone()[0], before)
            other.set_state("worker-fixture", "changed")
            self.assertGreater(reader.execute("PRAGMA data_version").fetchone()[0], before)

    def test_database_lock_diagnostics_are_bounded_and_do_not_hide_failure(self):
        from unittest.mock import patch

        connect = sqlite3.connect

        def quick(*args, **kwargs):
            return connect(*args, **(kwargs | {"timeout": 0.01}))

        with self.store.connection() as holder:
            holder.execute("BEGIN IMMEDIATE")
            with (
                patch("trabe.store.sqlite3.connect", side_effect=quick),
                patch("trabe.store.faulthandler.dump_traceback") as dump,
                patch("trabe.store._last_busy_trace", 0),
                self.assertLogs("trabe.store", level="ERROR"),
            ):
                for _ in range(2):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.store.set_state("locked-fixture", "value")
                dump.assert_called_once()

    def test_partial_page_replays_after_short_write_chunks_without_advancing_cursor(self):
        from unittest.mock import patch

        items = [
            self.item(
                f"NVIDIA reports quarterly results {i}",
                item_id=str(i),
                url=f"https://example.com/{i}",
            )
            for i in range(25)
        ]
        self.repo.coverage("scan", "NVDA", self.now, cursor="initial", watermark=0)
        original = self.repo._observe
        count = 0

        def interrupted(*args):
            nonlocal count
            count += 1
            if count == 11:
                raise RuntimeError("crash after committed chunk")
            return original(*args)

        with patch.object(self.repo, "_observe", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.repo.ingest(
                    items,
                    "NVDA",
                    self.now,
                    source="scan",
                    collection={"cursor": "next", "watermark": self.now},
                )
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0], 10)
            self.assertEqual(
                json.loads(
                    db.execute(
                        "SELECT payload FROM event_collection WHERE source='scan'"
                    ).fetchone()[0]
                )["cursor"],
                "initial",
            )
        self.repo.ingest(
            items,
            "NVDA",
            self.now + 1,
            source="scan",
            collection={"cursor": "next", "watermark": self.now},
        )
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0], 25)
            self.assertEqual(
                json.loads(
                    db.execute(
                        "SELECT payload FROM event_collection WHERE source='scan'"
                    ).fetchone()[0]
                )["cursor"],
                "next",
            )
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT b.event_id FROM event_bindings b JOIN event_evidence e ON e.id=b.evidence WHERE e.url=? OR (e.provider=? AND e.item_id IN (?,?)) ORDER BY e.received DESC LIMIT 1",
                ("https://example.com/1", "fixture", "1", "2"),
            ).fetchall()
            self.assertTrue(any("event_binding_evidence" in str(r) for r in plan))

    def test_ticker_cap_does_not_evict_other_company_evidence(self):
        self.repo.ingest(
            [
                self.item(
                    "Vistra announces CEO transition",
                    item_id="held",
                    url="https://example.com/held",
                )
            ],
            "VST",
            self.now,
        )
        for start in (0, 60):
            self.repo.ingest(
                [
                    self.item(
                        f"NVIDIA reports quarterly results {i}",
                        item_id=str(i),
                        url=f"https://example.com/{i}",
                    )
                    for i in range(start, start + 60)
                ],
                "NVDA",
                self.now + start,
            )
        self.assertEqual(len(self.repo.events(["VST"], self.now + 61)), 1)
        with self.store.connection() as db:
            self.assertLessEqual(
                db.execute("SELECT COUNT(*) FROM event_targets WHERE ticker='NVDA'").fetchone()[0],
                100,
            )

    def test_migration_preserves_legacy_money_and_frozen_inputs(self):
        from datetime import date

        from test_advisor import AdvisorTests

        from trabe.advisor import Advisor
        from trabe.advisor_results import AdvisorResults
        from trabe.calendar import USCalendar
        from trabe.ledger import Ledger
        from trabe.paper import Paper
        from trabe.snapshots import SnapshotService

        fixture = AdvisorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = fixture.store
        Ledger(store).record("VST", "buy", "2", "100", "USD", "2026-11-03", fee="1")
        paper = Paper(store, clock=lambda: fixture.now)
        paper.handle(["start"])
        session = USCalendar().session(date(2026, 11, 10))
        forecast = fixture.advisor.issue("VST", fixture.now)
        SnapshotService(store).capture(now=fixture.now)
        results = AdvisorResults(store)
        assessment = results.assess(forecast, {}, evaluated_at=forecast["closes"] + 8 * 86400)
        with store.connection() as db:
            run = paper.repo.current(db)
            before_paper = paper.inputs(db, "VST", session, fixture.now, run)
            before_advisor = Advisor(store).evidence_inputs(db, "VST", fixture.now, session)
            results._save(db, assessment)
            preserved = {
                table: db.execute(f"SELECT * FROM {table}").fetchall()
                for table in (
                    "trades",
                    "company_advice",
                    "advisor_evaluations",
                    "decision_snapshots",
                    "paper_runs",
                )
            }
            for table in (
                "event_delivery_aliases",
                "event_deliveries",
                "event_collection",
                "event_bindings",
                "event_revisions",
                "news_events",
                "event_targets",
                "event_evidence",
                "ui_tokens",
            ):
                db.execute(f"DROP TABLE {table}")
            for column in ("operation", "message_id", "attempts", "retry_at", "fallback"):
                db.execute(f"ALTER TABLE outbox DROP COLUMN {column}")
            db.execute("PRAGMA user_version=11")
        reopened = Store(store.path)
        EventStore(reopened).ingest([self.item("Vistra cuts full-year guidance")], "VST", self.now)
        with reopened.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            for table, rows in preserved.items():
                self.assertEqual(db.execute(f"SELECT * FROM {table}").fetchall(), rows)
            self.assertEqual(
                Paper(reopened).inputs(db, "VST", session, fixture.now, run), before_paper
            )
            self.assertEqual(
                Advisor(reopened).evidence_inputs(db, "VST", fixture.now, session), before_advisor
            )
