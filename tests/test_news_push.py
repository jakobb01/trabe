"""Opt-in identity, transaction rollback, quiet clocks and conservative enqueue caps."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from trabe.news_event_store import EventStore
from trabe.news_events import analyze
from trabe.news_push import NewsPush, quiet
from trabe.outbox import Outbox
from trabe.store import Store


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class PushTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "push.db")
        self.now = stamp("2026-11-14T12:00:00+00:00")
        self.repo = EventStore(self.store)
        self.push = NewsPush(self.store, 123)
        for ticker in ("AMD", "AMZN", "GOOGL", "NVDA", "VST"):
            self.store.add(ticker, 2)
        with patch("trabe.store.time.time", return_value=self.now - 30):
            self.store.set_setting("news_enabled", "on")
            self.store.set_setting("news_alerts_enabled", "on")

    def ingest(self, ticker="NVDA", **overrides):
        item = {
            "provider": "fixture",
            "item_id": ticker,
            "url": "https://example.com/" + ticker,
            "headline": ticker + " announces CEO transition",
            "snippet": "",
            "publisher": "Fixture",
            "published_at": self.now - 10,
            "input_quality": "headline_only",
            **overrides,
        }
        self.repo.ingest([item], ticker, self.now)
        return item

    def test_defaults_and_enable_watermark_never_replay_seed_history(self):
        self.store.set_setting("news_alerts_enabled", "off")
        self.ingest()
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 1), 0)
        with patch("trabe.store.time.time", return_value=self.now + 2):
            self.store.set_setting("news_alerts_enabled", "on")
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 3), 0)
        self.assertEqual(self.store.settings()["news_quiet_start"], "22:00")

    def test_outbound_alert_uses_retained_source_detail_for_the_same_delivery(self):
        body = (
            "NVIDIA (NASDAQ: NVDA) Board of Directors announced that its CEO "
            "has resigned, effective immediately."
        )
        self.ingest(snippet=body, input_quality="snippet")
        event = self.repo.events(["NVDA"], self.now)[0]
        self.assertNotIn(body, [fact["text"] for fact in event["facts"]])
        self.assertTrue(event["push_eligible"])
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 1), 1)
        telegram = Mock()
        telegram.send.return_value = {"message_id": 1}
        outbox = Outbox(self.store)
        outbox.flush(telegram, now=self.now + 2)
        self.assertEqual(outbox.pending(), 0)
        telegram.send.assert_called_once()
        chat_id, sent = telegram.send.call_args.args
        self.assertEqual(chat_id, 123)
        self.assertIn(body, sent)
        self.assertIn("Source: Fixture (feed extract)", sent)
        self.assertEqual(sent.count("Price impact unknown."), 1)
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 3), 0)

    def test_cross_provider_restart_one_initial_and_only_one_changed_followup(self):
        item = self.ingest()
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 1), 1)
        self.repo.ingest(
            [item | {"provider": "other", "item_id": "copy", "url": "https://copy.example/news"}],
            "NVDA",
            self.now + 2,
        )
        restarted = NewsPush(Store(self.store.path), 123)
        self.assertEqual(restarted.run_once(["NVDA"], self.now + 3), 0)
        self.repo.ingest(
            [
                item
                | {"headline": "Correction: NVDA cuts full-year guidance", "correction_of": "NVDA"}
            ],
            "NVDA",
            self.now + 4,
        )
        self.assertEqual(restarted.run_once(["NVDA"], self.now + 5), 1)
        self.repo.ingest(
            [item | {"headline": "NVDA raises full-year guidance"}], "NVDA", self.now + 6
        )
        self.assertEqual(restarted.run_once(["NVDA"], self.now + 7), 0)
        self.assertEqual(Outbox(self.store).pending(), 2)

    def test_v2_notified_source_and_v3_copy_never_replay_after_restart(self):
        current = analyze

        def legacy(item, ticker):
            result = current(item, ticker)
            result["rules"] = "events-v2"
            for key in ("relevance", "event_status", "news_role"):
                result.pop(key)
            return result

        with patch("trabe.news_event_store.analyze", side_effect=legacy):
            item = self.ingest()
        event = self.repo.events(["NVDA"], self.now)[0]
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 1), 1)
        self.repo.ingest([item], "NVDA", self.now + 2)
        self.repo.ingest(
            [
                item
                | {
                    "provider": "copy",
                    "publisher": "Other",
                    "item_id": "copy",
                    "url": "https://copy.example/v3",
                }
            ],
            "NVDA",
            self.now + 3,
        )
        reopened = Store(self.store.path)
        self.assertEqual(
            EventStore(reopened).events(["NVDA"], self.now + 4)[0]["revision"], event["revision"]
        )
        self.assertEqual(NewsPush(reopened, 123).run_once(["NVDA"], self.now + 4), 0)
        self.repo.ingest(
            [
                item
                | {
                    "headline": "Correction: NVIDIA cuts full-year guidance",
                    "correction_of": "NVDA",
                }
            ],
            "NVDA",
            self.now + 5,
        )
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 6), 1)
        self.assertEqual(self.store.settings()["news_alerts_enabled"], "on")

    def test_indirect_audience_cannot_receive_another_issuers_initial_event_or_queued_push(self):
        self.store.remove("NVDA")
        self.ingest(
            "AMZN",
            headline="Gatekeeper is available through AWS Marketplace; NVIDIA reported quarterly results.",
        )
        event = self.repo.events(["AMZN"], self.now)[0]
        self.assertEqual(event["ticker"], "NVDA")
        self.assertTrue(event["push_eligible"])
        self.assertEqual(self.push.run_once(["AMZN"], self.now + 1), 0)
        self.store.add("NVDA", 2)
        self.assertEqual(self.push.run_once(["AMZN", "NVDA"], self.now + 2), 1)
        self.store.remove("NVDA")
        with self.store.connection() as db:
            item_id = db.execute("SELECT id FROM outbox WHERE kind='news_event'").fetchone()[0]
            self.assertEqual(NewsPush.flush_policy(self.store, db, item_id, self.now + 3), "drop")

    def test_explicit_withdrawal_routes_to_prior_issuer_without_inventing_an_actor(self):
        item = self.ingest()
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 1), 1)
        withdrawn = item | {
            "item_id": "withdrawal",
            "url": "https://example.com/withdrawal",
            "headline": "Retraction: the previous report is withdrawn",
            "correction_of": "NVDA",
        }
        self.repo.ingest([withdrawn], "NVDA", self.now + 2)
        event = self.repo.events(["NVDA"], self.now + 3)[0]
        self.assertTrue(event["correction"])
        self.assertIsNone(event["actor"])
        self.assertEqual(event["issuers"], ["NVDA"])
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 3), 1)

    def test_correction_audience_bindings_count_for_both_original_and_corrected_issuers(self):
        item = self.ingest()
        correction = item | {
            "item_id": "corrected-issuer",
            "url": "https://example.com/corrected-issuer",
            "headline": "Correction: Alphabet says guidance remains unchanged",
            "correction_of": "NVDA",
        }
        self.repo.ingest([correction], "NVDA", self.now + 1)
        with self.store.connection() as db:
            targets = {
                r[0]
                for r in db.execute(
                    "SELECT t.ticker FROM event_targets t JOIN event_evidence e ON e.id=t.evidence WHERE e.item_id='corrected-issuer'"
                )
            }
        self.assertEqual(targets, {"NVDA", "GOOGL"})

    def test_rewritten_provider_reports_stay_digest_only_and_old_roots_share_push_limit(self):
        self.ingest()
        self.repo.ingest(
            [
                {
                    "provider": "other",
                    "item_id": "rewrite",
                    "url": "https://other.example/rewrite",
                    "headline": "NVIDIA confirms chief executive officer transition",
                    "snippet": "",
                    "published_at": self.now - 9,
                    "input_quality": "headline_only",
                }
            ],
            "NVDA",
            self.now + 1,
        )
        events = self.repo.events(["NVDA"], self.now + 2)
        self.assertEqual(len(events), 2)
        self.assertEqual(sum(e["ambiguous"] for e in events), 1)
        # Existing databases may have roots created before ambiguity detection.
        with self.store.connection() as db:
            db.execute("UPDATE news_events SET ambiguous=0")
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 2), 1)
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 3), 0)
        self.assertEqual(Outbox(self.store).pending(), 1)

    def test_quiet_pending_work_respects_combined_identity_cap_before_insert(self):
        self.ingest()
        self.store.set_setting("news_quiet_start", "00:00")
        self.store.set_setting("news_quiet_end", "23:59")
        with self.store.connection() as db:
            db.executemany(
                "INSERT INTO event_delivery_aliases VALUES (?,?,?)",
                [(f"retained-{i}", "prior", self.now) for i in range(2000)],
            )
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 1), 0)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT (SELECT COUNT(*) FROM event_deliveries)+(SELECT COUNT(*) FROM event_delivery_aliases)"
                ).fetchone()[0],
                2000,
            )

    def test_quiet_hold_does_not_block_reply_and_disable_drops_queued_generation(self):
        self.store.set_setting("news_quiet_enabled", "off")
        self.ingest()
        self.push.run_once(["NVDA"], self.now + 1)
        self.store.set_setting("news_quiet_start", "00:00")
        self.store.set_setting("news_quiet_end", "23:59")
        self.store.set_setting("news_quiet_enabled", "on")
        outbox = Outbox(self.store)
        outbox.send(123, "unrelated reply", now=self.now + 2)
        telegram = Mock()
        outbox.flush(telegram, now=self.now + 3)
        telegram.send.assert_called_once_with(123, "unrelated reply")
        self.assertEqual(outbox.pending(), 1)
        self.store.set_setting("news_alerts_enabled", "off")
        self.assertEqual(outbox.pending(), 0)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT status FROM event_deliveries").fetchone()[0], "suppressed"
            )

    def test_quiet_defers_then_coalesces_latest_facts(self):
        self.store.set_setting("news_quiet_start", "00:00")
        self.store.set_setting("news_quiet_end", "23:59")
        item = self.ingest()
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 1), 0)
        self.repo.ingest(
            [item | {"headline": "NVDA raises full-year guidance"}], "NVDA", self.now + 2
        )
        self.store.set_setting("news_quiet_enabled", "off")
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 3), 1)
        with self.store.connection() as db:
            text = db.execute("SELECT text FROM outbox").fetchone()[0]
        self.assertIn("raises full-year guidance", text)
        self.assertNotIn("CEO transition", text)

    def test_daily_and_rolling_cap_survives_timezone_and_expiry(self):
        for ticker in ("AMD", "AMZN", "GOOGL", "NVDA", "VST"):
            self.ingest(ticker)
        self.assertEqual(
            self.push.run_once(["AMD", "AMZN", "GOOGL", "NVDA", "VST"], self.now + 1), 3
        )
        self.store.set_setting("timezone", "Pacific/Kiritimati")
        self.assertEqual(
            self.push.run_once(["AMD", "AMZN", "GOOGL", "NVDA", "VST"], self.now + 2), 0
        )
        self.push.expire(self.now + 86400)
        self.assertEqual(Outbox(self.store).pending(), 0)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM event_deliveries WHERE enqueued IS NOT NULL"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM event_deliveries WHERE status='suppressed'"
                ).fetchone()[0],
                2,
            )

    def test_quiet_dst_and_cross_midnight_use_local_wall_time(self):
        settings = self.store.settings() | {
            "timezone": "America/New_York",
            "news_quiet_start": "22:00",
            "news_quiet_end": "08:00",
        }
        for value in (
            "2026-11-01T05:30:00+00:00",
            "2026-11-01T06:30:00+00:00",
            "2026-03-08T06:59:00+00:00",
            "2026-03-08T07:01:00+00:00",
        ):
            self.assertTrue(quiet(settings, stamp(value)))
        self.assertFalse(quiet(settings, stamp("2026-11-01T13:00:00+00:00")))

    def test_pause_off_and_transaction_failure_leave_no_half_enqueue(self):
        self.ingest()
        with patch.object(Outbox, "send", side_effect=RuntimeError("fixture rollback")):
            with self.assertRaises(RuntimeError):
                self.push.run_once(["NVDA"], self.now + 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM event_delivery_aliases").fetchone()[0], 0
            )
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 2), 1)
        self.store.pause_automation(self.now + 100)
        self.assertEqual(Outbox(self.store).pending(), 0)
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 101), 0)

    def test_removed_symbol_is_rechecked_before_enqueue_and_tombstones_outlive_evidence(self):
        self.ingest()
        self.store.remove("NVDA")
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 1), 0)
        self.store.add("NVDA", 2)
        self.assertEqual(self.push.run_once(["NVDA"], self.now + 2), 1)
        self.push.expire(self.now + 86400)
        self.repo.prune(self.now + 15 * 86400)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_deliveries").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM event_delivery_aliases").fetchone()[0], 3
            )
