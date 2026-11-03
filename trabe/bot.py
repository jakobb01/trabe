"""Owner-only commands. No scheduled trading logic runs in this first release."""

import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from trabe import __version__
from trabe.api import ApiError, MarketData, Telegram
from trabe.config import Config
from trabe.controls import Controls, intraday_text
from trabe.preferences import DESCRIPTIONS
from trabe.scans import Scans
from trabe.store import Store

logger = logging.getLogger(__name__)

COMMANDS = [
    ("menu", "Open the control menu"),
    ("help", "Show available commands"),
    ("status", "Service health and API budget"),
    ("watchlist", "Show watched symbols"),
    ("add", "Add symbol: /add IBM 1"),
    ("remove", "Remove symbol: /remove IBM"),
    ("quote", "Market API check: /quote IBM"),
    ("settings", "Show current preferences"),
    ("set", "Change a preference"),
    ("test", "Confirm Telegram delivery"),
    ("prices", "View cached watchlist prices without using API quota"),
    ("scan", "Check watchlist daily changes in the background"),
    ("lastscan", "Show the most recent scan result"),
    ("schedule", "Set daily scan time: /schedule 23:00 or off"),
    ("provider", "Show/select the configured market provider"),
    ("monitor", "Enable/disable intraday monitoring"),
    ("alerts", "Show thresholds and recent alerts"),
    ("alert", "Set per-stock rule: /alert VST -5"),
    ("pause", "Pause automation: /pause 24h"),
    ("resume", "Resume saved automation"),
    ("history", "Daily summaries: /history VST 5"),
    ("storage", "Storage and retention health"),
]

MENU = {
    "keyboard": [
        ["Status", "Watchlist"],
        ["Cached prices", "Scan watchlist"],
        ["Settings", "Help"],
        ["Monitor on", "Monitor off"],
        ["Alerts", "Storage"],
        ["Pause 24h", "Resume"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Choose an action, or type /help",
}
BUTTONS = {
    "Status": "/status",
    "Watchlist": "/watchlist",
    "Cached prices": "/prices",
    "Scan watchlist": "/scan",
    "Settings": "/settings",
    "Help": "/help",
    "Monitor on": "/monitor on",
    "Monitor off": "/monitor off",
    "Alerts": "/alerts",
    "Storage": "/storage",
    "Pause 24h": "/pause 24h",
    "Resume": "/resume",
}

HELP = """trabe
/menu — show the control buttons
/status — service health (uses no market API requests)
/test — confirm Telegram delivery
/watchlist — show your watchlist
/add TICKER [1|2|3] — add/update a symbol (default tier 2)
/remove TICKER — remove a symbol
/quote TICKER — request a quote; cached where possible
/prices — cached watchlist prices; no API requests
/scan [TICKER] — background daily-change check
/lastscan — recover the latest scan summary
/provider [finnhub|alphavantage] — show/select a source
/monitor on|off — intraday collection and alerts
/alerts — thresholds, pause state and recent alerts
/alert VST -5|default|off — set a stock's rule
/pause 24h, /resume — pause/resume automation
/history VST 5 — retained daily observed summaries
/storage — local data, backups and maintenance
/schedule HH:MM — opt into a weekday daily summary
/schedule off — turn off scheduled checks
/settings — show preferences
/set timezone Europe/Ljubljana
/set quote_cache_minutes 60
/set api_daily_budget 20
/set dip_threshold_percent -4

Settings and watchlist survive restarts. Symbol syntax is checked on /add; availability is checked on /quote.
Prices show their source and source timestamp. Alpha Vantage is end-of-day; Finnhub supplies the intraday feed. Monitoring starts off until enabled. Earnings, insider tracking and AI briefings will follow later."""


def symbol(value: str) -> str:
    value = value.upper()
    if len(value) > 20 or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*", value):
        raise ValueError("Use a ticker such as IBM, BRK.B or VOD.LON (up to 20 characters).")
    return value


class Bot:
    def __init__(
        self, config: Config, store: Store, telegram: Telegram, market: MarketData, *, monitor=None
    ):
        self.config, self.store, self.telegram, self.market = config, store, telegram, market
        self.started = time.monotonic()
        self.username = ""
        self.scans = Scans(store, market)
        self.controls = Controls(config, store, market, monitor)

    def handle(self, update: dict):
        message = update.get("message", {})
        chat, sender = message.get("chat", {}), message.get("from", {})
        # A chat ID alone is insufficient for groups; both identities and private
        # chat type must match. Unauthorized senders receive no response.
        if (
            chat.get("type") != "private"
            or chat.get("id") != self.config.owner_id
            or sender.get("id") != self.config.owner_id
            or sender.get("is_bot", False)
        ):
            return
        text = message.get("text", "")
        if not isinstance(text, str):
            return
        text = BUTTONS.get(text, text)
        if not text.startswith("/"):
            self.telegram.send(self.config.owner_id, "Choose a control button or use /help.")
            return
        parts = text.split()
        command, _, target = parts[0][1:].partition("@")
        if target and target.lower() != self.username.lower():
            return
        try:
            reply = self.command(command.lower(), parts[1:])
        except (ValueError, ApiError) as error:
            reply = str(error)  # Only our sanitized errors reach this boundary.
        if command.lower() in ("start", "help", "menu"):
            self.telegram.send(self.config.owner_id, reply, reply_markup=MENU)
        else:
            self.telegram.send(self.config.owner_id, reply)

    def command(self, command: str, args: list[str]) -> str:
        extra = self.controls.handle(command, args)
        if extra is not None:
            return extra
        if command in ("start", "help", "menu"):
            return HELP
        if command == "test":
            return "✅ Telegram delivery works. This response came from trabe."
        if command == "status":
            settings = self.store.settings()
            now = datetime.now(ZoneInfo(settings["timezone"])).strftime("%Y-%m-%d %H:%M %Z")
            provider = "configured" if self.config.api_key else "missing key"
            last_success = self.store.state("last_api_success", "not checked yet")
            scan = self.store.latest_scan()
            scan_status = f"#{scan[0]} {scan[1]}" if scan else "not run yet"
            return (
                f"trabe {__version__} • running\n{now}\n"
                f"Uptime: {int(time.monotonic() - self.started)} seconds\n"
                f"Watchlist: {len(self.store.watchlist())} symbols\n"
                f"Alpha Vantage: {provider}\nLast quote trading day: {last_success}\n"
                f"Local API attempts today (UTC): {self.store.usage()}/{settings['api_daily_budget']}\n"
                f"Latest scan: {scan_status}\n"
                f"Weekday summary: {settings['daily_scan_time']} ({settings['timezone']})\n"
                + self.controls.status()
            )
        if command == "watchlist":
            stocks = self.store.watchlist()
            return (
                "Watchlist:\n" + "\n".join(f"{ticker} • tier {tier}" for ticker, tier in stocks)
                if stocks
                else "Watchlist is empty. Use /add IBM 1."
            )
        if command == "add":
            if not 1 <= len(args) <= 2:
                raise ValueError("Usage: /add TICKER [1|2|3]")
            ticker = symbol(args[0])
            if len(args) == 2 and args[1] not in ("1", "2", "3"):
                raise ValueError("Tier must be 1, 2 or 3.")
            tier = int(args[1]) if len(args) == 2 else 2
            if (
                self.store.settings()["monitoring"] == "on"
                and ticker not in dict(self.store.watchlist())
                and len(self.store.watchlist()) >= 50
            ):
                raise ValueError(
                    "Free monitoring supports 50 symbols. Turn monitoring off before expanding the watchlist."
                )
            self.store.add(ticker, tier)
            self.controls.changed()
            return f"Saved {ticker}, tier {tier}. Use /quote {ticker} to check data availability."
        if command == "remove":
            if len(args) != 1:
                raise ValueError("Usage: /remove TICKER")
            ticker = symbol(args[0])
            removed = self.store.remove(ticker)
            self.controls.changed()
            return f"Removed {ticker}." if removed else f"{ticker} is not in your watchlist."
        if command == "quote":
            if len(args) != 1:
                raise ValueError("Usage: /quote TICKER")
            quote, cached = self.market.quote(symbol(args[0]))
            if quote.get("provider") == "finnhub":
                return intraday_text(
                    quote,
                    cached=cached,
                    max_age=int(self.store.settings()["max_source_age_seconds"]),
                )
            source = "cached" if cached else "fetched now"
            return (
                f"{quote['ticker']}: {quote['price']:.4f}\n"
                f"Daily change: {quote['change_percent']:+.2f}%\n"
                f"Trading day: {quote['trading_day']}\n"
                f"Source: Alpha Vantage, {source}. Default end-of-day quote; not live.\n"
                "Price is in the listing currency."
            )
        if command == "prices":
            stocks = self.store.watchlist()
            if not stocks:
                return "Your watchlist is empty. Use /add IBM 1."
            if self.store.settings()["market_provider"] == "finnhub":
                rows = ["Cached Finnhub prices • no API requests"]
                for ticker, _ in stocks:
                    quote = self.controls.monitor.latest(ticker) if self.controls.monitor else None
                    quote = quote or self.store.cached_live("finnhub", ticker)
                    rows.append(
                        intraday_text(
                            quote,
                            cached=True,
                            max_age=int(self.store.settings()["max_source_age_seconds"]),
                        )
                        if quote
                        else f"{ticker}: no cached Finnhub observation"
                    )
                return "\n\n".join(rows)
            cache = self.store.all_cached_quotes()
            rows = ["Cached watchlist prices • no API requests"]
            for ticker, _tier in stocks:
                if ticker in cache:
                    quote, fetched_at = cache[ticker]
                    age = max(0, int((time.time() - fetched_at) / 60))
                    rows.append(
                        f"{ticker}: {quote['price']:.4f} ({quote['change_percent']:+.2f}%) • "
                        f"{quote['trading_day']} • fetched {age} min ago"
                    )
                else:
                    rows.append(f"{ticker}: no cached quote; use /quote {ticker}")
            rows.append("End-of-day data; listing currency. Cached values can be old.")
            return "\n".join(rows)
        if command == "scan":
            if len(args) > 1:
                raise ValueError("Usage: /scan [TICKER]")
            return self.scans.start(symbol(args[0]) if args else None)
        if command == "lastscan":
            scan = self.store.latest_scan()
            if not scan:
                return "No scans yet. Use /scan to check your watchlist."
            return scan[2] or f"Scan #{scan[0]} is {scan[1]}."
        if command == "schedule":
            if len(args) != 1:
                raise ValueError(
                    "Usage: /schedule HH:MM or /schedule off. Uses your /settings timezone."
                )
            self.store.set_setting("daily_scan_time", args[0])
            value = self.store.settings()["daily_scan_time"]
            if value == "off":
                return "Scheduled scans are off. Manual /scan and /quote remain available."
            return (
                f"Weekday summary scheduled at {value} ({self.store.settings()['timezone']}). "
                "One scan per local date, within your API budget. Use /schedule off to disable it."
            )
        if command == "settings":
            return (
                "Preferences:\n"
                + "\n".join(
                    f"{key} = {value}\n  {DESCRIPTIONS[key]}"
                    for key, value in self.store.settings().items()
                )
                + "\n\nChange with /set NAME VALUE. Example: /set dip_threshold_percent -5"
            )
        if command == "set":
            if len(args) != 2:
                raise ValueError("Usage: /set NAME VALUE. Use /settings to see available names.")
            key, value = args
            if key in ("market_provider", "monitoring"):
                return self.controls.handle(
                    "provider" if key == "market_provider" else "monitor", [value]
                )
            self.store.set_setting(key, value)
            self.controls.changed()
            value = self.store.settings()[key]
            return f"Saved {key} = {value}. Applied immediately."
        return "Unknown command. Use /help."

    def process(self, update: dict):
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        if update_id < int(self.store.state("telegram_offset", "0")):
            return
        # Persist before dispatch to avoid duplicate API calls after a crash.
        # A crash or delivery failure can lose one reply; commands can be resent.
        self.store.set_state("telegram_offset", str(update_id + 1))
        try:
            self.handle(update)
        except ApiError:
            raise
        except Exception:
            # Avoid exception text/tracebacks, which can include provider secrets.
            logger.error("Command failed unexpectedly; update consumed. Try /status again.")
