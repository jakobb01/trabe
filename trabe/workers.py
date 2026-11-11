"""Independent delivery and maintenance keep polling and intraday work responsive."""

import logging
import threading
import time

from trabe.advisor_results import AdvisorResults
from trabe.api import ApiError
from trabe.fx import FX
from trabe.insights import Insights
from trabe.maintenance import Maintenance
from trabe.monitor import Monitor
from trabe.outlook import OutlookService
from trabe.paper import Paper
from trabe.portfolio import Portfolio
from trabe.signals import SignalHTTP, WorkStopped
from trabe.snapshots import SnapshotService

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
        self.insights = Insights(config, store, outbox, self.monitor)
        self.snapshots = SnapshotService(store)
        self.outlooks = OutlookService(store)
        self.advisor_results = AdvisorResults(store)
        self.paper = Paper(store)
        self.threads = []

    def start(self):
        # Enforce retention after an upgrade/restore before serving history or streaming.
        self.maintenance.run_once()
        for target, name in (
            (self.deliver, "telegram-delivery"),
            (self.maintain, "storage-maintenance"),
            (self.portfolio_work, "portfolio-work"),
            (self.outcome_work, "company-outcomes"),
            (self.paper_work, "paper-execution"),
        ):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self.threads.append(thread)
        if self.monitor:
            self.monitor.start()
        self.insights.start()

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
                self.snapshots.schedule(self.insights)
                self.outlooks.schedule(self.insights)
                self.paper.schedule(self.insights)
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

    def outcome_work(self):
        while not self.stop.wait(30):
            try:
                now = time.time()
                # Leave the pre-open preparation/issuance window to the outlook worker.
                session = self.advisor_results.calendar.at(now)
                if session and session.opens - 1920 <= now < session.opens - 1500:
                    continue
                http = SignalHTTP(
                    self.config,
                    self.store,
                    automatic=True,
                    feature="advisor_results",
                    stop=self.stop,
                    deadline=now + 30,
                )
                self.advisor_results.run_once(http, now=now)
                self.store.set_state("advisor_results_error", "")
            except WorkStopped:
                pass
            except Exception:
                logger.warning("Company outcome collection deferred; saved evidence retained.")
                self.store.set_state(
                    "advisor_results_error",
                    "Outcome collection failed/deferred; saved evidence retained.",
                )

    def close(self):
        self.insights.close()
        if self.monitor:
            self.monitor.close()
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=5)

    def paper_work(self):
        while not self.stop.wait(30):
            try:
                now = time.time()
                session = self.paper.calendar.at(now)
                if session and session.opens - 1920 <= now < session.opens - 1500:
                    continue
                http = SignalHTTP(
                    self.config,
                    self.store,
                    automatic=True,
                    feature="paper",
                    stop=self.stop,
                    deadline=now + 30,
                )
                self.paper.run_once(http, now=now)
                if self.store.state("paper_error").startswith("Paper processing failed/deferred"):
                    self.store.set_state("paper_error", "")
            except WorkStopped:
                pass
            except Exception:
                logger.warning("Paper processing deferred; ledger retained.")
                self.store.set_state(
                    "paper_error", "Paper processing failed/deferred; saved ledger retained."
                )
