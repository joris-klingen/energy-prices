"""One retrying GET for the sources added after the day-ahead pair.

It draws the same line the day-ahead clients do: connection errors, 5xx and rate
limiting that outlast the retries are `SourceUnavailable`; any other response is handed
back for the caller to judge, so a 4xx can still fail loudly.
"""

from __future__ import annotations

import logging
import time

import requests

from ..errors import SourceUnavailable

log = logging.getLogger(__name__)

BACKOFF_SECONDS = 4
RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = "energy-prices archive (+https://github.com/joris-klingen/energy-prices)"


def get(
    url: str,
    params: dict | None = None,
    session: requests.Session | None = None,
    max_retries: int = 4,
    timeout: float = 120,
    headers: dict | None = None,
) -> requests.Response:
    session = session or requests.Session()
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt in range(max_retries):
        last = attempt == max_retries - 1
        try:
            response = session.get(url, params=params, timeout=timeout, headers=headers)
        except requests.RequestException as exc:
            if last:
                raise SourceUnavailable(f"{url} unreachable: {exc}") from exc
            log.warning("%s, retrying", exc.__class__.__name__)
            time.sleep(BACKOFF_SECONDS * 2**attempt)
            continue
        if response.status_code in RETRY_STATUSES:
            if last:
                raise SourceUnavailable(f"{url} returned {response.status_code} on {max_retries} tries")
            wait = BACKOFF_SECONDS * 2**attempt
            if response.status_code == 429:
                wait = int(response.headers.get("retry-after", wait)) + 1
            log.warning("HTTP %s, retrying in %ss", response.status_code, wait)
            time.sleep(wait)
            continue
        return response
    raise AssertionError("unreachable")
