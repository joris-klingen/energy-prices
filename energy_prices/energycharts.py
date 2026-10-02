"""Client for the Energy-Charts API (Fraunhofer ISE), CC BY 4.0, no token required.

The endpoint returns parallel arrays of unix timestamps and prices with no resolution
field, so the market time unit is inferred from the spacing between timestamps. NL is
hourly until the EU-wide 15-minute MTU change on 2025-10-01 and quarter-hourly after,
and a range spanning the change comes back with both spacings in one response.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import requests

from . import config
from .model import PricePoint

log = logging.getLogger(__name__)

SOURCE = "energy-charts"
API_URL = "https://api.energy-charts.info/price"

# The service answers a burst of large requests with 429 and a Retry-After header.
# One pause between requests keeps a backfill under the limit in the first place.
PAUSE_SECONDS = 10.0
DEFAULT_RETRY_AFTER = 30

RESOLUTION_BY_SECONDS = {900: "PT15M", 1800: "PT30M", 3600: "PT60M"}

# Earliest delivery day the NL series covers.
ARCHIVE_START = date(2015, 1, 1)


def _resolution(seconds: int) -> str:
    name = RESOLUTION_BY_SECONDS.get(seconds)
    if name is None:
        raise ValueError(f"unexpected spacing of {seconds}s between price points")
    return name


def parse_price_series(payload: dict, zone: str, retrieved_at: datetime) -> list[PricePoint]:
    """Turn the parallel timestamp/price arrays into price points.

    Each point's resolution is the gap to the next timestamp; the final point inherits
    the gap before it, since there is nothing after it to measure against.
    """
    stamps = payload.get("unix_seconds") or []
    prices = payload.get("price") or []
    if len(stamps) != len(prices):
        raise ValueError(f"{len(stamps)} timestamps but {len(prices)} prices")
    if not stamps:
        return []

    unit = payload.get("unit", "EUR / MWh")
    if "MWh" not in unit:
        raise ValueError(f"expected a EUR/MWh series, got {unit!r}")

    points = []
    for index, (stamp, price) in enumerate(zip(stamps, prices)):
        if price is None:
            continue
        if index + 1 < len(stamps):
            seconds = stamps[index + 1] - stamps[index]
        else:
            seconds = stamps[index] - stamps[index - 1] if index else 3600
        start = datetime.fromtimestamp(stamp, timezone.utc)
        points.append(
            PricePoint(
                source=SOURCE,
                bidding_zone=zone,
                eic_code=config.ZONES.get(zone, ""),
                mtu_start_utc=start,
                mtu_end_utc=start + timedelta(seconds=seconds),
                resolution=_resolution(seconds),
                price_eur_mwh=float(price),
                currency="EUR",
                unit="MWH",
                source_reference="",
                revision_number=0,
                retrieved_at_utc=retrieved_at,
            )
        )
    return points


def fetch_day_ahead(
    zone: str,
    start: date,
    end: date,
    session: requests.Session | None = None,
    max_retries: int = 4,
) -> list[PricePoint]:
    """Fetch day-ahead prices for the local delivery days [start, end].

    Both bounds are inclusive, which is how the API itself reads them.
    """
    session = session or requests.Session()
    params = {"bzn": zone, "start": start.isoformat(), "end": end.isoformat()}

    for attempt in range(max_retries):
        try:
            response = session.get(API_URL, params=params, timeout=180)
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                raise
            log.warning("%s, retrying", exc.__class__.__name__)
            time.sleep(2 ** (attempt + 1))
            continue

        if response.status_code == 200:
            return parse_price_series(response.json(), zone, datetime.now(timezone.utc))
        if response.status_code == 404:
            log.info("no data for %s %s..%s", zone, start, end)
            return []
        if response.status_code == 429 and attempt < max_retries - 1:
            wait = int(response.headers.get("retry-after", DEFAULT_RETRY_AFTER)) + 1
            log.warning("rate limited, waiting %ss", wait)
            time.sleep(wait)
            continue
        if response.status_code >= 500 and attempt < max_retries - 1:
            time.sleep(2 ** (attempt + 1))
            continue
        response.raise_for_status()
    return []
