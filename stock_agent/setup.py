"""Interactive one-time pairing; credentials never pass through Telegram."""

import getpass
import hmac
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from stock_agent.api import Telegram
from stock_agent.config import read_env


def write_credentials(path: Path, token: str, owner: int, key: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={owner}\n"
        f"ALPHA_VANTAGE_API_KEY={key}\nDATABASE_PATH=data/stock_agent.db\n"
    )
    # Atomic replacement and mode 0600, even if an existing file is more permissive.
    fd, temporary = tempfile.mkstemp(prefix=".env-setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def setup(path: Path):
    values = read_env(path) | dict(os.environ)
    token = (
        values.get("TELEGRAM_BOT_TOKEN", "").strip()
        or getpass.getpass("BotFather token (hidden): ").strip()
    )
    key = values.get("ALPHA_VANTAGE_API_KEY", "").strip()
    if not key:
        key = getpass.getpass("Alpha Vantage key (hidden; Enter to configure later): ").strip()
    # Validate without storing or exposing the supplied secrets.
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Bot token format is invalid; copy the complete token from BotFather.")
    if key and not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("API key contains invalid characters.")
    telegram = Telegram(token)
    me = telegram.call("getMe")
    if telegram.call("getWebhookInfo").get("url"):
        raise ValueError(
            "This bot has an active webhook. Use a new dedicated bot for this deployment."
        )
    challenge = "/pair " + secrets.token_hex(12)
    print(
        f"Open https://t.me/{me['username']} in Telegram and send this exact message:", flush=True
    )
    print(challenge, flush=True)
    print("Waiting up to five minutes. Stop other polling instances of this bot first.", flush=True)
    deadline, offset = time.monotonic() + 300, 0
    while time.monotonic() < deadline:
        for update in telegram.updates(offset):
            offset = update["update_id"] + 1
            message = update.get("message", {})
            chat, sender = message.get("chat", {}), message.get("from", {})
            text = message.get("text", "")
            owner = sender.get("id")
            if (
                chat.get("type") == "private"
                and isinstance(owner, int)
                and owner > 0
                and chat.get("id") == owner
                and not sender.get("is_bot", False)
                and isinstance(text, str)
                and hmac.compare_digest(text.encode(), challenge.encode())
            ):
                write_credentials(path, token, owner, key)
                print(
                    f"Credentials saved to {path} with mode 0600. Confirming Telegram delivery…",
                    flush=True,
                )
                # Acknowledge the pairing update without running the service.
                telegram.updates(offset, timeout=0)
                telegram.send(
                    owner,
                    "✅ Your private account is paired. Deploy StockPulse Sentinel, then send /status.",
                )
                print(f"Paired successfully. Credentials saved to {path} with mode 0600.")
                return
    raise ValueError("Pairing timed out; run setup again for a new code.")
