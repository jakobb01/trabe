"""Owner-dispatched intraday controls and local history/storage presentation."""

import json
import re
import time
from datetime import UTC, datetime

from trabe.calendar import USCalendar
from trabe.maintenance import Maintenance


class Controls:
    def __init__(self, config, store, market, monitor=None):
        self.config, self.store, self.market, self.monitor = config, store, market, monitor
        self.maintenance = Maintenance(store, config.owner_id)

    def changed(self):
        if self.monitor:
            self.monitor.changed()

    def switch(self, provider):
        if self.monitor:
            self.monitor.cancel_validation()
        self.store.select_provider(provider)
        self.changed()

    def handle(self, command, args):
        if command in ("alerts", "storage", "resume") and args:
            raise ValueError(f"Usage: /{command}")
        if command == "provider":
            if len(args) > 1 or (args and args[0] not in ("finnhub", "alphavantage")):
                raise ValueError("Usage: /provider [finnhub|alphavantage]")
            if not args:
                client = getattr(self.market, "finnhub", None)
                return (
                    f"Selected: {self.store.settings()['market_provider']}\n"
                    f"Finnhub key: {'configured' if self.config.finnhub_key else 'missing'} • "
                    f"access: {'verified' if client and client.verified() else 'not verified'}\n"
                    "Finnhub: US last-trade stream, up to 50 subscribed symbols.\n"
                    "Alpha Vantage: end-of-day quotes. An outage never changes providers automatically.\n"
                    f"Validation: {self.store.state('provider_validation', 'not requested')}"
                )
            if args[0] == "alphavantage":
                if not self.config.api_key:
                    raise ValueError("Alpha Vantage key is not configured.")
                self.switch("alphavantage")
                return "Alpha Vantage selected (end-of-day). Intraday monitoring is off."
            client = getattr(self.market, "finnhub", None)
            if not self.config.finnhub_key or not client:
                raise ValueError(
                    "Configure FINNHUB_API_KEY locally and redeploy before selecting Finnhub."
                )
            if not client.verified():
                if not self.monitor:
                    raise ValueError("Finnhub validation worker is unavailable.")
                self.monitor.request_validation()
                return "Finnhub validation queued. /status stays available; I'll report the result here."
            self.switch("finnhub")
            return f"Finnhub selected. Monitoring is {self.store.settings()['monitoring']}. Use /monitor on to enable it."
        if command == "monitor":
            if len(args) > 1 or (args and args[0] not in ("on", "off")):
                raise ValueError("Usage: /monitor [on|off]")
            if not args:
                return self.status()
            if args[0] == "on":
                client = getattr(self.market, "finnhub", None)
                if (
                    self.store.settings()["market_provider"] != "finnhub"
                    or not client
                    or not client.verified()
                ):
                    raise ValueError("Select and verify Finnhub first: /provider finnhub")
                count = len(self.store.watchlist())
                if not 1 <= count <= 50:
                    raise ValueError("Monitoring needs 1–50 watchlist symbols on the free plan.")
                USCalendar().at(time.time())
                if self.store.state("storage_paused"):
                    raise ValueError("Storage maintenance needs attention first. Use /storage.")
            self.store.set_setting("monitoring", args[0])
            if args[0] == "off":
                with self.store.connection() as db:
                    db.execute("DELETE FROM outbox WHERE kind='alert'")
            self.changed()
            return f"Monitoring {args[0]}. " + (
                "Collection runs only in verified US core sessions; alerts follow your rules."
                if args[0] == "on"
                else "Manual commands and storage maintenance remain available."
            )
        if command == "alert":
            from trabe.bot import symbol

            if len(args) != 2:
                raise ValueError("Usage: /alert TICKER -5|default|off")
            ticker = symbol(args[0])
            self.store.set_alert_rule(ticker, args[1])
            self.changed()
            enabled, threshold = self.store.alert_rule(ticker)
            return f"{ticker}: {'threshold ' + str(threshold) + '%' if enabled else 'alerts off'}. Watchlist entry retained."
        if command == "alerts":
            rows = [
                f"Intraday alerts • global threshold {self.store.settings()['dip_threshold_percent']}%",
                "Automation paused."
                if self.store.automation_paused()
                else "Automation not paused.",
            ]
            for ticker, _ in self.store.watchlist():
                enabled, threshold = self.store.alert_rule(ticker)
                rows.append(f"{ticker}: {str(threshold) + '%' if enabled else 'off'}")
            rows.append("Recent alerts:")
            events = self.store.recent_alerts()
            rows.extend(
                f"{row['session_date']} {row['ticker']} {row['change_percent']:+.2f}% • {row['provider']}"
                for row in events
            )
            if not events:
                rows.append("None yet.")
            rows.append(
                "One alert per symbol/rule/exchange session. /alert VST -5 | /alert VST default | /alert VST off"
            )
            return "\n".join(rows)
        if command == "pause":
            if len(args) > 1:
                raise ValueError("Usage: /pause 24h (1 minute to 7 days)")
            match = re.fullmatch(r"([0-9]{1,5})([mhd])", args[0] if args else "24h")
            if not match:
                raise ValueError("Usage: /pause 24h; units m, h or d.")
            seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
            if not 60 <= seconds <= 604800:
                raise ValueError("Pause duration must be between 1 minute and 7 days.")
            until = time.time() + seconds
            self.store.pause_automation(until)
            self.changed()
            return f"Automation paused until {datetime.fromtimestamp(until, UTC).isoformat()}. Manual commands work. /resume ends the pause."
        if command == "resume":
            self.store.pause_automation(0)
            self.changed()
            return f"Pause cleared. Monitoring is {self.store.settings()['monitoring']}; the saved schedule applies without historical alerts."
        if command == "storage":
            return self.maintenance.report()
        if command == "history":
            from trabe.bot import symbol

            if not 1 <= len(args) <= 2:
                raise ValueError("Usage: /history TICKER [1–30 sessions]")
            ticker = symbol(args[0])
            count = args[1] if len(args) > 1 else "5"
            if not count.isascii() or not count.isdecimal() or not 1 <= int(count) <= 30:
                raise ValueError("History count must be 1–30.")
            self.maintenance.expire(time.time())
            values = self.store.history(ticker, int(count))
            if not values:
                return f"No retained daily summary for {ticker} yet. Summaries appear after session close; minute detail is temporary."
            rows = [f"{ticker} • daily observed feed summaries • USD"]
            for value in values:
                prices = (
                    "no usable prices"
                    if value["observed_last"] is None
                    else " / ".join(
                        f"{value[key]:.4f}"
                        for key in (
                            "observed_open",
                            "observed_high",
                            "observed_low",
                            "observed_last",
                        )
                    )
                )
                rows.append(
                    f"{value['session_date']}: {prices}\n  {value['provider']}/{value['feed']} • minutes {value['observed_minutes']}/{value['expected_minutes']} • {'partial' if value['quality_flags'] else 'observed'}"
                )
            rows.append(
                "First / high / low / last from received data; not official exchange OHLC. Retained for 3 calendar months."
            )
            return "\n".join(rows)
        return None

    def status(self):
        settings = self.store.settings()
        try:
            status = json.loads(self.store.state("monitor_status", "{}"))
            session = USCalendar().describe(time.time())
        except (ValueError, KeyError):
            status, session = {}, "calendar unavailable/expired"
        source = status.get("last_source", 0)
        last = datetime.fromtimestamp(source, UTC).isoformat() if source else "none yet"
        return (
            f"Selected source: {settings['market_provider']}\nMonitoring: {settings['monitoring']} • "
            f"{'paused' if self.store.automation_paused() else 'not paused'}\n"
            f"Stream: {status.get('state', 'not started')}\nSession: {session}\n"
            f"Subscribed: {', '.join(status.get('subscribed', [])) or 'none'}\n"
            f"Symbol issues: {json.dumps(status.get('unsupported', {}))}\nLast source observation: {last}\n"
            f"Finnhub REST attempts today (UTC): {self.store.provider_usage('finnhub')}/{settings['finnhub_daily_budget']}\n"
            f"Delivery: {self.store.state('delivery_error') or 'ready'}\n"
            f"Maintenance: {self.store.state('maintenance_error') or 'healthy'}\n"
            + (
                "Live intraday alerts are not enabled."
                if settings["monitoring"] == "off"
                else "Intraday rules follow the saved thresholds and pause state."
            )
        )


def intraday_text(quote: dict, *, cached: bool, max_age: int) -> str:
    now = time.time()
    age = max(0, now - quote["source_time"])
    try:
        session = USCalendar().at(now)
        regular = bool(session and session.contains(now) and session.contains(quote["source_time"]))
    except ValueError:
        regular = False
    freshness = (
        "fresh intraday"
        if regular and age <= max_age
        else "stale for alerts"
        if regular
        else "last observation • market closed"
    )
    source = datetime.fromtimestamp(quote["source_time"], UTC).isoformat()
    basis = quote.get("baseline_reason", "")
    return (
        f"{quote['ticker']}: {quote['price']:.4f} USD\n"
        f"Change vs provider previous close: {quote['change_percent']:+.2f}%\n"
        f"Source: Finnhub/{quote['feed']} • {'cached/streamed' if cached else 'fetched now'}\n"
        f"Observed: {source} • age {age:.0f}s • {freshness}\n"
        f"Price kind: {quote.get('price_kind', 'last-trade')}\n"
        + (f"Alert baseline: {basis}\n" if basis else "")
        + "Observed feed price; coverage can differ from consolidated market data."
    )
