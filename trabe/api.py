"""Bounded HTTPS calls with deliberately sanitized errors (URLs contain secrets)."""

import json
import math
import os
import threading
import time
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from trabe import __version__
from trabe.presentation import message_entities
from trabe.store import Store


class ApiError(Exception):
    def __init__(self, message: str, code: int = 0, retry_after: int = 0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def message_chunks(text: str, max_units: int = 3500):
    """Telegram counts UTF-16 units; do not split a non-BMP character in half."""
    start, units = 0, 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > max_units:
            yield text[start:index]
            start, units = index, 0
        units += width
    if start < len(text):
        yield text[start:]


def request_json(
    url: str, payload: dict | None = None, timeout: int = 20, headers: dict | None = None
) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"trabe/{__version__}",
            **(headers or {}),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ApiError("API response exceeds size limit.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ApiError("API returned an unexpected response.")
        return result
    except HTTPError as error:
        # Do not log error objects or bodies: either may contain the secret URL.
        retry_after = 0
        if error.code == 429:
            try:
                body = json.loads(error.read(16_384))
                retry_after = min(
                    3600, max(1, int(body.get("parameters", {}).get("retry_after", 30)))
                )
            except (ValueError, TypeError, AttributeError):
                retry_after = 30
        error.close()
        raise ApiError(f"Upstream HTTP error {error.code}.", error.code, retry_after) from None
    except (URLError, TimeoutError, OSError):
        raise ApiError("Network request failed or timed out.") from None
    except (ValueError, UnicodeError):
        raise ApiError("API returned invalid JSON.") from None


class Telegram:
    def __init__(self, token: str, transport=request_json):
        self._base = f"https://api.telegram.org/bot{token}/"
        self.transport = transport
        self._send_lock = threading.Lock()
        self._next_send = 0.0

    def call(self, method: str, **payload):
        result = self.transport(self._base + method, payload, timeout=40)
        if result.get("ok") is not True:
            # Telegram descriptions can reflect submitted content; keep them private.
            raise ApiError("Telegram rejected the request.", int(result.get("error_code", 0)))
        return result.get("result")

    def send(self, chat_id: int, text: str, *, reply_markup: dict | None = None):
        # Native entities style plain text safely, including older queued replies.
        with self._send_lock:
            for index, chunk in enumerate(message_chunks(text)):
                delay = self._next_send - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                payload = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "link_preview_options": {"is_disabled": True},
                }
                entities = message_entities(chunk)
                if entities:
                    payload["entities"] = entities
                if index == 0 and reply_markup is not None:
                    payload["reply_markup"] = reply_markup
                try:
                    self.call("sendMessage", **payload)
                finally:
                    self._next_send = time.monotonic() + 1.05

    def updates(self, offset: int, timeout: int = 25):
        result = self.call(
            "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"], limit=20
        )
        if not isinstance(result, list):
            raise ApiError("Telegram returned an unexpected update list.")
        return result


class MarketData:
    def __init__(self, api_key: str, store: Store, transport=request_json):
        self.api_key = api_key
        self.store = store
        self.transport = transport
        self._lock = threading.Lock()

    def quote(self, ticker: str, *, refresh: bool = False) -> tuple[dict, bool]:
        import fcntl

        with self._lock:
            # The bot, scan worker and doctor can use separate clients/processes.
            # Share a short request lock through the same private data volume.
            fd = os.open(str(self.store.path) + ".market.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                return self._quote(ticker, refresh=refresh)
            finally:
                os.close(fd)

    def _quote(self, ticker: str, *, refresh: bool = False) -> tuple[dict, bool]:
        settings = self.store.settings()
        cached = self.store.cached_quote(ticker, int(settings["quote_cache_minutes"]))
        if cached and not refresh:
            return cached, True
        if not self.api_key:
            raise ValueError(
                "Market API key missing. Add ALPHA_VANTAGE_API_KEY to the server environment and redeploy."
            )
        until = float(self.store.state("market_retry_after", "0"))
        if until > time.time():
            raise ApiError(
                f"Market provider is cooling down; retry in {int(until - time.time()) + 1} seconds.",
                429,
            )
        self.store.reserve_request(int(settings["api_daily_budget"]))
        # Alpha Vantage's free key also has a one-request/second burst limit.
        # Wait after the previous request finishes, including failed requests.
        finished = float(self.store.state("market_request_finished", "0"))
        delay = max(0, min(1.1, finished + 1.1 - time.time()))
        if delay:
            time.sleep(delay)
        query = urlencode({"function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": self.api_key})
        try:
            data = self.transport("https://www.alphavantage.co/query?" + query, timeout=15)
        except ApiError as error:
            if error.code == 429:
                self.store.set_state(
                    "market_retry_after", str(time.time() + max(60, error.retry_after))
                )
            raise
        finally:
            self.store.set_state("market_request_finished", str(time.time()))
        notice = str(data.get("Note") or data.get("Information") or "").lower()
        if notice:
            if any(
                phrase in notice
                for phrase in (
                    "rate limit",
                    "call frequency",
                    "requests per",
                    "request per",
                    "spreading out",
                )
            ):
                self.store.set_state("market_retry_after", str(time.time() + 60))
                raise ApiError(
                    "Alpha Vantage request limit reached. Wait at least a minute before retrying; "
                    "if its daily allowance is exhausted, wait for the provider's reset. "
                    "This is a provider limit, not a ticker lookup result.",
                    429,
                    60,
                )
            raise ApiError("Alpha Vantage refused the request. Check the API key and plan access.")
        raw = data.get("Global Quote")
        if not isinstance(raw, dict) or not raw:
            raise ApiError(
                f"Alpha Vantage returned no quote for {ticker}. "
                "This does not confirm the ticker is invalid; the provider may be unavailable."
            )
        try:
            quote = {
                "ticker": raw["01. symbol"],
                "price": float(raw["05. price"]),
                "change_percent": float(raw["10. change percent"].rstrip("%")),
                "trading_day": date.fromisoformat(raw["07. latest trading day"]).isoformat(),
            }
            if (
                quote["ticker"] != ticker
                or not math.isfinite(quote["price"])
                or quote["price"] <= 0
                or not math.isfinite(quote["change_percent"])
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ApiError("Market provider returned an incomplete or invalid quote.") from None
        self.store.save_quote(ticker, quote)
        self.store.set_state("last_api_success", quote["trading_day"])
        return quote, False
