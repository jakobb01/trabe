"""Owner-bound opaque UI sessions. All reads are cached; mutations explicitly set values."""

import secrets
import time

from trabe.ledger import Ledger
from trabe.news_digest import NewsDigest
from trabe.presentation import display_text

UI_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS ui_tokens(
        token TEXT PRIMARY KEY,owner INTEGER NOT NULL,action TEXT NOT NULL,
        ticker TEXT NOT NULL,page INTEGER NOT NULL,value TEXT NOT NULL,
        expires REAL NOT NULL,message_id INTEGER,used INTEGER NOT NULL DEFAULT 0)""",
)
ACTIONS = {
    "home",
    "today",
    "companies",
    "company",
    "news",
    "sources",
    "refresh",
    "research",
    "earnings",
    "insiders",
    "portfolio",
    "settings",
    "set",
    "help",
}
SETTINGS = {
    "news_enabled",
    "news_alerts_enabled",
    "news_quiet_enabled",
    "briefing_enabled",
    "earnings_enabled",
    "insiders_enabled",
}


def bind_markup(db, markup, owner, message_id, *, rebind=False):
    for row in (markup or {}).get("inline_keyboard", []):
        for button in row:
            data = button.get("callback_data", "")
            if isinstance(data, str) and data.startswith("n:"):
                db.execute(
                    "UPDATE ui_tokens SET message_id=? WHERE token=? AND owner=?"
                    + ("" if rebind else " AND message_id IS NULL"),
                    (message_id, data[2:], owner),
                )


def prune_tokens(db, now):
    db.execute("DELETE FROM ui_tokens WHERE expires<=?", (now,))
    db.execute(
        "DELETE FROM ui_tokens WHERE token NOT IN (SELECT token FROM ui_tokens ORDER BY expires DESC,rowid DESC LIMIT 256)"
    )


class Navigation:
    def __init__(self, bot, responder=None):
        self.bot, self.store, self.responder = bot, bot.store, responder

    def button(self, label, action, *, ticker="", page=0, value="", message_id=None):
        if action not in ACTIONS or not 0 <= page <= 50:
            raise ValueError("Invalid navigation action.")
        token = secrets.token_urlsafe(18)
        now = time.time()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO ui_tokens(token,owner,action,ticker,page,value,expires,message_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    token,
                    self.bot.config.owner_id,
                    action,
                    ticker,
                    page,
                    value,
                    now + 86400,
                    message_id,
                ),
            )
            prune_tokens(db, now)
        return {"text": label, "callback_data": "n:" + token}

    def render(self, action="home", ticker="", page=0, *, message_id=None):
        now = time.time()
        rows = []

        def button(label, target, **kwargs):
            return self.button(label, target, message_id=message_id, **kwargs)

        symbols = sorted(Ledger(self.store).coverage())
        if ticker and ticker not in symbols:
            return "This company is no longer held or watched. Open /home again.", {
                "inline_keyboard": [[button("Home", "home")]]
            }
        if action == "home":
            text = "trabe\nYour cached market briefing, company developments and portfolio."
            rows = [
                [button("Today", "today"), button("Companies", "companies")],
                [button("Portfolio", "portfolio"), button("Settings", "settings")],
            ]
        elif action == "today":
            text = self.bot.insights.render_briefing(symbols, now)
            rows = [[button("Companies", "companies"), button("Refresh news", "refresh")]]
        elif action == "companies":
            text = "Companies • held and watched\nSelect a company for cached reports."
            selected = symbols[page * 8 : page * 8 + 8]
            rows = [[button(s, "company", ticker=s)] for s in selected]
            if not selected:
                text += "\nNo companies here. Add one with /add TICKER."
            paging = []
            if page:
                paging.append(button("Previous", "companies", page=page - 1))
            if len(symbols) > (page + 1) * 8:
                paging.append(button("More", "companies", page=page + 1))
            if paging:
                rows.append(paging)
        elif action == "company":
            text = f"{ticker} • cached company reports"
            rows = [
                [
                    button("News", "news", ticker=ticker),
                    button("Research", "research", ticker=ticker),
                ],
                [
                    button("Earnings", "earnings", ticker=ticker),
                    button("Insiders", "insiders", ticker=ticker),
                ],
            ]
        elif action in ("news", "sources"):
            text = NewsDigest(self.store).report(
                [ticker] if ticker else symbols, now, page=page, sources=action == "sources"
            )
            rows = [
                [
                    button("Sources", "sources", ticker=ticker),
                    button("More", "news", ticker=ticker, page=min(50, page + 1)),
                    button("Refresh", "refresh", ticker=ticker),
                ]
            ]
        elif action == "research":
            from trabe.advisor import Advisor

            text = Advisor(self.store).handle(["cached", ticker])
            text += (
                "\nResearch uses its saved legacy news baseline. To create research explicitly: /advisor "
                + ticker
            )
        elif action in ("earnings", "insiders"):
            text = self.bot.insights.feature(action).report([ticker], now=now)
        elif action == "portfolio":
            text = self.bot.portfolio.report(now=now)
        elif action == "settings":
            settings = self.store.settings()
            text = "Settings\n" + "\n".join(f"{key}: {settings[key]}" for key in sorted(SETTINGS))
            text += f"\nNews cap: {settings['news_alert_daily_cap']}/day and rolling 24h\nQuiet: {settings['news_quiet_start']}–{settings['news_quiet_end']} ({settings['timezone']})\nAdvanced preferences: /settings"
            for key in sorted(SETTINGS):
                label = key.replace("_enabled", "").replace("_", " ")
                rows.append(
                    [
                        button(label + " on", "set", value=key + ":on"),
                        button(label + " off", "set", value=key + ":off"),
                    ]
                )
        elif action == "help":
            text = "Everyday: /home /briefing cached /news cached /portfolio /status\nAdvanced commands: /help advanced"
        elif action == "refresh":
            text = self.bot.insights.queue("news", [ticker] if ticker else None)
            text += "\nCached status while refresh runs:\n" + NewsDigest(self.store).report(
                [ticker] if ticker else symbols, now
            )
            rows = [[button("Cached news", "news", ticker=ticker)]]
        else:
            text = "Open /home again."
        if action != "home":
            rows.append([button("Home", "home"), button("Companies", "companies")])
        text = display_text(text)
        # Long cached reports omit whole paragraphs/lines with an explicit notice.
        if len(text.encode("utf-16-le")) // 2 > 3400:
            selected = []
            for section in text.split("\n\n"):
                if (
                    len(
                        (
                            "\n\n".join([*selected, section]) + "\nMore: use the company command."
                        ).encode("utf-16-le")
                    )
                    // 2
                    <= 3400
                ):
                    selected.append(section)
            text = "\n\n".join(selected) + "\nMore: use the company command."
        return text, {"inline_keyboard": rows}

    def callback(self, query):
        message = query.get("message")
        sender = query.get("from", {})
        if not isinstance(message, dict) or not self.responder:
            return
        chat = message.get("chat", {})
        author = message.get("from", {})
        message_id = message.get("message_id")
        if (
            sender.get("id") != self.bot.config.owner_id
            or sender.get("is_bot", False)
            or chat.get("id") != self.bot.config.owner_id
            or chat.get("type") != "private"
            or author.get("id") != self.bot.bot_id
            or not author.get("is_bot")
            or type(self.bot.bot_id) is not int
            or type(message.get("date")) is not int
            or message["date"] <= 0
            or type(message_id) is not int
            or message_id <= 0
            or not isinstance(query.get("id"), str)
        ):
            return
        data = query.get("data")
        now = time.time()
        valid = isinstance(data, str) and data.startswith("n:") and len(data.encode()) <= 64
        with self.store.connection() as db:
            row = db.execute(
                "SELECT owner,action,ticker,page,value,expires,message_id,used FROM ui_tokens WHERE token=?",
                (data[2:] if valid else "",),
            ).fetchone()
        if (
            not row
            or row[0] != self.bot.config.owner_id
            or row[5] <= now
            or row[6] != message_id
            or row[7]
            or row[1] not in ACTIONS
        ):
            self.responder.answer_callback(query["id"], "Open /home again")
            return
        self.responder.answer_callback(query["id"])
        _, action, ticker, page, value, _, _, _ = row
        if action == "set":
            key, _, setting = value.partition(":")
            if key not in SETTINGS or setting not in {"on", "off"}:
                return
            try:
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    consumed = db.execute(
                        "UPDATE ui_tokens SET used=1 WHERE token=? AND used=0 AND expires>? AND owner=? AND message_id=?",
                        (data[2:], time.time(), self.bot.config.owner_id, message_id),
                    ).rowcount
                    if not consumed:
                        return
                    if key in {"news_enabled", "earnings_enabled", "insiders_enabled"}:
                        self.bot.insights.apply_automation(
                            db, key.removesuffix("_enabled"), setting
                        )
                    else:
                        self.store.apply_setting(db, key, setting)
                    db.execute(
                        "INSERT INTO state VALUES ('ui_mutation_generation','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
                    )
            except ValueError as error:
                text, markup = self.render("settings", message_id=message_id)
                self.bot.telegram.edit(
                    self.bot.config.owner_id,
                    message_id,
                    str(error) + "\n\n" + text,
                    reply_markup=markup,
                )
                return
            self.bot.insights.wake.set()
            action = "settings"
        text, markup = self.render(action, ticker, page, message_id=message_id)
        self.bot.telegram.edit(self.bot.config.owner_id, message_id, text, reply_markup=markup)
