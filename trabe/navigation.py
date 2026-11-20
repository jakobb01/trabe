"""Owner-bound opaque UI sessions. All reads are cached; mutations explicitly set values."""

import secrets
import time

from trabe.advisor import Advisor
from trabe.news_digest import NewsDigest, units
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
    "news_companies",
    "advisor_companies",
    "positions",
    "company",
    "news",
    "sources",
    "earlier_news",
    "refresh",
    "research",
    "advisor",
    "advisor_preview",
    "advisor_evidence",
    "advisor_results",
    "advisor_compare",
    "advisor_status",
    "earnings",
    "insiders",
    "portfolio",
    "position",
    "trades",
    "reports",
    "market_reports",
    "cached_prices",
    "alerts",
    "last_scan",
    "latest_snapshot",
    "outlook",
    "outlook_results",
    "paper",
    "paper_results",
    "paper_decisions",
    "paper_fills",
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
COMPANIES_PER_PAGE = 8
COMPANY_PICKERS = {
    "companies": ("Companies • held and watched", "company", None),
    "news_companies": ("News • choose a company", "news", ("All news", "news")),
    "advisor_companies": ("Advisor • choose a company", "research", ("Advisor", "advisor")),
    "positions": ("Portfolio • open positions", "position", ("Portfolio", "portfolio")),
}
ADVISOR_VIEWS = {
    "research": "cached",
    "advisor_preview": "preview",
    "advisor_evidence": "evidence",
    "advisor_results": "results",
    "advisor_compare": "compare",
    "advisor_status": "shadow",
}
PAPER_VIEWS = {
    "paper": "status",
    "paper_results": "results",
    "paper_decisions": "decisions",
    "paper_fills": "trades",
}
MARKET_VIEWS = {
    "cached_prices": ("Cached prices", "prices", ()),
    "alerts": ("Alerts", "alerts", ()),
    "last_scan": ("Last scan", "lastscan", ()),
    "latest_snapshot": ("Latest snapshot", "snapshot", ("latest",)),
}


def page_buttons(button, action, page, count, **scope):
    row = []
    if page > 0:
        row.append(button("Previous", action, page=page - 1, **scope))
    if page + 1 < count:
        row.append(button("Next", action, page=page + 1, **scope))
    return row


def bounded_card(text, command):
    text = display_text(text)
    if units(text) <= 3400:
        return text
    footer = f"\n\nFull report: {command}"
    limit = 3400 - units(footer)
    lines = []
    used = 0
    for line in text.split("\n"):
        size = units(line) + bool(lines)
        if used + size > limit:
            break
        lines.append(line)
        used += size
    return "\n".join(lines).rstrip() + footer


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
        if action not in ACTIONS or page < 0:
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

    def render(self, action="home", ticker="", page=0, *, value="", message_id=None):
        now = time.time()
        rows = []
        fallback = "/home"

        def button(label, target, **kwargs):
            return self.button(label, target, message_id=message_id, **kwargs)

        symbols = sorted(self.bot.ledger.coverage())
        if ticker and ticker not in symbols:
            return "This company is no longer held or watched.", {
                "inline_keyboard": [[button("Home", "home")]]
            }
        if action == "home":
            text = "trabe\nYour cached market briefing, company developments and portfolio."
            rows = [
                [button("Today", "today"), button("Companies", "companies")],
                [button("News", "news"), button("Advisor", "advisor")],
                [button("Portfolio", "portfolio"), button("Reports", "reports")],
                [button("Settings", "settings")],
            ]
        elif action == "today":
            text = self.bot.insights.render_briefing(symbols, now)
            rows = [[button("News", "news"), button("Market outlook", "outlook", value="today")]]
            fallback = "/briefing details"
        elif action in COMPANY_PICKERS:
            title, target, back = COMPANY_PICKERS[action]
            if action == "positions":
                symbols = sorted(self.bot.ledger.held())
            text, rows = self.company_picker(button, symbols, action, title, target, back, page)
        elif action == "company":
            text = f"{ticker} • cached company reports"
            rows = [
                [
                    button("News", "news", ticker=ticker),
                    button("Research", "research", ticker=ticker, value="company"),
                ],
                [
                    button("Earnings", "earnings", ticker=ticker),
                    button("Insiders", "insiders", ticker=ticker),
                ],
                [
                    button(
                        "Companies", "companies", page=symbols.index(ticker) // COMPANIES_PER_PAGE
                    )
                ],
            ]
        elif action in ("news", "sources", "refresh", "earlier_news"):
            text, rows = self.news_view(button, symbols, now, action, ticker, page, value)
            fallback = "/news sources" if action == "sources" else "/news cached"
            if ticker and action != "sources":
                fallback += f" {ticker}"
        elif action == "advisor":
            text = (
                "Advisor • five-session company forecasts\n"
                "View saved research, check a preview, or review forecast performance.\n"
                "To save a forecast, use /advisor TICKER."
            )
            rows = [
                [button("Choose company", "advisor_companies")],
                [
                    button("All results", "advisor_results"),
                    button("Compare methods", "advisor_compare"),
                ],
                [button("Sampling status", "advisor_status")],
            ]
        elif action in ADVISOR_VIEWS:
            text, rows, fallback = self.advisor_view(button, symbols, action, ticker, value)
        elif action in ("earnings", "insiders"):
            text = self.bot.insights.feature(action).report([ticker], now=now)
            rows = [[button(ticker + " reports", "company", ticker=ticker)]]
            fallback = f"/{action} cached {ticker}"
        elif action == "portfolio":
            text = self.bot.portfolio.report(now=now)
            rows = [
                [
                    button("Positions & transactions", "positions"),
                    button("Paper experiment", "paper", value="portfolio"),
                ]
            ]
            fallback = "/portfolio"
        elif action in ("position", "trades"):
            fallback = f"/{action} {ticker}"
            text = (
                self.bot.portfolio.report(ticker, now=now)
                if action == "position"
                else self.bot.command("trades", [ticker])
            )
            rows = [
                [
                    button("Transactions", "trades", ticker=ticker)
                    if action == "position"
                    else button("Position", "position", ticker=ticker),
                    button("Positions", "positions"),
                ]
            ]
        elif action == "reports":
            text = "Reports • cached market and paper data"
            rows = [
                [
                    button("Market & monitoring", "market_reports"),
                    button("Paper experiment", "paper"),
                ],
            ]
        elif action == "market_reports":
            text = "Market & monitoring • cached reports"
            rows = [
                [button("Market outlook", "outlook", value="market_reports")],
                [button(MARKET_VIEWS[target][0], target) for target in ("cached_prices", "alerts")],
                [
                    button(MARKET_VIEWS[target][0], target)
                    for target in ("last_scan", "latest_snapshot")
                ],
                [button("Reports", "reports")],
            ]
        elif action in MARKET_VIEWS:
            _, command, args = MARKET_VIEWS[action]
            text = self.bot.command(command, list(args))
            fallback = "/" + " ".join((command, *args))
            rows = [[button("Market & monitoring", "market_reports")]]
        elif action in ("outlook", "outlook_results"):
            option = "cached" if action == "outlook" else "results"
            text = self.bot.command("outlook", [option])
            fallback = f"/outlook {option}"
            rows = [
                [
                    button("Outlook results", "outlook_results", value=value)
                    if action == "outlook"
                    else button("Saved outlook", "outlook", value=value),
                    button("Today", "today")
                    if value == "today"
                    else button("Market & monitoring", "market_reports"),
                ]
            ]
        elif action in PAPER_VIEWS:
            option = PAPER_VIEWS[action]
            text = self.bot.command("paper", [option])
            fallback = f"/paper {option}"
            tabs = (
                ("Paper status", "paper"),
                ("Paper results", "paper_results"),
                ("Latest decisions", "paper_decisions"),
                ("Recent fills", "paper_fills"),
            )
            buttons = [
                button(label, target, value=value) for label, target in tabs if action != target
            ]
            rows = [buttons[:2], buttons[2:]]
            rows[-1].append(
                button("Portfolio", "portfolio")
                if value == "portfolio"
                else button("Reports", "reports")
            )
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
        else:
            text = "Open /home again."
        if action != "home":
            rows.append([button("Home", "home")])
        return bounded_card(text, fallback), {"inline_keyboard": rows}

    @staticmethod
    def company_picker(button, symbols, action, title, target, back, page):
        count = (len(symbols) + COMPANIES_PER_PAGE - 1) // COMPANIES_PER_PAGE
        page = min(page, max(0, count - 1))
        offset = page * COMPANIES_PER_PAGE
        selected = symbols[offset : offset + COMPANIES_PER_PAGE]
        text = title
        if selected:
            text += f"\nSelect a company • page {page + 1}/{count}"
        elif action == "positions":
            text += "\nNo open positions. For past transactions, use /trades TICKER."
        else:
            text += "\nNo held or watched companies. Add one with /add TICKER."
        rows = [[button(s, target, ticker=s, value=action)] for s in selected]
        paging = page_buttons(button, action, page, count)
        if paging:
            rows.append(paging)
        if back:
            rows.append([button(*back)])
        return text, rows

    def advisor_view(self, button, symbols, action, ticker, origin):
        option = ADVISOR_VIEWS[action]
        args = [option, ticker] if ticker else [option]
        text = self.bot.command("advisor", args)
        fallback = "/advisor " + " ".join(args)
        if not ticker:
            return (
                text,
                [[button("Advisor", "advisor"), button("Choose company", "advisor_companies")]],
                fallback,
            )

        scope = {"ticker": ticker, "value": origin}
        tabs = (
            ("Saved research", "research"),
            ("Preview", "advisor_preview"),
            ("Results", "advisor_results"),
            ("Compare", "advisor_compare"),
        )
        buttons = [button(label, target, **scope) for label, target in tabs if action != target]
        rows = [buttons[:2], buttons[2:]]
        if action == "research" and Advisor(self.store).get(ticker):
            rows[-1].append(button("Saved evidence", "advisor_evidence", **scope))
        back = (
            button(ticker + " reports", "company", ticker=ticker)
            if origin in {"", "company"}
            else button(
                "Advisor companies",
                "advisor_companies",
                page=symbols.index(ticker) // COMPANIES_PER_PAGE,
            )
        )
        rows.append([back, button("Advisor", "advisor")])
        return text, rows, fallback

    def news_view(self, button, symbols, now, action, ticker, page, value):
        value = value or ("company" if ticker else "all")
        origin, _, latest_page = value.partition(":")
        scope = {"ticker": ticker, "value": value}
        selected = [ticker] if ticker else symbols
        title = f"News • {ticker or 'All companies'}"
        digest = NewsDigest(self.store)
        if action == "sources":
            text = (
                title
                + " • source coverage\n\n"
                + "\n\n".join(digest.repo.coverage_lines(selected, now))
            )
            return text, [[button("Back to news", "news", page=page, **scope)]]

        earlier = action == "earlier_news"
        text, page, count = digest.inline_page(selected, now, page, earlier=earlier)
        title += " • Earlier coverage (3–7 days)" if earlier else " • Latest cached"
        if not selected:
            text = "No held or watched companies. Add one with /add TICKER."
        if count:
            title += f" • {page + 1}/{count}"
        if action == "refresh":
            text = self.bot.insights.queue("news", selected) + "\n\n" + text
        rows = []
        paging = page_buttons(button, "earlier_news" if earlier else "news", page, count, **scope)
        if paging:
            rows.append(paging)
        if earlier:
            rows.append(
                [
                    button(
                        "Back to news",
                        "news",
                        ticker=ticker,
                        page=int(latest_page or 0),
                        value=origin,
                    )
                ]
            )
        else:
            if selected:
                rows.extend(
                    [
                        [
                            button("Sources", "sources", page=page, **scope),
                            button("Refresh news", "refresh", page=page, **scope),
                        ],
                        [
                            button(
                                "Earlier coverage",
                                "earlier_news",
                                ticker=ticker,
                                value=f"{origin}:{page}",
                            )
                        ],
                    ]
                )
            scope_buttons = [button("Choose company", "news_companies")]
            if ticker:
                scope_buttons.insert(
                    0,
                    button(ticker + " reports", "company", ticker=ticker)
                    if origin != "news_companies"
                    else button("All news", "news"),
                )
            rows.append(scope_buttons)
        return title + "\n\n" + text, rows

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
        if action == "news" and not value:
            # Older More buttons stored a three-card group index and no scope marker.
            page *= 3
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
        text, markup = self.render(action, ticker, page, value=value, message_id=message_id)
        self.bot.telegram.edit(self.bot.config.owner_id, message_id, text, reply_markup=markup)
