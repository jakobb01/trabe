"""Durable replies, typed coalesced navigation edits and opt-in event delivery."""

import json
import time

from trabe.api import ApiError, Telegram, message_chunks
from trabe.presentation import display_text
from trabe.store import Store


class Outbox:
    def __init__(self, store: Store):
        self.store = store

    def send(self, chat_id, text, *, reply_markup=None, kind="reply", db=None, now=None):
        now = time.time() if now is None else now
        if db is None:
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.send(
                    chat_id, text, reply_markup=reply_markup, kind=kind, db=connection, now=now
                )
        chunks = list(message_chunks(display_text(text)))
        db.execute("DELETE FROM outbox WHERE created_at < ?", (now - 86400,))
        count = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if count + len(chunks) > 500:
            raise ValueError(
                "Reply queue is full; let pending messages finish before sending more commands."
            )
        first_id = None
        for index, chunk in enumerate(chunks):
            markup = json.dumps(reply_markup) if index == 0 and reply_markup else None
            row = db.execute(
                "INSERT INTO outbox(chat_id,text,markup,created_at,kind) VALUES (?,?,?,?,?)",
                (chat_id, chunk, markup, now, kind),
            )
            first_id = first_id or row.lastrowid
        return first_id

    def edit(self, chat_id, message_id, text, *, reply_markup=None):
        text = display_text(text)
        if len(list(message_chunks(text))) != 1:
            raise ValueError("Navigation card exceeds one message.")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM outbox WHERE chat_id=? AND message_id=? AND operation='edit'",
                (chat_id, message_id),
            )
            item_id = self.send(chat_id, text, reply_markup=reply_markup, db=db)
            db.execute(
                "UPDATE outbox SET operation='edit',message_id=? WHERE id=?", (message_id, item_id)
            )
            return item_id

    def flush(self, telegram: Telegram, limit=5, *, now=None):
        fixed = now is not None
        now = time.time() if now is None else now
        from trabe.navigation import bind_markup
        from trabe.news_push import NewsPush

        with self.store.connection() as db:
            NewsPush(self.store, 0).expire(now, db)
            db.execute("DELETE FROM outbox WHERE created_at < ?", (now - 86400,))
            if self.store.automation_paused(now):
                db.execute("DELETE FROM outbox WHERE kind!='reply'")
            if self.store.settings()["monitoring"] == "off":
                db.execute("DELETE FROM outbox WHERE kind='alert'")
            rows = db.execute(
                "SELECT id,chat_id,text,markup,kind,operation,message_id,attempts,fallback FROM outbox WHERE retry_at<=? ORDER BY id LIMIT 500",
                (now,),
            ).fetchall()
        sent = 0
        for item_id, chat_id, text, markup, kind, operation, message_id, attempts, fallback in rows:
            if sent >= limit:
                break
            now = now if fixed else time.time()
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                # A setting change or newer edit may already have removed this row.
                if not db.execute("SELECT 1 FROM outbox WHERE id=?", (item_id,)).fetchone():
                    continue
                if kind == "news_event":
                    policy = NewsPush.flush_policy(self.store, db, item_id, now)
                    if policy != "send":
                        continue
                elif (
                    (kind != "reply" and self.store.automation_paused(now))
                    or (kind == "alert" and self.store.settings(db)["monitoring"] != "on")
                    or (
                        kind in ("earnings", "insiders", "news", "briefing", "outlook")
                        and self.store.settings(db).get(kind + "_enabled") != "on"
                    )
                ):
                    db.execute("DELETE FROM outbox WHERE id=?", (item_id,))
                    continue
            keyboard = json.loads(markup) if markup else None
            kwargs = {"reply_markup": keyboard} if keyboard else {}
            try:
                result = (
                    telegram.edit(chat_id, message_id, text, **kwargs)
                    if operation == "edit"
                    else telegram.send(chat_id, text, **kwargs)
                )
            except ApiError as error:
                if operation != "edit" and not fallback:
                    raise
                with self.store.connection() as db:
                    if error.reason == "not_modified":
                        result = {"message_id": message_id}
                    elif error.reason == "uneditable" and not fallback:
                        db.execute(
                            "UPDATE outbox SET operation='send',fallback=1,message_id=NULL WHERE id=?",
                            (item_id,),
                        )
                        continue
                    elif attempts >= 4:
                        db.execute("DELETE FROM outbox WHERE id=?", (item_id,))
                        continue
                    else:
                        db.execute(
                            "UPDATE outbox SET attempts=attempts+1,retry_at=? WHERE id=?",
                            (now + max(error.retry_after, min(300, 2 ** (attempts + 1))), item_id),
                        )
                        continue
            # No write transaction spans HTTP. A lost response can still duplicate a send.
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                    bind_markup(db, keyboard, chat_id, result["message_id"], rebind=bool(fallback))
                db.execute(
                    "UPDATE event_deliveries SET status='delivered',reason='' WHERE outbox_id=? AND status='queued'",
                    (item_id,),
                )
                db.execute("DELETE FROM outbox WHERE id=?", (item_id,))
            sent += 1

    def pending(self):
        with self.store.connection() as db:
            return db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
