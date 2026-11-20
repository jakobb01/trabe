"""Weather cards and deliberately initiated owner input flows."""

import math
import re
import time
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trabe import weather_met as met
from trabe.weather import (
    DAY_NAMES,
    PREVIEW_WINDOW,
    PROMPT_WINDOW,
    SLOT_WINDOW,
    access_problem,
    cache_capacity,
    cached,
    forecast_card,
    guard_problem,
    latest_slot,
    local_stamp,
    next_slot,
    preferences,
    provider_status,
    slot_available,
    update_preferences,
)

PROMPTS = {
    "coordinates": (
        "Send a static pin with Telegram’s attachment menu, or type latitude and longitude "
        "such as 46.05, 14.50. The selected coordinates will be shared with MET Norway on "
        "forecast requests after you accept its terms and add a contact."
    ),
    "label": "Type a short label for this location, up to 48 characters.",
    "times": "Type one to four distinct times in 24-hour HH:MM form, e.g. 07:00 12:30 18:00.",
    "timezone": "Type an IANA timezone, e.g. Europe/Ljubljana, America/New_York or UTC.",
    "contact": (
        "Type a real service email or HTTPS contact page where MET Norway can reach you. "
        "The contact is sent to MET in the request header."
    ),
}
FORWARDED = {
    "forward_origin",
    "forward_date",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "via_bot",
    "is_automatic_forward",
}
COORDINATE = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
TIMES = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", re.I)
EMAIL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
EXAMPLE_DOMAINS = (
    "example.com",
    "example.net",
    "example.org",
    "example",
    "invalid",
    "localhost",
    "test",
)


def coordinates_input(message):
    location = message.get("location")
    if (
        isinstance(location, dict)
        and not any(
            field in location for field in ("live_period", "heading", "proximity_alert_radius")
        )
        and "venue" not in message
    ):
        values = (location.get("latitude"), location.get("longitude"))
        if not all(
            type(value) is int or (type(value) is float and math.isfinite(value))
            for value in values
        ):
            values = ()
    else:
        text = message.get("text", "").strip()
        match = (
            re.fullmatch(rf"({COORDINATE})(?:\s*,\s*|\s+)({COORDINATE})", text)
            if len(text) <= 80
            else None
        )
        values = match.groups() if match else ()
    if not values:
        raise ValueError("Send a static pin or type latitude, longitude.")
    latitude, longitude = (Decimal(str(value)) for value in values)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Latitude must be within ±90° and longitude within ±180°.")
    rounded = [
        value.quantize(Decimal("0.0001"), rounding=ROUND_DOWN) for value in (latitude, longitude)
    ]
    return ",".join(f"{value or Decimal(0):.4f}" for value in rounded)


def contact_input(text):
    parsed = urlsplit(text)
    email = re.fullmatch(rf"({EMAIL_ATOM}(?:\.{EMAIL_ATOM})*)@([A-Za-z0-9.-]+)", text)
    host = ""
    if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
        if parsed.netloc.lower() in {parsed.hostname, f"{parsed.hostname}:443"}:
            host = parsed.hostname or ""
    elif email and len(email[1]) <= 64:
        host = email[2].lower()
    if (
        not 0 < len(text) <= 254
        or any(not 33 <= ord(char) <= 126 for char in text)
        or not DOMAIN.fullmatch(host)
        or any(host == domain or host.endswith("." + domain) for domain in EXAMPLE_DOMAINS)
    ):
        raise ValueError("Enter a real service email or public HTTPS contact page.")
    return text


def parse_input(prompt, message):
    if FORWARDED.intersection(message):
        raise ValueError("Send the requested information directly.")
    if prompt == "coordinates":
        return coordinates_input(message)
    text = message.get("text", "").strip()
    if prompt == "label":
        if not 0 < len(text) <= 48 or not text.isprintable():
            raise ValueError("Use a label of 1–48 printable characters.")
        return text
    if prompt == "times":
        slots = re.split(r"\s*,\s*|\s+", text)
        if not (
            1 <= len(slots) <= 4
            and len(set(slots)) == len(slots)
            and all(TIMES.fullmatch(slot) for slot in slots)
        ):
            raise ValueError("Use one to four distinct HH:MM times.")
        return " ".join(sorted(slots))
    if prompt == "timezone":
        if len(text) <= 64 and re.fullmatch(
            r"(?!posix/|right/)[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", text
        ):
            try:
                return ZoneInfo(text).key
            except ZoneInfoNotFoundError:
                pass
        raise ValueError("Use an IANA timezone such as Europe/Ljubljana or UTC.")
    if prompt == "contact":
        try:
            return contact_input(text)
        except ValueError:
            raise ValueError("Enter a real service email or public HTTPS contact page.") from None
    raise ValueError("Open Weather to start again.")


def accept_input(store, message, now=None):
    now = time.time() if now is None else now
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        settings = preferences(db)
        prompt = settings["prompt"]
        if not prompt or ("location" not in message and not isinstance(message.get("text"), str)):
            return None
        if settings["prompt_until"] <= now:
            update_preferences(db, now)
            return "Weather setup expired. Choose the setting again."
        try:
            value = parse_input(prompt, message)
        except ValueError as error:
            return str(error)
        values = {prompt: value}
        if prompt == "coordinates" and value != settings["coordinates"]:
            values["label"] = ""
        update_preferences(db, now, **values)
    return {
        "coordinates": "Location saved. You can add a label in Location.",
        "contact": "Provider contact saved.",
        "label": "Location label saved.",
        "times": "Delivery times saved.",
        "timezone": "Weather timezone saved.",
    }[prompt]


def apply_action(db, owner, value, now):
    generation, _, action = value.partition(":")
    settings = preferences(db)
    if generation != str(settings["generation"]):
        return "", "Weather settings changed. Use the updated controls below."
    panel = ""
    if action.startswith("prompt_"):
        prompt = action.removeprefix("prompt_")
        if prompt not in PROMPTS:
            return "", "Choose a setting below."
        if prompt == "label" and not settings["coordinates"]:
            return "location", "Set a location first."
        db.execute(
            "UPDATE weather_settings SET prompt=?,prompt_until=? WHERE id=1",
            (prompt, now + PROMPT_WINDOW),
        )
        return "input", ""
    if action == "cancel":
        update_preferences(db, now)
        return "", "Weather setup cancelled."
    if action == "preview":
        problem = access_problem(settings, owner)
        if problem:
            return "preview", problem
        cache = cached(db, settings["coordinates"])
        paused = guard_problem(db, now)
        if (
            not paused
            and now >= max(cache.next_at, settings["throttle_until"])
            and cache_capacity(db, cache, now)
        ):
            db.execute(
                "UPDATE weather_settings SET preview_until=? WHERE id=1", (now + PREVIEW_WINDOW,)
            )
            return "preview", "Preview queued. The result will arrive in this chat."
        return "preview", paused
    if action == "on":
        if problem := access_problem(settings, owner):
            return "", problem
        update_preferences(db, now, enabled=1)
    elif action == "off":
        update_preferences(db, now, enabled=0)
    elif action == "clear":
        update_preferences(db, now, coordinates="", label="", enabled=0)
        panel = "location"
    elif action == "clear_label":
        update_preferences(db, now, label="")
        panel = "location"
    elif action == "accept":
        update_preferences(db, now, accepted_by=owner, terms=met.TERMS_REVISION)
        panel = "provider"
    elif action == "withdraw":
        update_preferences(db, now, accepted_by=0, terms="", enabled=0)
        panel = "provider"
    elif action == "clear_contact":
        update_preferences(db, now, contact="", enabled=0)
        panel = "provider"
    elif re.fullmatch(r"day_[0-6]", action):
        days = settings["days"] ^ (1 << int(action[-1]))
        if not days:
            return "days", "Keep at least one day selected."
        update_preferences(db, now, days=days)
        panel = "days"
    return panel, ""


def render_weather(store, owner, panel, now, button):
    with store.connection() as db:
        settings = preferences(db)
        cache = cached(db, settings["coordinates"])
        contact = bool(settings["contact"])
        accepted = settings["accepted_by"] == owner and settings["terms"] == met.TERMS_REVISION
        location = settings["label"] or ("Saved" if settings["coordinates"] else "Unset")
        problem = access_problem(settings, owner)
        guard = guard_problem(db, now)
        provider = provider_status(db, settings, cache, now) if settings["coordinates"] else ""
        upcoming = (
            next_slot(db, settings, now) if settings["enabled"] else "Automatic delivery is off"
        )
        previous = latest_slot(settings, now) if settings["enabled"] else None
        missed = previous and now >= previous.stamp + SLOT_WINDOW and slot_available(db, previous)
        error = db.execute("SELECT value FROM state WHERE key='weather_error'").fetchone()

    def change(label, action):
        return button(label, "weather_do", value=f"{settings['generation']}:{action}")

    def view(label, target=""):
        return button(label, "weather", value=target)

    rows = []
    if panel == "input" and settings["prompt"] and settings["prompt_until"] > now:
        text = f"Weather setup\n{PROMPTS[settings['prompt']]}\nThis prompt expires in 5 minutes."
        rows = [[change("Cancel setup", "cancel")]]
    elif panel == "location":
        text = f"Weather location\n{location}\nOne static location is used for all forecasts."
        rows = [[change("Set location", "prompt_coordinates")]]
        if settings["coordinates"]:
            rows.extend(
                [[change("Set label", "prompt_label")], [change("Clear location", "clear")]]
            )
        if settings["label"]:
            rows[1].append(change("Clear label", "clear_label"))
    elif panel == "provider":
        text = (
            "Weather provider • MET Norway\n"
            f"Terms {'accepted' if accepted else 'awaiting owner acceptance'} · "
            f"service contact {'saved' if contact else 'unset'}.\n"
            "MET receives your selected coordinates and the service contact in requests. "
            "Read the terms before accepting them."
        )
        if provider:
            text += f"\n{provider}"
        rows = [
            [{"text": "MET terms", "url": met.TERMS_URL}],
            [
                change("Withdraw acceptance", "withdraw")
                if accepted
                else change("I accept the MET terms", "accept")
            ],
            [change("Change contact" if contact else "Set contact", "prompt_contact")],
        ]
        if contact:
            rows[-1].append(change("Clear contact", "clear_contact"))
    elif panel == "days":
        text = "Weather delivery days\nSelect the days to use all saved times."
        buttons = [
            change(("✓ " if settings["days"] & (1 << index) else "○ ") + name, f"day_{index}")
            for index, name in enumerate(DAY_NAMES)
        ]
        rows = [buttons[:4], buttons[4:]]
    elif panel == "preview":
        forecast = forecast_card(settings, cache, now) if not problem else None
        text = forecast or "🌦 Weather preview\nNo usable saved forecast."
        if note := (problem or provider):
            text += "\n\n" + note
        if forecast:
            rows.append(met.attribution())
        rows.append([change("Check forecast", "preview")])
    else:
        days = " ".join(
            name for index, name in enumerate(DAY_NAMES) if settings["days"] & (1 << index)
        )
        times = ", ".join(settings["times"].split())
        text = f"🌦 Weather\nLocation • {location}\nSchedule • {days} at {times} ({settings['timezone']})"
        text += f"\nAutomatic delivery • {'On' if settings['enabled'] else 'Off'}"
        if settings["enabled"]:
            text += f"\nNext • {upcoming}"
        if problem or provider:
            text += f"\nProvider • {problem or provider}"
        if guard:
            text += f"\n{guard}"
        if cache.next_at > now and settings["coordinates"] and not problem:
            text += f"\nNext MET request allowed • {local_stamp(max(cache.next_at, settings['throttle_until']), settings)}"
        if missed:
            text += f"\n{previous.hhmm} skipped • the 30-minute delivery window elapsed."
        elif settings["delivery_status"]:
            text += "\n" + settings["delivery_status"]
        if error and error[0]:
            text += "\n" + error[0]
        rows = [
            [change("Forecast preview", "preview"), view("Location", "location")],
            [
                change("Set times", "prompt_times"),
                view("Days", "days"),
                change("Set timezone", "prompt_timezone"),
            ],
            [
                change(
                    "Turn off" if settings["enabled"] else "Turn on",
                    "off" if settings["enabled"] else "on",
                ),
                view("Provider setup", "provider"),
            ],
            [button("Reports", "reports")],
        ]
        if settings["prompt"] and settings["prompt_until"] > now:
            rows.insert(0, [view("Open active prompt", "input"), change("Cancel setup", "cancel")])
        return text, rows
    rows.append([view("Weather")])
    return text, rows
