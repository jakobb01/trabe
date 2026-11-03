"""Persist replies before delivery, so an API outage does not rerun a command."""

import json
import time

from stock_agent.api import Telegram, message_chunks
from stock_agent.store import Store


class Outbox:
    def __init__(self, store: Store):
        self.store = store

    def send(self, chat_id: int, text: str, *, reply_markup: dict | None = None):
        chunks = list(message_chunks(text))
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Do not deliver obsolete price summaries after a very long outage.
            db.execute("DELETE FROM outbox WHERE created_at < ?", (time.time() - 86400,))
            count = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            if count + len(chunks) > 500:
                raise ValueError(
                    "Reply queue is full; let pending messages finish before sending more commands."
                )
            for index, chunk in enumerate(chunks):
                markup = json.dumps(reply_markup) if index == 0 and reply_markup else None
                db.execute(
                    "INSERT INTO outbox(chat_id,text,markup,created_at) VALUES (?,?,?,?)",
                    (chat_id, chunk, markup, time.time()),
                )

    def flush(self, telegram: Telegram, limit: int = 5):
        with self.store.connection() as db:
            db.execute("DELETE FROM outbox WHERE created_at < ?", (time.time() - 86400,))
        for _ in range(limit):
            with self.store.connection() as db:
                row = db.execute(
                    "SELECT id,chat_id,text,markup FROM outbox ORDER BY id LIMIT 1"
                ).fetchone()
            if row is None:
                break
            item_id, chat_id, text, markup = row
            kwargs = {"reply_markup": json.loads(markup)} if markup else {}
            telegram.send(chat_id, text, **kwargs)
            # One chunk is acknowledged at a time. A lost HTTP response can still
            # cause a duplicate, because Telegram has no sendMessage idempotency key.
            with self.store.connection() as db:
                db.execute("DELETE FROM outbox WHERE id=?", (item_id,))

    def pending(self) -> int:
        with self.store.connection() as db:
            return db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
