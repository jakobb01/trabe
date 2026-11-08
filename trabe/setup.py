"""Interactive one-time pairing; credentials never pass through Telegram."""

import getpass
import hmac
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from trabe.api import Telegram
from trabe.config import read_env


def write_credentials(
    path: Path,
    token: str,
    owner: int,
    key: str,
    finnhub_key: str = "",
    massive_key: str = "",
    marketaux_key: str = "",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={owner}\n"
        f"ALPHA_VANTAGE_API_KEY={key}\nFINNHUB_API_KEY={finnhub_key}\n"
        f"MASSIVE_API_KEY={massive_key}\nMARKETAUX_API_KEY={marketaux_key}\n"
        "DATABASE_PATH=data/stock_agent.db\n"
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
    finnhub_key = values.get("FINNHUB_API_KEY", "").strip()
    massive_key = values.get("MASSIVE_API_KEY", "").strip()
    marketaux_key = values.get("MARKETAUX_API_KEY", "").strip()
    for name, value in (("MASSIVE_API_KEY", massive_key), ("MARKETAUX_API_KEY", marketaux_key)):
        if value and not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError(f"{name} contains invalid characters.")
    if not key:
        key = getpass.getpass("Alpha Vantage key (hidden; Enter to configure later): ").strip()
    # Validate without storing or exposing the supplied secrets.
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Bot token format is invalid; copy the complete token from BotFather.")
    if key and not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("API key contains invalid characters.")
    if finnhub_key and not re.fullmatch(r"[A-Za-z0-9_-]+", finnhub_key):
        raise ValueError("Finnhub API key contains invalid characters.")
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
                write_credentials(path, token, owner, key, finnhub_key, massive_key, marketaux_key)
                print(
                    f"Credentials saved to {path} with mode 0600. Confirming Telegram delivery…",
                    flush=True,
                )
                # Acknowledge the pairing update without running the service.
                telegram.updates(offset, timeout=0)
                telegram.send(
                    owner,
                    "✅ Your private account is paired. Deploy trabe, then send /status.",
                )
                print(f"Paired successfully. Credentials saved to {path} with mode 0600.")
                return
    raise ValueError("Pairing timed out; run setup again for a new code.")
