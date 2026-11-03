"""Finnhub REST bootstrap; secrets use headers and errors never expose responses."""

import fcntl
import hashlib
import json
import math
import os
import time
from datetime import date, datetime
from urllib.parse import urlencode

from trabe.api import ApiError, request_json
from trabe.calendar import NY, USCalendar


class Finnhub:
    def __init__(self, api_key: str, store, transport=request_json, calendar=None):
        self.api_key, self.store, self.transport = api_key, store, transport
        self.calendar = calendar or USCalendar()

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self.api_key.encode()).hexdigest()

    def verified(self) -> bool:
        return (
            bool(self.api_key) and self.store.state("finnhub_verified_key") == self.key_fingerprint
        )

    def call(self, endpoint: str, *, automatic: bool = False, **params):
        if not self.api_key:
            raise ValueError("Finnhub key missing. Configure FINNHUB_API_KEY locally and redeploy.")
        fd = os.open(str(self.store.path) + ".finnhub.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.check_automation(automatic)
            if float(self.store.state("finnhub_retry_after", "0")) > time.time():
                raise ApiError("Finnhub is cooling down; cached prices remain available.", 429, 60)
            self.store.reserve_provider(
                "finnhub", int(self.store.settings()["finnhub_daily_budget"])
            )
            elapsed = time.time() - float(self.store.state("finnhub_request_finished", "0"))
            if elapsed < 1.4:
                time.sleep(min(1.4, 1.4 - elapsed))
            url = "https://finnhub.io/api/v1/" + endpoint + "?" + urlencode(params)
            try:
                self.check_automation(automatic)
                result = self.transport(url, timeout=15, headers={"X-Finnhub-Token": self.api_key})
            except ApiError as error:
                if error.code == 429:
                    self.store.set_state(
                        "finnhub_retry_after", str(time.time() + max(60, error.retry_after))
                    )
                if error.code in (401, 403):
                    raise ApiError(
                        "Finnhub denied access. Check key, plan and symbol entitlement.", error.code
                    ) from None
                raise
            finally:
                self.store.set_state("finnhub_request_finished", str(time.time()))
            if not isinstance(result, dict) or result.get("error"):
                raise ApiError("Finnhub rejected the request; verify account access.")
            return result
        finally:
            os.close(fd)

    def check_automation(self, automatic: bool):
        if automatic:
            settings = self.store.settings()
            if (
                self.store.automation_paused()
                or settings["monitoring"] != "on"
                or settings["market_provider"] != "finnhub"
                or self.store.state("storage_paused")
            ):
                raise ValueError("Automatic Finnhub work is paused.")

    def metadata(self, ticker: str, *, automatic: bool = False) -> dict:
        state = "finnhub_profile_" + ticker
        cached = json.loads(self.store.state(state, "{}"))
        if cached and 0 <= time.time() - cached.get("checked_at", 0) < 86400:
            return cached
        raw = self.call("stock/profile2", symbol=ticker, automatic=automatic)
        exchange = str(raw.get("exchange", "")).upper()
        if raw.get("currency") != "USD" or not any(
            name in exchange
            for name in ("NASDAQ", "NEW YORK", "NYSE", "AMEX", "CBOE", "ARCA", "BATS")
        ):
            raise ApiError("Finnhub did not confirm a supported US USD listing for this symbol.")
        if raw.get("ticker") != ticker:
            raise ApiError("Finnhub did not return a matching instrument profile.")
        result = {
            "ticker": ticker,
            "currency": "USD",
            "exchange": exchange[:100],
            "checked_at": time.time(),
        }
        self.store.set_state(state, json.dumps(result))
        return result

    def quote(
        self, ticker: str, *, refresh: bool = False, automatic: bool = False
    ) -> tuple[dict, bool]:
        cached = self.store.cached_live("finnhub", ticker)
        if cached and not refresh and 0 <= time.time() - cached["received_at"] < 15:
            return cached, True
        self.metadata(ticker, automatic=automatic)
        raw = self.call("quote", symbol=ticker, automatic=automatic)
        try:
            if any(isinstance(raw.get(key), bool) for key in ("c", "pc", "t", "o", "dp")):
                raise ValueError
            price, previous, source_time = float(raw["c"]), float(raw["pc"]), float(raw["t"])
            opening = float(raw.get("o", 0))
            if not all(math.isfinite(v) for v in (price, previous, source_time, opening)):
                raise ValueError
            if min(price, previous, source_time) <= 0 or source_time > time.time() + 2:
                raise ValueError
            change = (price / previous - 1) * 100
            if "dp" in raw and (
                not math.isfinite(float(raw["dp"])) or abs(float(raw["dp"]) - change) > 0.1
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ApiError(
                "Finnhub returned no usable quote/baseline; this is not a ticker validity result."
            ) from None
        quote = {
            "ticker": ticker,
            "provider": "finnhub",
            "feed": "us-trades",
            "price_kind": "last-trade",
            "currency": "USD",
            "price": price,
            "previous_close": previous,
            "open_price": opening,
            "change_percent": change,
            "source_time": source_time,
            "received_at": time.time(),
            "trading_day": datetime.fromtimestamp(source_time, NY).date().isoformat(),
        }
        self.store.save_live(quote)
        return quote, False

    def prepare_baseline(self, ticker: str, session, *, automatic: bool = False) -> dict:
        cached = self.store.baseline(ticker, session.day)
        if cached and cached["accepted"]:
            gap = abs(cached["open_price"] / cached["previous_close"] - 1) * 100
            if gap <= float(self.store.settings()["baseline_max_gap_percent"]):
                return cached
        quote, _ = self.quote(ticker, refresh=True, automatic=automatic)
        reason = ""
        if quote["trading_day"] != session.day or not session.contains(quote["source_time"]):
            reason = "Awaiting a quote from the current regular session."
        elif quote["open_price"] <= 0:
            reason = "Current-session opening price is missing."
        elif abs(quote["open_price"] / quote["previous_close"] - 1) * 100 > float(
            self.store.settings()["baseline_max_gap_percent"]
        ):
            reason = (
                "Large opening gap: baseline/corporate-action review required; alerts suppressed."
            )
        values = {
            "previous_date": self.calendar.previous(date.fromisoformat(session.day)).day,
            "previous_close": quote["previous_close"],
            "open_price": quote["open_price"],
            "source_time": quote["source_time"],
            "accepted": not reason,
            "reason": reason,
        }
        self.store.save_baseline(ticker, session.day, values)
        return self.store.baseline(ticker, session.day)

    def validate(self, tickers: list[str] | None = None) -> dict:
        selected = tickers or ["GOOGL", "VST"]
        results = {}
        for ticker in selected:
            quote, _ = self.quote(ticker, refresh=True)
            results[ticker] = {
                "source_time": quote["source_time"],
                "trading_day": quote["trading_day"],
            }
        status = self.call("stock/market-status", exchange="US")
        if status.get("exchange") != "US" or status.get("timezone") != "America/New_York":
            raise ApiError("Finnhub market-status response could not be verified.")
        timestamp = status.get("t")
        if type(timestamp) not in (int, float) or not -2 <= time.time() - timestamp <= 120:
            raise ApiError("Finnhub market-status timestamp is stale or invalid.")
        self.store.set_state("finnhub_verified_key", self.key_fingerprint)
        self.store.set_state("finnhub_verified_at", str(time.time()))
        return {"quotes": results, "market_status": status}
