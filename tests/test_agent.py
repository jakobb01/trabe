import io
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from stock_agent.api import ApiError, MarketData, Telegram, request_json
from stock_agent.bot import Bot
from stock_agent.config import Config, read_env
from stock_agent.runtime import health
from stock_agent.setup import setup, write_credentials
from stock_agent.store import Store

TOKEN = "123456:" + "a" * 32
QUOTE = {
    "Global Quote": {
        "01. symbol": "IBM",
        "05. price": "257.81",
        "10. change percent": "-1.20%",
        "07. latest trading day": "2026-10-30",
    }
}


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.store = Store(self.path / "agent.db")
        self.config = Config(TOKEN, 123, "secret-key", self.store.path)
        self.requests = []

        def transport(*args, **kwargs):
            self.requests.append((args, kwargs))
            return json.loads(json.dumps(QUOTE))

        self.market = MarketData("secret-key", self.store, transport)
        self.telegram = FakeTelegram()
        self.bot = Bot(self.config, self.store, self.telegram, self.market)
        self.bot.username = "test_bot"

    def update(self, text, user=123, chat=123, kind="private", update_id=1):
        return {
            "update_id": update_id,
            "message": {"text": text, "from": {"id": user}, "chat": {"id": chat, "type": kind}},
        }

    def test_private_owner_can_manage_watchlist(self):
        self.bot.handle(self.update("/add ibm 1"))
        self.assertEqual(self.store.watchlist(), [("IBM", 1)])
        self.assertIn("Saved IBM", self.telegram.messages[-1][1])

    def test_unauthorized_chats_and_senders_are_ignored(self):
        for params in (
            {"user": 999},
            {"chat": 999},
            {"kind": "group"},
            {"kind": "supergroup"},
            {"user": 999, "chat": 999},
        ):
            self.bot.handle(self.update("/add IBM", **params))
        self.assertEqual(self.store.watchlist(), [])
        self.assertEqual(self.telegram.messages, [])

    def test_wrong_bot_target_is_ignored(self):
        self.bot.handle(self.update("/add@someone_else IBM"))
        self.assertEqual(self.telegram.messages, [])
        self.bot.handle(self.update("/add@Test_Bot IBM"))
        self.assertEqual(self.store.watchlist(), [("IBM", 2)])

    def test_invalid_inputs_do_not_write_or_use_api(self):
        for command in (
            "/add <script>",
            "/add IBM 0",
            "/add IBM 4",
            "/add IBM 2 extra",
            "/remove",
            "/quote",
            "/quote IBM MSFT",
        ):
            self.bot.handle(self.update(command))
        self.assertEqual(self.store.watchlist(), [])
        self.assertEqual(self.store.usage(), 0)

    def test_preferences_apply_and_survive_reopen(self):
        self.bot.command("set", ["timezone", "America/New_York"])
        self.bot.command("set", ["api_daily_budget", "5"])
        self.bot.command("add", ["IBM", "3"])
        reopened = Store(self.store.path)
        self.assertEqual(reopened.settings()["timezone"], "America/New_York")
        self.assertEqual(reopened.settings()["api_daily_budget"], "5")
        self.assertEqual(reopened.watchlist(), [("IBM", 3)])

    def test_bad_preferences_are_rejected(self):
        for args in (
            ["timezone", "No/SuchPlace"],
            ["timezone", "/etc/passwd"],
            ["api_daily_budget", "0"],
            ["api_daily_budget", "26"],
            ["api_daily_budget", "NaN"],
            ["quote_cache_minutes", "1441"],
            ["quote_cache_minutes", "²"],
            ["api_key", "secret"],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.bot.command("set", args)
        self.assertEqual(self.store.settings()["api_daily_budget"], "20")

    def test_status_does_not_consume_market_quota(self):
        result = self.bot.command("status", [])
        self.assertIn("not checked yet", result)
        self.assertIn("Live intraday alerts are not enabled", result)
        self.assertEqual(self.requests, [])

    def test_deleting_last_symbol_does_not_reseed(self):
        self.store.add("IBM", 1)
        self.assertTrue(self.store.remove("IBM"))
        self.assertEqual(Store(self.store.path).watchlist(), [])

    def test_quote_is_validated_cached_and_budgeted(self):
        quote, cached = self.market.quote("IBM")
        self.assertEqual(quote["price"], 257.81)
        self.assertFalse(cached)
        self.assertTrue(self.market.quote("IBM")[1])
        self.assertEqual(self.store.usage(), 1)
        self.assertEqual(len(self.requests), 1)

    def test_doctor_refresh_uses_real_request_even_when_cached(self):
        self.market.quote("IBM")
        self.assertFalse(self.market.quote("IBM", refresh=True)[1])
        self.assertEqual(len(self.requests), 2)

    def test_cache_survives_reopen(self):
        self.market.quote("IBM")
        other = MarketData(
            "secret-key", Store(self.store.path), lambda *a, **k: self.fail("network called")
        )
        self.assertTrue(other.quote("IBM")[1])

    def test_expired_cache_fetches_again(self):
        self.market.quote("IBM")
        with self.store.connection() as db:
            db.execute("UPDATE quote_cache SET fetched_at=0")
        self.market.quote("IBM")
        self.assertEqual(len(self.requests), 2)

    def test_budget_blocks_request_before_network(self):
        self.store.set_setting("api_daily_budget", "1")
        self.market.quote("IBM")
        with self.assertRaisesRegex(ValueError, "budget reached"):
            self.market.quote("MSFT")
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.market.quote("IBM")[1])

    def test_concurrent_budget_reservations_are_atomic(self):
        def attempt(_):
            try:
                self.store.reserve_request(3)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(attempt, range(20)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(self.store.usage(), 3)

    def test_budget_resets_on_new_utc_day(self):
        with patch.object(Store, "today", return_value="2026-11-01"):
            self.store.reserve_request(1)
            with self.assertRaises(ValueError):
                self.store.reserve_request(1)
        with patch.object(Store, "today", return_value="2026-11-02"):
            self.assertEqual(self.store.usage(), 0)
            self.store.reserve_request(1)
            self.assertEqual(self.store.usage(), 1)

    def test_provider_errors_count_against_budget(self):
        market = MarketData("key", self.store, lambda *a, **k: {"Information": "quota secret-key"})
        with self.assertRaises(ApiError) as caught:
            market.quote("IBM")
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertEqual(self.store.usage(), 1)

    def test_missing_key_does_not_consume_budget(self):
        with self.assertRaises(ValueError):
            MarketData("", self.store).quote("IBM")
        self.assertEqual(self.store.usage(), 0)

    def test_invalid_quote_payloads_are_never_cached(self):
        for field, value in (
            ("05. price", "NaN"),
            ("05. price", "-1"),
            ("10. change percent", "inf"),
            ("07. latest trading day", "yesterday"),
            ("01. symbol", "WRONG"),
        ):
            data = json.loads(json.dumps(QUOTE))
            data["Global Quote"][field] = value
            market = MarketData("key", self.store, lambda *a, **k: data)
            with self.subTest(field=field), self.assertRaises(ApiError):
                market.quote("IBM")
            self.assertIsNone(self.store.cached_quote("IBM", 60))

    def test_duplicate_update_does_not_repeat_quote(self):
        update = self.update("/quote IBM", update_id=10)
        self.bot.process(update)
        reopened = Bot(self.config, Store(self.store.path), self.telegram, self.market)
        reopened.process(update)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(self.store.state("telegram_offset"), "11")

    def test_delivery_failure_does_not_replay_side_effect(self):
        with patch.object(self.telegram, "send", side_effect=ApiError("timeout")):
            with self.assertRaises(ApiError):
                self.bot.process(self.update("/add IBM", update_id=11))
        self.assertEqual(self.store.state("telegram_offset"), "12")
        self.assertEqual(self.store.watchlist(), [("IBM", 2)])

    def test_health_requires_recent_successful_poll(self):
        with patch.dict(os.environ, {"DATABASE_PATH": str(self.store.path)}):
            self.assertFalse(health())
            self.store.set_state("heartbeat", str(time.time()))
            self.assertTrue(health())
            self.store.set_state("heartbeat", str(time.time() - 181))
            self.assertFalse(health())

    def test_health_does_not_create_database(self):
        missing = self.path / "missing.db"
        with patch.dict(os.environ, {"DATABASE_PATH": str(missing)}):
            self.assertFalse(health())
        self.assertFalse(missing.exists())

    def test_credentials_are_atomic_private_and_loadable(self):
        env = self.path / ".env"
        write_credentials(env, TOKEN, 123, "key")
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        with patch.dict(os.environ, {}, clear=True):
            loaded = Config.load(env)
        self.assertEqual(loaded.owner_id, 123)
        self.assertEqual(loaded.token, TOKEN)
        self.assertEqual(list(self.path.glob(".env-setup-*")), [])

    def test_missing_owner_and_group_chat_are_rejected(self):
        for owner in ("", "-123", "0", "wrong"):
            env = self.path / ".env"
            env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={owner}\n")
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                Config.load(env)

    def test_environment_overrides_file(self):
        env = self.path / ".env"
        write_credentials(env, TOKEN, 123, "file-key")
        with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "env-key"}, clear=True):
            self.assertEqual(Config.load(env).api_key, "env-key")

    def test_environment_parser_never_executes_shell(self):
        env = self.path / ".env"
        env.write_text('ALPHA_VANTAGE_API_KEY="$(touch should-not-exist)"\n')
        self.assertEqual(read_env(env)["ALPHA_VANTAGE_API_KEY"], "$(touch should-not-exist)")

    def test_pairing_rejects_wrong_sender_then_saves_correct_owner(self):
        env = self.path / ".env"
        env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nALPHA_VANTAGE_API_KEY=key\n")
        fake = FakeTelegram()
        fake.call = lambda method: {"username": "test_bot"} if method == "getMe" else {"url": ""}
        fake.updates = lambda offset, **kw: (
            [self.update("/pair challenge", user=999), self.update("/pair challenge", update_id=2)]
            if offset == 0
            else []
        )
        with (
            patch("stock_agent.setup.Telegram", return_value=fake),
            patch("stock_agent.setup.secrets.token_hex", return_value="challenge"),
            patch.dict(os.environ, {}, clear=True),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            setup(env)
        self.assertEqual(read_env(env)["TELEGRAM_CHAT_ID"], "123")
        self.assertEqual(fake.messages[0][0], 123)


class TransportTests(unittest.TestCase):
    def test_network_error_does_not_expose_url_or_key(self):
        with patch("stock_agent.api.urlopen", side_effect=URLError("https://secret-token/")):
            with self.assertRaises(ApiError) as caught:
                request_json("https://example.com/secret-token")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_http_error_is_sanitized(self):
        error = HTTPError(
            "https://secret-token/", 401, "secret-token", {}, io.BytesIO(b"secret-token")
        )
        with patch("stock_agent.api.urlopen", side_effect=error):
            with self.assertRaises(ApiError) as caught:
                request_json("https://example.com/")
        self.assertEqual(caught.exception.code, 401)
        self.assertNotIn("secret-token", str(caught.exception))

    def test_rate_limit_preserves_retry_after(self):
        body = io.BytesIO(b'{"parameters":{"retry_after":42}}')
        error = HTTPError("https://secret/", 429, "secret", {}, body)
        with patch("stock_agent.api.urlopen", side_effect=error):
            with self.assertRaises(ApiError) as caught:
                request_json("https://example.com/")
        self.assertEqual(caught.exception.retry_after, 42)

    def test_telegram_error_body_is_not_exposed(self):
        telegram = Telegram(
            TOKEN, lambda *a, **k: {"ok": False, "error_code": 409, "description": "secret-token"}
        )
        with self.assertRaises(ApiError) as caught:
            telegram.updates(0)
        self.assertEqual(caught.exception.code, 409)
        self.assertNotIn("secret-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
