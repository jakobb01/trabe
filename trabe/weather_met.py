"""Bounded MET Locationforecast transport and summaries of its compact data."""

import bisect
import http.client
import json
import re
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from trabe import __version__

ENDPOINT = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
TERMS_URL = "https://docs.api.met.no/doc/TermsOfService"
TERMS_REVISION = "2026-11-05"
LICENSE_URL = "https://developer.yr.no/doc/License/"
SOURCE_URL = "https://api.met.no/weatherapi/locationforecast/2.0/documentation"

HOUR = 3600
REQUEST_INTERVAL = 3 * HOUR
OLDER_AFTER = 12 * HOUR
MAX_ISSUE_AGE = 36 * HOUR
MAX_WIRE = 512 * 1024
MAX_BODY = 1024 * 1024
HTTP_TIMEOUT = 6
HTTP_BUDGET = 15
REDIRECTS = {301, 302, 303, 307, 308}


def attribution():
    return [
        {"text": "MET Norway", "url": SOURCE_URL},
        {"text": "Data license", "url": LICENSE_URL},
    ]


def http_date(value):
    if not isinstance(value, str) or len(value) > 80 or not value.isascii():
        return None
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        return None
    try:
        date = parsedate_to_datetime(value)
        return date.timestamp() if date.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def retry_time(value, now):
    if value and re.fullmatch(r"[0-9]+", value):
        # Saturate before converting an arbitrarily long integer.
        return min(253402214400, now + int(value.lstrip("0")[:12] or "0"))
    return max(now, http_date(value) or now + REQUEST_INTERVAL)


class NoAutomaticRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib's default handler drains the redirect body without a size limit.
        return None


def redirect(url, location):
    target = urlsplit(urljoin(url, location))
    if (
        not location
        or target.scheme != "https"
        or target.netloc.lower() not in {"api.met.no", "api.met.no:443"}
        or target.path != urlsplit(ENDPOINT).path
        or target.fragment
        or sorted(parse_qsl(target.query, keep_blank_values=True))
        != sorted(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    ):
        raise ValueError("Unsafe MET redirect.")
    return target.geturl()


@dataclass(frozen=True)
class Response:
    status: int
    modified: str = ""
    expires: float | None = None
    retry_at: float = 0
    forecast: dict | None = None


def read_body(response, deadline):
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise ValueError("Unsupported MET encoding.")
    decoder = None
    prefix = b""
    wire = 0
    data = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError
        block = response.read1(16384)
        if not block:
            break
        wire += len(block)
        if wire > MAX_WIRE:
            raise ValueError("MET response exceeds limit.")
        if encoding and encoding != "identity" and decoder is None:
            if encoding == "gzip":
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            else:
                # HTTP deflate normally has a zlib wrapper. Accept raw deflate too.
                block = prefix + block
                if len(block) < 2:
                    prefix = block
                    continue
                wrapped = block[0] & 15 == 8 and int.from_bytes(block[:2], "big") % 31 == 0
                decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
        data.extend(decoder.decompress(block, MAX_BODY + 1 - len(data)) if decoder else block)
        if len(data) > MAX_BODY or (decoder and decoder.unused_data):
            raise ValueError("MET response exceeds limit or has trailing data.")
    if encoding in {"gzip", "deflate"} and (not decoder or not decoder.eof):
        raise ValueError("Incomplete MET response.")
    return data


def fetch(coordinates, contact, modified, now):
    lat, lon = coordinates.split(",")
    headers = {
        "User-Agent": f"trabe/{__version__} ({contact})",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }
    if (modified_at := http_date(modified)) is not None and modified_at <= now:
        headers["If-Modified-Since"] = modified
    url = f"{ENDPOINT}?lat={lat}&lon={lon}"
    deadline = time.monotonic() + HTTP_BUDGET
    try:
        # Only the MET origin receives the coordinates and service contact.
        opener = build_opener(ProxyHandler({}), NoAutomaticRedirect())
        for hop in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = opener.open(
                    Request(url, headers=headers), timeout=min(HTTP_TIMEOUT, remaining)
                )
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                if status in REDIRECTS and hop < 2:
                    url = redirect(url, response.headers.get("Location", ""))
                    continue
                return read_response(response, status, now, deadline)
    except (ValueError, http.client.HTTPException, OSError):
        pass
    return Response(0)


def read_response(response, status, now, deadline):
    expires = http_date(response.headers.get("Expires"))
    last_modified = response.headers.get("Last-Modified", "")
    if (valid_modified := http_date(last_modified)) is None or valid_modified > now:
        last_modified = ""
    forecast = None
    if status == 200:
        try:
            forecast = parse_forecast(json.loads(read_body(response, deadline)), now)
        except (
            ValueError,
            OverflowError,
            RecursionError,
            zlib.error,
            http.client.HTTPException,
            OSError,
        ):
            pass
    retry = retry_time(response.headers.get("Retry-After"), now) if status == 429 else 0
    return Response(status, last_modified, expires, retry, forecast)


def object_map(value):
    return value if isinstance(value, dict) else {}


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        instant = datetime.fromisoformat(value)
        return int(instant.timestamp()) if instant.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def measurement(value, lower, upper):
    return value if type(value) in (int, float) and lower <= value <= upper else None


def parse_forecast(payload, now):
    properties = object_map(object_map(payload).get("properties"))
    issue = timestamp(object_map(properties.get("meta")).get("updated_at"))
    series = properties.get("timeseries")
    if issue is None or issue > now + 300 or not isinstance(series, list):
        raise ValueError("Missing MET forecast issue or series.")
    samples = {}
    for point in series:
        point = object_map(point)
        start = timestamp(point.get("time"))
        if start is None or not now - 12 * HOUR <= start <= now + 72 * HOUR:
            continue
        data = object_map(point.get("data"))
        details = object_map(object_map(data.get("instant")).get("details"))
        temp = measurement(details.get("air_temperature"), -100, 70)
        wind = measurement(details.get("wind_speed"), 0, 150)
        periods = []
        for hours in (1, 6, 12):
            period = object_map(data.get(f"next_{hours}_hours"))
            amount = measurement(
                object_map(period.get("details")).get("precipitation_amount"), 0, 2000
            )
            if amount is not None:
                periods.append([hours, amount])
        if temp is not None or wind is not None or periods:
            samples[start] = [start, temp, wind, periods]
    if not samples:
        raise ValueError("Empty MET forecast.")
    return {"issue": issue, "samples": [samples[key] for key in sorted(samples)[:96]]}


def precipitation(samples, now, end):
    """Maximize covered time without counting overlapping MET periods twice."""
    intervals = sorted(
        (start + hours * HOUR, start, hours, amount)
        for start, _, _, periods in samples
        for hours, amount in periods
        if now <= start and start + hours * HOUR <= end
    )
    ends = [finish for finish, *_ in intervals]
    # Prefer finer periods when two sets cover the same amount of time.
    best = [(0, [])]
    for index, interval in enumerate(intervals):
        _, start, hours, _ = interval
        coverage, chosen = best[bisect.bisect_right(ends, start, hi=index)]
        previous_coverage, previous = best[-1]
        candidate = (coverage + hours, len(chosen) + 1)
        best.append(
            (candidate[0], [*chosen, interval])
            if candidate > (previous_coverage, len(previous))
            else best[-1]
        )
    return best[-1]


def point_coverage(points, now, end):
    edges = [now, *(stamp for stamp, _ in points), end]
    return bool(points) and all(b - a <= 6 * HOUR for a, b in zip(edges, edges[1:]))


def forecast_expiry(forecast):
    return min(forecast["issue"] + MAX_ISSUE_AGE, forecast["samples"][-1][0])


def summarize(forecast, label, zone, now, *, cached=False):
    if not forecast or now >= forecast_expiry(forecast):
        return None
    end = now + 24 * HOUR
    samples = forecast["samples"]
    temps, winds = (
        [
            (sample[0], sample[index])
            for sample in samples
            if now <= sample[0] <= end and sample[index] is not None
        ]
        for index in (1, 2)
    )
    coverage, periods = precipitation(samples, now, end)
    rain = sum(period[3] for period in periods)
    if not temps and not winds and not coverage:
        return None

    def local(stamp):
        return datetime.fromtimestamp(stamp, zone).strftime("%a %d %b %H:%M %Z")

    title = "🌦 Weather" + (f" • {label}" if label else "")
    if temps:
        values = [value for _, value in temps]
        temp = (
            f"{min(values):g} to {max(values):g} °C"
            if min(values) != max(values)
            else f"{values[0]:g} °C"
        )
    else:
        temp = "unavailable"
    rain_text = f"{rain:g} mm across {coverage} h" if coverage else "unavailable"
    wind = f"up to {max(value for _, value in winds):g} m/s" if winds else "unavailable"
    lines = [title, f"{local(now)} → {local(end)}", f"🌡 {temp} · 🌧 {rain_text} · 💨 {wind}"]
    if rain > 0:
        finish, start, hours, amount = max(periods, key=lambda period: period[3] / period[2])
        cue = (
            "Wettest covered window"
            if hours == 1
            else f"Wettest covered window ({hours} h estimate)"
        )
        lines.append(f"{cue} • {local(start)} → {local(finish)} · {amount:g} mm")
    if coverage < 24 or not all(point_coverage(points, now, end) for points in (temps, winds)):
        lines.append("Partial forecast coverage.")
    age = " · older forecast" if now - forecast["issue"] >= OLDER_AFTER else ""
    lines.append(f"Issued {local(forecast['issue'])}{age}" + (" · saved data" if cached else ""))
    lines.append("Summarized from MET Norway data.")
    return "\n".join(lines)
