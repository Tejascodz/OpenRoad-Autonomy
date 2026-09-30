"""Bounded, well-behaved Overpass requests for OSMnx.

OSMnx 2.1's internal `_overpass_request` retries HTTP 429/504 recursively *forever*
(55 s apart). When the public servers are overloaded, the map download therefore
never finishes and never fails. It also caches responses whose body says
"runtime error: Query timed out", i.e. incomplete road networks.

`install()` replaces that one internal function with a version that:
  * waits for a free Overpass slot (reads /status on servers that provide it),
  * retries busy responses a few times, then raises OverpassBusy,
  * never caches incomplete ("runtime error") answers,
  * keeps OSMnx's on-disk response cache (successful pieces are never downloaded twice).
Pinned to osmnx==2.1.1; tests fail loudly if the internal API changes.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any, Callable

import requests

from ..config import logger

BUSY_CODES = {429, 502, 503, 504}
RETRY_WAITS_S = (15, 40)          # after 1st and 2nd busy answer
MAX_SLOT_WAIT_S = 90
_SLOT_RE = re.compile(r"in (\d+) seconds")
_FREE_RE = re.compile(r"(\d+) slots? available now")


class OverpassBusy(Exception):
    """All attempts on this server were refused or timed out."""


def slot_wait_seconds(base_url: str, timeout=(6, 10)) -> float:
    """Seconds until a query slot is free on servers exposing /status (0 if unknown/now)."""
    try:
        r = requests.get(base_url.rstrip("/") + "/status", timeout=timeout)
        if r.status_code != 200:
            return 0.0
        text = r.text
    except requests.RequestException:
        return 0.0
    free = _FREE_RE.search(text)
    if free and int(free.group(1)) > 0:
        return 0.0
    waits = [int(m) for m in _SLOT_RE.findall(text)]
    return float(min(waits)) if waits else 0.0


def make_bounded_request(ox_settings, http, sleep: Callable[[float], None] = time.sleep,
                         post: Callable[..., Any] = requests.post) -> Callable[[OrderedDict], dict]:
    def bounded_overpass_request(data: "OrderedDict[str, Any]") -> dict:
        base = ox_settings.overpass_url.rstrip("/")
        url = base + "/interpreter"
        prepared_url = str(requests.Request("GET", url, params=data).prepare().url)
        cached = http._retrieve_from_cache(prepared_url)
        if isinstance(cached, dict) and not _incomplete(cached):
            return cached
        last = "no attempt"
        for attempt in range(len(RETRY_WAITS_S) + 1):
            wait = min(MAX_SLOT_WAIT_S, slot_wait_seconds(base)) if "overpass-api.de" in base else 0.0
            if wait:
                logger.info("Overpass: waiting %.0f s for a free query slot on %s", wait, base)
                sleep(wait)
            try:
                resp = post(url, data=data, timeout=ox_settings.requests_timeout,
                            headers=http._get_http_headers())
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}"
            else:
                if resp.status_code in BUSY_CODES:
                    last = f"HTTP {resp.status_code}"
                elif resp.status_code != 200:
                    raise OverpassBusy(f"{base} answered HTTP {resp.status_code}")
                else:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    if not isinstance(body, dict):
                        last = "invalid JSON"
                    elif _incomplete(body):
                        last = "server-side timeout (incomplete answer)"
                    else:
                        http._save_to_cache(prepared_url, body, True)
                        return body
            if attempt < len(RETRY_WAITS_S):
                logger.warning("Overpass %s busy (%s) - retry %d in %d s", base, last, attempt + 1,
                               RETRY_WAITS_S[attempt])
                sleep(RETRY_WAITS_S[attempt])
        raise OverpassBusy(f"{base}: {last}")

    return bounded_overpass_request


def _incomplete(body: dict) -> bool:
    remark = str(body.get("remark", ""))
    return "runtime error" in remark or "timed out" in remark.lower()


def install(ox) -> None:
    """Swap OSMnx's unbounded Overpass request for the bounded one (idempotent)."""
    from osmnx import _http, _overpass

    if getattr(_overpass._overpass_request, "_openroad", False):
        return
    fn = make_bounded_request(ox.settings, _http)
    fn._openroad = True  # type: ignore[attr-defined]
    _overpass._overpass_request = fn
    ox.settings.overpass_rate_limit = False  # handled above, per server
