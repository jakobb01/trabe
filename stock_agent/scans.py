"""One background watchlist scan at a time; scheduled scans are opt-in."""

import logging
import queue
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from stock_agent.api import ApiError, MarketData
from stock_agent.store import Store

logger = logging.getLogger(__name__)


class Scans:
    def __init__(self, store: Store, market: MarketData):
        self.store, self.market = store, market
        self.results: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, ticker: str | None = None, *, day: str | None = None) -> str:
        stocks = [(ticker, 2)] if ticker else self.store.watchlist()
        if not stocks:
            return "Your watchlist is empty. Add a symbol first, e.g. /add IBM 1."
        if not self.market.api_key:
            return "Market API key is missing. Configure it before starting a scan."
        job_id = self.store.start_scan(day)
        if job_id is None:
            if day is not None:
                return "A scan is already running or today's scheduled summary has already run."
            return "A scan is already running. Use /lastscan for its result."
        # A daemon cannot hold shutdown indefinitely. Every API call is bounded,
        # and the next runtime marks an interrupted job explicitly.
        self._thread = threading.Thread(
            target=self._work, args=(job_id, stocks), daemon=True, name="watchlist-scan"
        )
        try:
            self._thread.start()
        except RuntimeError:
            self.store.finish_scan(job_id, "Worker could not start. Try /scan again.", "failed")
            raise ValueError("Scan worker could not start. Try again later.") from None
        return f"Scan #{job_id} started for {len(stocks)} symbol(s). I'll send the summary here. /status stays available."

    def _work(self, job_id: int, stocks: list[tuple[str, int]]):
        settings = self.store.settings()
        threshold = float(settings["dip_threshold_percent"])
        lines = [f"Watchlist scan #{job_id} • daily dip threshold {threshold:g}%"]
        failures, checked, dips = 0, 0, 0
        status = "completed"
        try:
            # Prefer older/missing cache entries on a large watchlist so repeated
            # scans make progress within the same request budget.
            cache = self.store.all_cached_quotes()
            stocks = sorted(stocks, key=lambda row: cache.get(row[0], ({}, 0))[1])
            for ticker, _tier in stocks:
                if self._stop.is_set():
                    status = "interrupted"
                    lines.append("Scan interrupted by service shutdown.")
                    break
                try:
                    quote, cached = self.market.quote(ticker)
                    is_dip = quote["change_percent"] <= threshold
                    checked += 1
                    dips += int(is_dip)
                    marker = "🔻" if is_dip else "•"
                    lines.append(
                        f"{marker} {ticker}: {quote['price']:.4f} ({quote['change_percent']:+.2f}%) • "
                        f"{quote['trading_day']}{' • cached' if cached else ''}"
                    )
                except ValueError as error:
                    lines.append(str(error))
                    break  # Missing key or exhausted budget cannot be fixed by more calls.
                except ApiError as error:
                    failures += 1
                    lines.append(f"• {ticker}: data unavailable")
                    if error.code == 429:
                        lines[-1] = f"• {ticker}: provider request limit reached"
                        lines.append(str(error))
                        lines.append("Remaining symbols skipped. Cached prices remain available.")
                        break
                    if failures >= 3:
                        lines.append(
                            "Provider unavailable; remaining symbols skipped. Try again later."
                        )
                        break
                self._stop.wait(0.01)
            lines.append(
                f"Checked {checked}/{len(stocks)} • dip flags: {dips} • failed requests: {failures}."
            )
            lines.append(
                "Alpha Vantage end-of-day data; listing currency. These are daily changes, not live prices."
            )
        except Exception:
            logger.error("Watchlist scan failed unexpectedly; no provider details logged.")
            lines.append("Scan failed unexpectedly. Try /status, then /scan again.")
            status = "failed"
        summary = "\n".join(lines)
        self.store.finish_scan(job_id, summary, status)
        self.results.put(summary)

    def schedule(self, now: datetime | None = None):
        settings = self.store.settings()
        scheduled = settings["daily_scan_time"]
        if scheduled == "off" or not self.store.watchlist():
            return
        local = (now or datetime.now().astimezone()).astimezone(ZoneInfo(settings["timezone"]))
        if local.weekday() >= 5 or local.strftime("%H:%M") < scheduled:
            return
        self.start(day=local.date().isoformat())

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=20)
