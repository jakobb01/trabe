"""Bounded indicative premarket bars and delayed VIX for the Telegram briefing."""

import re
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from trabe.calendar import NY, USCalendar
from trabe.signals import SignalStore, WorkStopped

BENCHMARKS = ("SPY", "QQQ")
SOURCE = "yahoo-chart"
CACHE_SECONDS = 900


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid market observation.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid market observation.") from None
    if not result.is_finite() or not 0 < result < Decimal("1e12"):
        raise ValueError("Invalid market observation.")
    return result


def timestamp(value, received):
    result = float(number(value))
    if result > received + 2:
        raise ValueError("Future market observation.")
    datetime.fromtimestamp(result, NY)
    return result


def target_session(calendar, now):
    session = calendar.at(now)
    return session if session and now < session.closes else calendar.next_session(now)


def preopen(session):
    return datetime.combine(datetime.fromisoformat(session.day).date(), time(4), NY).timestamp()


def parse_chart(raw, symbol, received, calendar):
    """Bind prices to their actual sessions; chartPreviousClose is not yesterday."""
    provider_symbol = symbol.replace(".", "-")
    chart = raw["chart"]
    if chart.get("error") or len(chart["result"]) != 1:
        raise ValueError("Unavailable chart.")
    row = chart["result"][0]
    meta = row["meta"]
    index = symbol == "^VIX"
    if (
        meta.get("symbol") != provider_symbol
        or meta.get("dataGranularity") != "1m"
        or (index and meta.get("instrumentType") != "INDEX")
        or (
            not index
            and (
                meta.get("instrumentType") not in {"EQUITY", "ETF"}
                or meta.get("currency") != "USD"
                or meta.get("exchangeTimezoneName") != "America/New_York"
            )
        )
    ):
        raise ValueError("Unsupported or mismatched chart instrument.")
    regular_time = timestamp(meta["regularMarketTime"], received)
    regular_day = datetime.fromtimestamp(regular_time, NY).date()
    session = target_session(calendar, received)
    value = {
        "symbol": symbol,
        "currency": "points" if index else "USD",
        "regular_price": str(number(meta["regularMarketPrice"])),
        "regular_time": regular_time,
        "regular_day": regular_day.isoformat(),
        "received_at": received,
        "previous_close": None,
        "previous_close_day": None,
        "premarket_price": None,
        "premarket_time": None,
        "premarket_session": session.day,
        "delay_minutes": 15 if index else None,
    }
    # previousClose belongs to the session named in currentTradingPeriod, whereas
    # chartPreviousClose is the start of the requested multi-day chart window.
    anchor = meta["currentTradingPeriod"]["regular"]
    anchor_day = datetime.fromtimestamp(float(number(anchor["start"])), NY).date()
    anchor_session = calendar.session(anchor_day)
    if anchor_session and (index or abs(float(anchor["start"]) - anchor_session.opens) <= 2):
        try:
            value["previous_close"] = str(number(meta.get("previousClose")))
            value["previous_close_day"] = calendar.previous(anchor_day).day
        except ValueError:
            pass
    if index:
        return value
    times = row.get("timestamp", [])
    closes = row["indicators"]["quote"][0]["close"]
    if (
        not isinstance(times, list)
        or not isinstance(closes, list)
        or len(times) != len(closes)
        or len(times) > 10000
    ):
        raise ValueError("Invalid chart bars.")
    if meta.get("hasPrePostMarketData") is True:
        for raw_time, price in zip(times, closes):
            if price is None:
                continue
            observed = timestamp(raw_time, received)
            if preopen(session) <= observed < session.opens:
                if value["premarket_time"] is None or observed >= value["premarket_time"]:
                    value["premarket_price"] = str(number(price))
                    value["premarket_time"] = observed
    return value


def baseline(value, session, calendar):
    previous = calendar.previous(datetime.fromisoformat(session.day).date())
    if (
        value.get("regular_day") == previous.day
        and previous.closes - 120 <= value.get("regular_time", 0) <= previous.closes + 120
    ):
        return number(value["regular_price"]), previous.day
    if value.get("previous_close_day") == previous.day and value.get("previous_close"):
        return number(value["previous_close"]), previous.day
    return None, previous.day


class MarketContext:
    def __init__(self, store, http, calendar=None):
        self.store, self.http = store, http
        self.repo = SignalStore(store)
        self.calendar = calendar or USCalendar()

    def refresh(self, symbols, *, now, force=False, include_market=True):
        try:
            session = target_session(self.calendar, now)
        except ValueError:
            return
        selected = sorted(set(symbols) | (set(BENCHMARKS) | {"^VIX"} if include_market else set()))
        if not include_market:
            selected = [s for s in selected if s not in BENCHMARKS and s != "^VIX"]
        for symbol in selected:
            if symbol != "^VIX" and not re.fullmatch(r"[A-Z]{1,10}(?:[.-][A-Z])?", symbol):
                continue
            active = preopen(session) <= now < session.opens
            if symbol == "^VIX":
                today = self.calendar.at(now)
                active = bool(today and preopen(today) - 3600 <= now <= today.closes + 1800)
            interval = CACHE_SECONDS if active else 6 * 3600
            if not force and not self.repo.due("market-context", SOURCE, symbol, interval, now):
                continue
            self.repo.attempt("market-context", SOURCE, symbol, now)
            try:
                url = (
                    "https://query1.finance.yahoo.com/v8/finance/chart/"
                    + quote(symbol.replace(".", "-"), safe="")
                    + "?"
                    + urlencode({"range": "5d", "interval": "1m", "includePrePost": "true"})
                )
                raw = self.http.json(url, provider=SOURCE)
                received = max(now, getattr(self.http, "received_at", now))
                value = parse_chart(raw, symbol, received, self.calendar)
                value["source_url"] = url
                self.repo.put(
                    "market-context",
                    symbol,
                    symbol,
                    SOURCE,
                    value,
                    now=received,
                    event_time=value["regular_time"],
                )
                self.repo.ok("market-context", SOURCE, symbol, received)
            except WorkStopped:
                raise
            except Exception:
                self.repo.fail(
                    "market-context",
                    SOURCE,
                    symbol,
                    "Yahoo chart unavailable, deferred or invalid; cached observations retained.",
                    now,
                )

    def briefing(self, symbols, *, now):
        zone = ZoneInfo(self.store.settings()["timezone"])

        def local(stamp):
            return datetime.fromtimestamp(stamp, zone).strftime("%d %b %H:%M %Z")

        try:
            session = target_session(self.calendar, now)
            start = preopen(session)
        except ValueError:
            return ["Market context: calendar unavailable/expired."]
        lines = [
            f"Premarket • US session {session.day}",
            "Yahoo Finance • indicative 1m bars • equity delay unverified",
        ]
        if now < start:
            lines.append(
                f"Premarket not started; begins {local(start)}. Previous closes are references."
            )
        elif now >= session.opens:
            lines.append("Regular session underway; premarket bars below are earlier references.")
        selected = list(BENCHMARKS) + [s for s in dict.fromkeys(symbols) if s not in BENCHMARKS][:8]
        failed = {s["symbol"] for s in self.repo.status("market-context") if s["error"]}
        for symbol in selected:
            row = self.repo.get("market-context", symbol)
            if row and row["payload"]["received_at"] > now + 2:
                row = None
            if not row:
                lines.append(f"• {symbol}: premarket unavailable; no saved observation")
                continue
            value = row["payload"]
            try:
                close, close_day = baseline(value, session, self.calendar)
            except ValueError:
                close, close_day = None, None
            observed = value.get("premarket_time")
            if (
                value.get("premarket_session") == session.day
                and observed is not None
                and observed <= now
            ):
                price = number(value["premarket_price"])
                change = (
                    f"{(price / close - 1) * 100:+.2f}% vs {close_day} close {close:.2f}"
                    if close
                    else "change unavailable; previous close missing"
                )
                stale = " • stale" if now - observed > 20 * 60 else ""
                lines.append(f"• {symbol}: {price:.2f} USD • {change} • {local(observed)}{stale}")
            elif close:
                lines.append(
                    f"• {symbol}: premarket unavailable • previous close {close:.2f} USD ({close_day})"
                )
            else:
                lines.append(f"• {symbol}: premarket and previous close unavailable")
        row = self.repo.get("market-context", "^VIX")
        if row and row["payload"]["received_at"] > now + 2:
            row = None
        if row:
            value = row["payload"]
            price = number(value["regular_price"])
            previous = value.get("previous_close")
            change = "change unavailable; previous index close missing"
            if previous:
                previous = number(previous)
                change = f"{price - previous:+.2f} points ({(price / previous - 1) * 100:+.2f}%) vs {value['previous_close_day']} close"
            quality = (
                "prior session"
                if value["regular_day"] != session.day
                else "stale"
                if now - value["regular_time"] > 30 * 60
                else "observed"
            )
            lines.extend(
                (
                    "VIX • volatility index",
                    f"{price:.2f} points • {change}",
                    f"Yahoo Finance • 15m delayed • {quality} • {local(value['regular_time'])}",
                )
            )
        else:
            lines.append("VIX: unavailable; no saved index observation")
        if failed.intersection(set(selected) | {"^VIX"}):
            lines.append("Some market requests failed/deferred; saved observations may be stale.")
        return lines
