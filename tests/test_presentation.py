"""Safe Telegram styling at the real sender/outbox boundary, using fixtures."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trabe.api import ApiError, Telegram
from trabe.outbox import Outbox
from trabe.presentation import message_entities
from trabe.store import Store


def styled_text(text, entity):
    raw = text.encode("utf-16-le")
    start = entity["offset"] * 2
    return raw[start : start + entity["length"] * 2].decode("utf-16-le")


class PresentationTests(unittest.TestCase):
    def test_headings_sentiment_and_notes_have_valid_unicode_entities(self):
        text = (
            "📰 News sentiment\n\n🟢 GOOGL • Bullish (+0.5) • 1h ago\n"
            "Revenue & profits <b>rose</b>.\nSentiment is heuristic, not a price forecast."
        )
        spans = [(entity["type"], styled_text(text, entity)) for entity in message_entities(text)]
        self.assertIn(("bold", "📰 News sentiment"), spans)
        self.assertIn(("bold", "GOOGL"), spans)
        self.assertIn(("bold", "Bullish (+0.5)"), spans)
        self.assertIn(("italic", "Sentiment is heuristic, not a price forecast."), spans)
        self.assertNotIn(("bold", "rose"), spans)

    def test_portfolio_values_and_commands_are_readable_without_markup_parsing(self):
        text = "Portfolio • EUR\n\nVST • 5 shares\nUnrealized P/L: +12.34 EUR\nMore: /position VST\nTry /portfolio"
        spans = [(entity["type"], styled_text(text, entity)) for entity in message_entities(text)]
        self.assertIn(("bold", "Portfolio • EUR"), spans)
        self.assertIn(("bold", "VST"), spans)
        self.assertIn(("bold", "Unrealized P/L"), spans)
        self.assertIn(("code", "/portfolio"), spans)
        previous_end = 0
        for entity in message_entities(text):
            self.assertGreaterEqual(entity["offset"], previous_end)
            previous_end = entity["offset"] + entity["length"]

    def test_persisted_reply_retries_with_the_same_safe_formatting_and_keyboard(self):
        payloads = []

        def transport(url, payload, **kwargs):
            payloads.append(payload)
            if len(payloads) == 1:
                raise ApiError("fixture outage")
            return {"ok": True, "result": {"message_id": 1}}

        text = "News\n🟢 VST • Bullish\nLiteral <b>tags</b> & *stars*\nSentiment is heuristic."
        keyboard = {"keyboard": [["News"]]}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "app.db")
            Outbox(store).send(123, text, reply_markup=keyboard)
            box = Outbox(Store(store.path))
            sender = Telegram("fixture-token", transport)
            with patch("trabe.api.time.sleep"):
                with self.assertRaises(ApiError):
                    box.flush(sender)
                self.assertEqual(box.pending(), 1)
                box.flush(sender)
            self.assertEqual(box.pending(), 0)
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[1]["text"], text)
        self.assertEqual(payloads[1]["reply_markup"], keyboard)
        self.assertNotIn("parse_mode", payloads[1])
        self.assertIn("entities", payloads[1])

    def test_long_messages_keep_chunk_limits_and_rebase_entities_after_emoji(self):
        payloads = []

        def transport(url, payload, **kwargs):
            payloads.append(payload)
            return {"ok": True, "result": True}

        text = "News\n" + "😀" * 2000 + "\nSentiment is heuristic."
        with patch("trabe.api.time.sleep"):
            Telegram("fixture-token", transport).send(123, text)
        self.assertEqual("".join(p["text"] for p in payloads), text)
        self.assertGreater(len(payloads), 1)
        for payload in payloads:
            units = len(payload["text"].encode("utf-16-le")) // 2
            self.assertLessEqual(units, 3500)
            for entity in payload.get("entities", []):
                self.assertLessEqual(entity["offset"] + entity["length"], units)
                styled_text(payload["text"], entity)  # Surrogate boundaries must decode.
