"""One targeted retention/resource fixture. No network; run for event-storage changes."""

import gc
import json
import platform
import resource
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from trabe.news_digest import NewsDigest
from trabe.news_event_store import PAYLOAD_LIMIT, EventStore
from trabe.news_events import analyze, digest, encoded
from trabe.store import Store


class BoundedEventStorage(unittest.TestCase):
    def test_max_cap_reopen_prune_and_pending_evidence_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "bounded.db")
            repo = EventStore(store)
            now = 1794657600.0
            tickers = ["FIX" + chr(65 + i) for i in range(40)]
            started = time.perf_counter()
            with store.connection() as db:
                for i in range(250):
                    ticker = tickers[i % 40]
                    event_id = f"event{i}"
                    published = now - i
                    db.execute(
                        "INSERT INTO news_events VALUES (?,?,?,?,?,?,?)",
                        (event_id, digest(event_id), now, now, published, "high", 0),
                    )
                    for j in range(4):
                        headline = f"{ticker} raises full-year guidance to {i * 4 + j}"
                        item = {
                            "headline": headline,
                            "snippet": "",
                            "published_at": published,
                            "input_quality": "snippet",
                        }
                        analysis = analyze(item, ticker) | {
                            "issuers": [ticker],
                            "seeded": False,
                            "correction": False,
                        }
                        # A large but supported source fragment exercises serialized budgets.
                        analysis["uncertainty"] = "Unverified source scope. " * 72
                        raw = encoded(analysis)
                        self.assertLessEqual(len(raw.encode()), 4096)
                        revision = f"revision{i}-{j}"
                        db.execute(
                            "INSERT INTO event_revisions VALUES (?,?,?,?,?)",
                            (revision, event_id, digest(revision), now + j - 4, raw),
                        )
                        evidence = f"evidence{i}-{j}"
                        payload = encoded(
                            {
                                "headline": headline,
                                "snippet": "Source sentence. " * 100,
                                "publisher": "Fixture",
                                "role": "",
                                "input_quality": "snippet",
                                "sentences": [
                                    {"id": f"s{k}", "text": "Source sentence."} for k in range(120)
                                ],
                            }
                        )
                        self.assertLessEqual(len(payload.encode()), 8192)
                        db.execute(
                            "INSERT INTO event_evidence VALUES (?,?,?,?,?,?,?,?)",
                            (
                                evidence,
                                "fixture",
                                evidence,
                                "https://example.com/" + evidence,
                                digest(payload),
                                published,
                                now,
                                payload,
                            ),
                        )
                        db.execute("INSERT INTO event_targets VALUES (?,?)", (evidence, ticker))
                        db.execute(
                            "INSERT INTO event_bindings VALUES (?,?,?)",
                            (event_id, revision, evidence),
                        )
                # Maximum compact delivery identities, retaining one pending event.
                for i in range(2000):
                    db.execute(
                        "INSERT INTO event_deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"key{i}",
                            "event0",
                            "revision0-3",
                            123,
                            1,
                            "pending" if i == 0 else "suppressed",
                            "fixture",
                            now,
                            now + 86400,
                            None,
                            None,
                            None,
                        ),
                    )
                self.assertLessEqual(repo.payload_bytes(db), PAYLOAD_LIMIT)
            load_seconds = time.perf_counter() - started
            store = Store(store.path)
            repo = EventStore(store)
            from trabe.news import News

            legacy = News(store, object())
            for ticker in tickers:
                items = [
                    {
                        "provider": "fixture",
                        "item_id": ticker + str(i),
                        "url": f"https://legacy.example/{ticker}/{i}",
                        "headline": f"{ticker} raises guidance",
                        "snippet": "Source sentence. " * 200,
                        "published_at": now - i,
                        "input_quality": "snippet",
                    }
                    for i in range(20)
                ]
                legacy._merge(items, ticker, "fixture", now)
            gc.collect()
            tracemalloc.start()
            baseline_start = time.perf_counter()
            baseline = legacy.legacy_report(tickers, now=now)
            baseline_seconds = time.perf_counter() - baseline_start
            baseline_peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.reset_peak()
            start = time.perf_counter()
            rendered = NewsDigest(store).report(tickers, now)
            render_seconds = time.perf_counter() - start
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            self.assertTrue(baseline)
            self.assertLess(peak - baseline_peak, 32 * 1024 * 1024)
            self.assertLessEqual(len(rendered.encode("utf-16-le")) // 2, 3500)
            # One over-cap insert must shorten unpinned history and show a gap.
            item = {
                "headline": "FIXA reports quarterly results",
                "snippet": "",
                "provider": "fixture",
                "item_id": "overflow",
                "url": "https://example.com/overflow",
                "published_at": now,
                "input_quality": "headline_only",
            }
            repo.ingest([item], "FIXA", now)
            with store.connection() as db:
                self.assertLessEqual(
                    db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0], 250
                )
                self.assertLessEqual(
                    db.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0], 1000
                )
                self.assertFalse(
                    db.execute(
                        "SELECT ticker FROM event_targets GROUP BY ticker HAVING COUNT(*)>100"
                    ).fetchone()
                )
                self.assertFalse(
                    db.execute(
                        "SELECT event_id FROM event_revisions GROUP BY event_id HAVING COUNT(*)>4"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    db.execute("SELECT 1 FROM event_evidence WHERE id='evidence0-3'").fetchone()
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0], 2000
                )
                serialized = repo.payload_bytes(db)
                self.assertLessEqual(serialized, PAYLOAD_LIMIT)
            self.assertIn("partial", " ".join(repo.coverage_lines(tickers, now)))
            repo.prune(now + 15 * 86400)
            with store.connection() as db:
                self.assertIsNotNone(
                    db.execute("SELECT 1 FROM news_events WHERE id='event0'").fetchone()
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0], 2000
                )
            print(
                json.dumps(
                    {
                        "platform": platform.platform(),
                        "architecture": platform.machine(),
                        "load_seconds": load_seconds,
                        "legacy_render_seconds": baseline_seconds,
                        "render_seconds": render_seconds,
                        "incremental_python_peak_bytes": peak - baseline_peak,
                        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        "serialized_bytes_at_cap": serialized,
                        "database_bytes": store.path.stat().st_size,
                        "wal_bytes": Path(str(store.path) + "-wal").stat().st_size
                        if Path(str(store.path) + "-wal").exists()
                        else 0,
                        "scope": "250 roots, 1000 evidence/revisions, 2000 tombstones. Desktop timing does not establish ARM timing.",
                    }
                )
            )
