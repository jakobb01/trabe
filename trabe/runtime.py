"""Single-process polling, independent liveness, background scans and shutdown."""

import logging
import os
import queue
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from trabe import __version__
from trabe.api import ApiError, MarketData, Telegram
from trabe.bot import COMMANDS, MENU, Bot
from trabe.config import Config
from trabe.outbox import Outbox
from trabe.preferences import validate_setting
from trabe.providers import MarketRouter
from trabe.store import Store
from trabe.workers import Services

logger = logging.getLogger(__name__)


@contextmanager
def instance_lock(database: Path):
    """Stop a second poller before it consumes updates or modifies running jobs."""
    import fcntl

    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(database) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another trabe process is using this database. Stop it before starting another."
            ) from None
        yield
    finally:
        os.close(fd)


def run(
    config: Config,
    *,
    telegram: Telegram | None = None,
    market: MarketData | None = None,
    stop_event: threading.Event | None = None,
    poll_timeout: int = 25,
):
    with instance_lock(config.database):
        _run(
            config,
            telegram=telegram,
            market=market,
            stop_event=stop_event,
            poll_timeout=poll_timeout,
        )


def _run(config: Config, *, telegram, market, stop_event, poll_timeout):
    store = Store(config.database)
    for key, value in store.settings().items():
        validate_setting(key, value)
    store.interrupt_scans()
    store.set_state("last_poll", "0")
    telegram = telegram or Telegram(config.token)
    outbox = Outbox(store)
    market = market or MarketRouter(config, store)
    services = Services(store, config, market, outbox, telegram)
    bot = Bot(config, store, outbox, market, monitor=services.monitor, insights=services.insights)
    stop = stop_event if stop_event is not None else threading.Event()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        me = telegram.call("getMe")
        bot.username = me["username"]
        if telegram.call("getWebhookInfo").get("url"):
            raise ValueError(
                "This bot has an active webhook. Use a dedicated bot or remove its webhook before starting."
            )
        telegram.call(
            "setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS]
        )
        if store.state("startup_notice_version") != __version__:
            outbox.send(
                config.owner_id,
                "✅ trabe started. Use the menu below, /status or /help.",
                reply_markup=MENU,
            )
            store.set_state("startup_notice_version", __version__)
        logger.info("Telegram connected; owner-only long polling started.")
        services.start()
        backoff = 2
        while not stop.is_set():
            # Liveness measures our event loop, not an upstream provider's uptime.
            # A temporary Telegram outage should not create restart notifications.
            store.set_state("heartbeat", str(time.time()))
            try:
                bot.scans.schedule()
                while not bot.scans.results.empty():
                    try:
                        result = bot.scans.results.get_nowait()
                    except queue.Empty:
                        break
                    outbox.send(config.owner_id, result, kind=getattr(result, "kind", "reply"))
                updates = telegram.updates(
                    int(store.state("telegram_offset", "0")), timeout=poll_timeout
                )
                store.set_state("last_poll", str(time.time()))
                for update in updates:
                    if stop.is_set():
                        break
                    bot.process(update)
                backoff = 2
            except ApiError as error:
                if error.code in (401, 403, 409):
                    logger.error(
                        "Telegram authorization/polling conflict (HTTP %s). Check token, chat access and other running bot instances.",
                        error.code,
                    )
                    raise
                delay = max(backoff, error.retry_after)
                logger.warning("Telegram temporarily unavailable; retry in %s seconds.", delay)
                until = time.monotonic() + delay
                while not stop.is_set() and time.monotonic() < until:
                    stop.wait(min(60, max(0, until - time.monotonic())))
                    store.set_state("heartbeat", str(time.time()))
                backoff = min(backoff * 2, 60)
    finally:
        bot.scans.close()
        services.close()
        store.set_state("heartbeat", "0")
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        logger.info("Service stopped.")


def health(*, require_poll: bool = False) -> bool:
    """Read only; does not require secrets or make billable network requests."""
    path = Path(os.environ.get("DATABASE_PATH", "/data/stock_agent.db")).resolve()
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            state = dict(
                connection.execute(
                    "SELECT key,value FROM state WHERE key IN ('heartbeat','last_poll')"
                )
            )
            keys = ("heartbeat", "last_poll") if require_poll else ("heartbeat",)
            return all(0 <= time.time() - float(state.get(key, "0")) < 180 for key in keys)
        finally:
            connection.close()
    except (sqlite3.Error, ValueError, OSError):
        return False
