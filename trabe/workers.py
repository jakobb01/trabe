"""Independent delivery and maintenance keep polling and intraday work responsive."""

import logging
import threading

from trabe.api import ApiError
from trabe.fx import FX
from trabe.maintenance import Maintenance
from trabe.monitor import Monitor
from trabe.portfolio import Portfolio

logger = logging.getLogger(__name__)


class Services:
    def __init__(self, store, config, market, outbox, telegram):
        self.store, self.config, self.outbox, self.telegram = store, config, outbox, telegram
        self.stop = threading.Event()
        self.maintenance = Maintenance(store, config.owner_id)
        self.monitor = (
            Monitor(store, config, market.finnhub) if hasattr(market, "finnhub") else None
        )
        if self.monitor:
            market.monitor = self.monitor
        self.fx = FX(store)
        self.portfolio = Portfolio(store, self.monitor)
        self.threads = []

    def start(self):
        # Enforce retention after an upgrade/restore before serving history or streaming.
        self.maintenance.run_once()
        for target, name in (
            (self.deliver, "telegram-delivery"),
            (self.maintain, "storage-maintenance"),
            (self.portfolio_work, "portfolio-work"),
        ):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self.threads.append(thread)
        if self.monitor:
            self.monitor.start()

    def deliver(self):
        delay = 1
        while not self.stop.is_set():
            try:
                self.outbox.flush(self.telegram)
                self.store.set_state("delivery_error", "")
                delay = 1
                self.stop.wait(0.2)
            except ApiError as error:
                self.store.set_state(
                    "delivery_error", f"Telegram delivery waiting (HTTP {error.code or 'network'})"
                )
                self.stop.wait(max(delay, error.retry_after))
                delay = min(60, delay * 2)
            except Exception:
                logger.error("Reply delivery failed; persistent queue retained.")
                self.stop.wait(2)

    def maintain(self):
        failures = 0
        while not self.stop.wait(30):
            try:
                self.maintenance.run_once()
                failures = 0
            except Exception:
                failures += 1
                logger.error("Storage maintenance failed; retry scheduled.")
                try:
                    self.store.set_state(
                        "maintenance_error", "Maintenance failed; retry scheduled."
                    )
                    if failures >= 3:
                        self.store.set_state(
                            "storage_paused", "Repeated maintenance failures; collection paused."
                        )
                except Exception:
                    pass

    def portfolio_work(self):
        while not self.stop.is_set():
            try:
                if not self.store.automation_paused():
                    self.fx.refresh()
                self.portfolio.schedule(self.config.owner_id, self.outbox)
            except Exception:
                logger.error("Portfolio background work failed; retry scheduled.")
            self.stop.wait(60)

    def close(self):
        if self.monitor:
            self.monitor.close()
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=5)
