"""Owner-bound callbacks, immediate acknowledgements and durable typed edits."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from trabe.advisor import Advisor
from trabe.api import ApiError, Telegram
from trabe.bot import COMMANDS, Bot
from trabe.config import Config
from trabe.navigation import bind_markup
from trabe.news_digest import NewsDigest, units
from trabe.news_event_store import EventStore
from trabe.outbox import Outbox
from trabe.paper import Paper
from trabe.snapshots import SnapshotService
from trabe.store import Store


class NavigationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "navigation.db")
        self.store.add("NVDA", 2)
        self.config = Config("100:" + "a" * 32, 123, "fixture", self.store.path)
        self.transport = Mock()
        self.transport.send.return_value = {"message_id": 77}
        self.transport.edit.return_value = {"message_id": 77}
        self.outbox = Outbox(self.store)
        self.bot = Bot(
            self.config, self.store, self.outbox, Mock(), callback_responder=self.transport
        )
        self.bot.bot_id = 100
        self.addCleanup(self.bot.scans.close)

    def token(self, action, **kwargs):
        button = self.bot.navigation.button("Test", action, **kwargs)
        with self.store.connection() as db:
            bind_markup(db, {"inline_keyboard": [[button]]}, 123, 77)
        return button["callback_data"]

    def query(self, data):
        return {
            "id": "callback-fixture",
            "from": {"id": 123, "is_bot": False},
            "data": data,
            "message": {
                "message_id": 77,
                "date": 1794657600,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 100, "is_bot": True},
            },
        }

    def open_home(self):
        self.bot.handle(
            {"message": self.query("")["message"] | {"from": {"id": 123}, "text": "/home"}}
        )
        self.outbox.flush(self.transport)
        self.message = self.transport.send.call_args.args[1]
        self.markup = self.transport.send.call_args.kwargs["reply_markup"]

    def button_names(self):
        return {
            button["text"]: button["callback_data"]
            for row in self.markup["inline_keyboard"]
            for button in row
        }

    def tap(self, name):
        self.bot.handle({"callback_query": self.query(self.button_names()[name])})
        self.assertEqual(self.outbox.pending(), 1)
        self.outbox.flush(self.transport)
        self.assertEqual(self.transport.edit.call_args.args[:2], (123, 77))
        self.message = self.transport.edit.call_args.args[2]
        self.markup = self.transport.edit.call_args.kwargs["reply_markup"]
        self.assertLessEqual(units(self.message), 3500)
        self.assertTrue(all(len(token.encode()) <= 64 for token in self.button_names().values()))
        return self.message

    def counts(self, *tables):
        with self.store.connection() as db:
            return tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
            )

    def test_home_send_binds_tokens_and_callback_ack_precedes_cached_edit(self):
        self.bot.handle(
            {
                "message": {
                    "text": "/home",
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 123},
                }
            }
        )
        self.outbox.flush(self.transport)
        markup = self.transport.send.call_args.kwargs["reply_markup"]
        data = markup["inline_keyboard"][0][1]["callback_data"]

        def ack(*args):
            self.assertEqual(self.outbox.pending(), 0)

        self.transport.answer_callback.side_effect = ack
        with patch(
            "trabe.signals.SignalHTTP._read", side_effect=AssertionError("cached navigation HTTP")
        ):
            self.bot.process({"update_id": 10, "callback_query": self.query(data)})
        self.transport.answer_callback.assert_called_once_with("callback-fixture")
        self.assertEqual(self.store.state("telegram_offset"), "11")
        self.assertEqual(self.outbox.pending(), 1)
        self.outbox.flush(self.transport)
        self.transport.edit.assert_called_once()
        self.assertIn(
            "NVDA",
            self.transport.edit.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["text"],
        )
        self.assertEqual(
            [c for c, _ in COMMANDS], ["home", "briefing", "news", "portfolio", "status", "help"]
        )

    def test_foreign_sender_chat_bot_author_and_inaccessible_messages_are_silent(self):
        query = self.query(self.token("settings"))
        cases = []
        for path, value in (
            (["from", "id"], 999),
            (["from", "is_bot"], True),
            (["message", "chat", "id"], 999),
            (["message", "chat", "type"], "group"),
            (["message", "from", "id"], 999),
            (["message", "date"], 0),
        ):
            candidate = copy.deepcopy(query)
            target = candidate
            for field in path[:-1]:
                target = target[field]
            target[path[-1]] = value
            cases.append(candidate)
        cases.append({k: v for k, v in query.items() if k != "message"})
        for candidate in cases:
            self.bot.navigation.callback(candidate)
        self.transport.answer_callback.assert_not_called()
        self.assertEqual(self.outbox.pending(), 0)

    def test_forged_expired_wrong_message_and_unbound_tokens_do_not_mutate(self):
        valid = self.token("set", value="news_alerts_enabled:on")
        forged = self.query("n:forged")
        wrong_message = self.query(valid)
        wrong_message["message"]["message_id"] = 78
        unbound = self.bot.navigation.button("Test", "set", value="news_alerts_enabled:on")[
            "callback_data"
        ]
        for candidate in (forged, wrong_message, self.query(unbound)):
            self.bot.navigation.callback(candidate)
        with self.store.connection() as db:
            db.execute("UPDATE ui_tokens SET expires=1 WHERE token=?", (valid[2:],))
        self.bot.navigation.callback(self.query(valid))
        self.assertEqual(self.store.settings()["news_alerts_enabled"], "off")
        self.assertEqual(self.outbox.pending(), 0)
        self.assertEqual(self.transport.answer_callback.call_count, 4)
        self.transport.answer_callback.assert_called_with("callback-fixture", "Open /home again")

    def test_explicit_setting_consumed_atomically_replay_and_restart(self):
        token = self.token("set", value="news_alerts_enabled:on")
        self.bot.navigation.callback(self.query(token))
        self.assertEqual(self.store.settings()["news_alerts_enabled"], "on")
        generation = self.store.state("news_event_generation")
        reopened = Store(self.store.path)
        self.bot.navigation.callback(self.query(token))
        self.assertEqual(reopened.state("news_event_generation"), generation)
        self.assertEqual(self.outbox.pending(), 1)
        with reopened.connection() as db:
            self.assertEqual(
                db.execute("SELECT used FROM ui_tokens WHERE token=?", (token[2:],)).fetchone()[0],
                1,
            )

    def test_edits_coalesce_retry_and_deleted_message_falls_back_once_with_rebind(self):
        markup = {"inline_keyboard": [[self.bot.navigation.button("Home", "home", message_id=77)]]}
        self.outbox.edit(123, 77, "old")
        self.outbox.edit(123, 77, "new 📰", reply_markup=markup)
        self.assertEqual(self.outbox.pending(), 1)
        self.transport.edit.side_effect = ApiError("deleted", 400, reason="uneditable")
        self.outbox.flush(self.transport)
        self.assertEqual(self.outbox.pending(), 1)
        self.transport.send.return_value = {"message_id": 78}
        self.outbox.flush(self.transport)
        self.transport.send.assert_called_once_with(123, "new 📰", reply_markup=markup)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT message_id FROM ui_tokens WHERE token=?",
                    (markup["inline_keyboard"][0][0]["callback_data"][2:],),
                ).fetchone()[0],
                78,
            )
        self.outbox.edit(123, 77, "retry")
        self.transport.edit.side_effect = ApiError("temporary", 429, 10)
        self.outbox.flush(self.transport)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT attempts FROM outbox").fetchone()[0], 1)

    def test_real_telegram_contract_entities_markup_ack_and_allowed_updates(self):
        calls = []

        def request(url, payload, **kwargs):
            calls.append((url.rsplit("/", 1)[-1], payload))
            return {"ok": True, "result": [] if url.endswith("getUpdates") else {"message_id": 77}}

        telegram = Telegram(self.config.token, request)
        telegram._next_send = 100000000000
        with patch("trabe.api.time.sleep") as sleep:
            telegram.answer_callback("query", "Opening")
            sleep.assert_not_called()
            telegram._next_send = 0
            telegram.edit(
                123, 77, "📰 NVDA\nhttps://example.com/source", reply_markup={"inline_keyboard": []}
            )
        telegram.updates(4)
        self.assertEqual(calls[0][0], "answerCallbackQuery")
        self.assertEqual(calls[1][0], "editMessageText")
        self.assertIn("entities", calls[1][1])
        self.assertIn("reply_markup", calls[1][1])
        self.assertEqual(calls[2][1]["allowed_updates"], ["message", "callback_query"])

    def test_cached_company_views_issue_no_new_financial_records_or_provider_calls(self):
        for action in ("news", "sources", "research", "earnings", "insiders", "portfolio"):
            with (
                self.subTest(action=action),
                patch(
                    "trabe.signals.SignalHTTP._read", side_effect=AssertionError("provider call")
                ),
            ):
                self.bot.navigation.render(action, ticker="NVDA" if action != "portfolio" else "")
        with self.store.connection() as db:
            for table in (
                "company_advice",
                "decision_snapshots",
                "paper_runs",
                "signal_jobs",
                "trades",
            ):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_news_journey_preserves_company_page_and_only_refresh_queues(self):
        now = 1794657600.0
        self.store.add("AMD", 2)
        repo = EventStore(self.store)
        stories = [
            ("NVDA", "NVIDIA appoints a new CEO", now - 60),
            ("NVDA", "NVIDIA raises fourth-quarter guidance", now - 70),
            ("NVDA", "NVIDIA completes acquisition of Fixture Robotics", now - 80),
            ("NVDA", "NVIDIA announces a new factory", now - 4 * 86400),
            ("AMD", "AMD beats revenue estimates", now - 90),
        ]
        for index, (ticker, headline, published) in enumerate(stories):
            repo.ingest(
                [
                    {
                        "headline": headline,
                        "snippet": "",
                        "provider": "fixture",
                        "item_id": str(index),
                        "url": f"https://example.com/{index}",
                        "published_at": published,
                        "input_quality": "headline_only",
                    }
                ],
                ticker,
                now - index,
            )
        for ticker in ("AMD", "NVDA"):
            repo.coverage("fixture", ticker, now, state="complete")
        self.assertEqual(len(repo.events(["NVDA"], now, presentation=True)), 3)
        with (
            patch("trabe.navigation.time.time", return_value=now),
            patch("trabe.signals.SignalHTTP._read", side_effect=AssertionError("provider call")),
        ):
            self.open_home()
            self.assertIn("All companies", self.tap("News"))
            self.assertIn("News", self.tap("Choose company"))
            first = self.tap("NVDA")
            self.assertIn("NVDA • Latest cached • 1/3", first)
            self.assertNotIn("Previous", self.button_names())
            second = self.tap("Next")
            self.assertIn("NVDA • Latest cached • 2/3", second)
            self.assertNotEqual(first.split("\n")[2:], second.split("\n")[2:])
            self.assertIn("NVDA/fixture", self.tap("Sources"))
            self.assertNotIn("AMD/fixture", self.message)
            self.assertEqual(second, self.tap("Back to news"))
            self.assertIn("NVIDIA announces a new factory", self.tap("Earlier coverage"))
            self.assertEqual(second, self.tap("Back to news"))
            self.assertIn("NVDA • Latest cached • 3/3", self.tap("Next"))
            self.assertNotIn("Next", self.button_names())
            self.assertEqual(second, self.tap("Previous"))
            self.assertEqual(self.counts("signal_jobs"), (0,))
            self.assertIn("News refresh queued for 1 symbol(s)", self.tap("Refresh news"))
            self.assertIn("NVDA • Latest cached • 2/3", self.message)
            self.tap("Refresh news")
            with self.store.connection() as db:
                self.assertEqual(
                    db.execute("SELECT json_extract(symbols,'$.all') FROM signal_jobs").fetchall(),
                    [('["NVDA"]',)],
                )
            self.assertIn("All companies", self.tap("All news"))

    def test_news_reaches_every_long_card_and_clamps_a_stale_page(self):
        event = {
            "ticker": "NVDA",
            "sources": [{"publisher": "Fixture"}],
            "type": "earnings",
            "materiality": "high",
            "published": 1794657500.0,
            "quality": "article",
            "attribution": "reporting",
            "uncertainty": "",
            "direction": "unknown",
        }
        events = [
            event
            | {
                "facts": [
                    {"id": "s1", "text": f"Story {i} begins with " + "🧪" * 175},
                    {"id": "s2", "text": "Material qualifier for the report " + "🧪" * 175},
                ]
            }
            for i in range(54)
        ]
        with patch.object(EventStore, "events", return_value=events):
            slash = NewsDigest(self.store).report(["NVDA"], 1794657600.0)
            self.assertNotIn("Story 3 begins", slash)
            legacy_more = self.token("news", ticker="NVDA", page=1)
            self.bot.navigation.callback(self.query(legacy_more))
            self.outbox.flush(self.transport)
            legacy_text = self.transport.edit.call_args.args[2]
            self.assertIn("NVDA • Latest cached • 4/54", legacy_text)
            self.assertIn("Story 3 begins", legacy_text)
            self.open_home()
            text = self.tap("News")
            for i in range(len(events)):
                self.assertIn(f"Story {i} begins", text)
                self.assertIn("Material qualifier", text)
                self.assertIn(f"{i + 1}/54", text)
                if i + 1 < len(events):
                    text = self.tap("Next")
            self.assertNotIn("Next", self.button_names())
            events[:] = events[:3]
            self.assertIn("Story 2 begins", self.tap("Previous"))
            self.assertNotIn("Next", self.button_names())
            self.assertLessEqual(self.counts("ui_tokens")[0], 256)

    def test_advisor_journey_reads_scoped_evidence_and_preserves_enrollment(self):
        now = 1794657600.0
        for i in range(9):
            self.store.add(f"AAA{i}", 2)
        record = Advisor(self.store).issue("NVDA", now)
        tables = (
            "company_advice",
            "advisor_evaluations",
            "shadow_company_advice",
            "trades",
            "paper_runs",
            "signal_jobs",
        )
        before = self.counts(*tables)
        settings = self.store.settings()
        with (
            patch("trabe.navigation.time.time", return_value=now),
            patch("trabe.signals.SignalHTTP._read", side_effect=AssertionError("provider call")),
        ):
            self.open_home()
            self.assertIn("/advisor TICKER", self.tap("Advisor"))
            self.assertIn("1/2", self.tap("Choose company"))
            self.assertNotIn("NVDA", self.button_names())
            self.assertIn("2/2", self.tap("Next"))
            self.assertIn("NVDA research", self.tap("NVDA"))
            self.assertIn(f"Record {record['id']}", self.tap("Saved evidence"))
            self.assertIn("NVDA preview", self.tap("Preview"))
            self.assertIn("Company results • NVDA", self.tap("Results"))
            self.assertIn("Company forecast comparison • NVDA", self.tap("Compare"))
            self.assertIn("page 2/2", self.tap("Advisor companies"))
            self.tap("Advisor")
            self.assertIn("Automatic forecast sampling", self.tap("Sampling status"))
            self.tap("Advisor")
            self.assertIn("Owner-issued cohort", self.tap("Compare methods"))
            self.assertIn("Automatic shadow cohort", self.message)
            self.tap("Advisor")
            self.assertIn("Company results", self.tap("All results"))
        self.assertEqual(self.counts(*tables), before)
        self.assertEqual(self.store.settings(), settings)

    def test_market_paper_and_portfolio_reports_use_existing_cached_data(self):
        self.store.set_setting("default_currency", "USD")
        self.store.set_setting("market_provider", "alphavantage")
        self.bot.ledger.record("VST", "buy", "2", "45", "USD", "2026-11-06")
        self.store.save_quote(
            "NVDA", {"price": 131, "change_percent": 2, "trading_day": "2026-11-06"}
        )
        self.store.finish_scan(self.store.start_scan(), "Saved scan summary")
        snapshot = SnapshotService(self.store).capture(now=1794657600.0)
        Paper(self.store).handle(["start"])
        tables = (
            "decision_snapshots",
            "market_outlooks",
            "paper_runs",
            "paper_decisions",
            "trades",
            "signal_jobs",
        )
        before = self.counts(*tables)
        settings = self.store.settings()
        with patch("trabe.signals.SignalHTTP._read", side_effect=AssertionError("provider call")):
            self.open_home()
            self.tap("Reports")
            self.tap("Market & monitoring")
            for label, expected in (
                ("Cached prices", "NVDA: 131.0000"),
                ("Alerts", "Intraday alerts"),
                ("Last scan", "Saved scan summary"),
                ("Latest snapshot", f"Snapshot #{snapshot['id'][:8]}"),
            ):
                self.assertIn(expected, self.tap(label))
                self.tap("Market & monitoring")
            self.assertIn("No saved outlook", self.tap("Market outlook"))
            self.assertIn("outlook", self.tap("Outlook results").lower())
            self.tap("Market & monitoring")
            self.tap("Reports")
            self.assertIn("Cash $2000.00", self.tap("Paper experiment"))
            self.assertIn("Cash $2000.00", self.tap("Paper results"))
            self.assertIn("No saved decisions", self.tap("Latest decisions"))
            self.assertIn("No simulated trades", self.tap("Recent fills"))
            self.tap("Home")
            self.assertIn("VST", self.tap("Portfolio"))
            self.tap("Positions & transactions")
            self.assertNotIn("NVDA", self.button_names())
            self.assertIn("Cost basis", self.tap("VST"))
            self.assertIn("buy 2 at 45 USD", self.tap("Transactions"))
            self.assertIn("Cost basis", self.tap("Position"))
        self.assertEqual(self.counts(*tables), before)
        self.assertEqual(self.store.settings(), settings)

    def test_empty_scope_and_legacy_token_for_removed_company(self):
        legacy = self.token("news", ticker="NVDA", page=1)
        self.store.remove("NVDA")
        self.bot.navigation.callback(self.query(legacy))
        self.outbox.flush(self.transport)
        self.assertIn("no longer held or watched", self.transport.edit.call_args.args[2])
        self.open_home()
        self.assertIn("No held or watched companies", self.tap("News"))
        self.assertNotIn("Next", self.button_names())
        self.assertNotIn("Refresh news", self.button_names())
        self.assertIn("No held or watched companies", self.tap("Choose company"))
