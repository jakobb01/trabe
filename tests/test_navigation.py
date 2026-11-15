"""Owner-bound callbacks, immediate acknowledgements and durable typed edits."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from trabe.api import ApiError, Telegram
from trabe.bot import COMMANDS, Bot
from trabe.config import Config
from trabe.navigation import bind_markup
from trabe.outbox import Outbox
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
