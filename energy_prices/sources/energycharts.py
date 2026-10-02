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

import polars as pl
import requests

from .. import config
from ..schema import empty_frame, validate

log = logging.getLogger(__name__)

SOURCE = "energy-charts"
API_URL = "https://api.energy-charts.info/price"

# The service answers a burst of large requests with 429 and a Retry-After header.
# One pause between requests keeps a backfill under the limit in the first place.
PAUSE_SECONDS = 10.0
DEFAULT_RETRY_AFTER = 30

# Five years comes back in a single response, so chunk generously.
CHUNK_DAYS = 3 * 365

RESOLUTION_BY_SECONDS = {900: "PT15M", 1800: "PT30M", 3600: "PT60M"}

# Earliest delivery day the NL series covers.
ARCHIVE_START = date(2015, 1, 1)


def parse_price_series(payload: dict, zone: str, retrieved_at: datetime) -> pl.DataFrame:
    """Turn the parallel timestamp/price arrays into validated price rows.

    Each point's resolution is the gap to the next timestamp; the final point inherits
    the gap before it, since there is nothing after it to measure against. Spacing is
    measured before missing prices are dropped, so a gap in the series does not make
    the surrounding points look longer than they are.
    """
    stamps = payload.get("unix_seconds") or []
    prices = payload.get("price") or []
    if len(stamps) != len(prices):
        raise ValueError(f"{len(stamps)} timestamps but {len(prices)} prices")
    if not stamps:
        return empty_frame()

    unit = payload.get("unit", "EUR / MWh")
    if "MWh" not in unit:
        raise ValueError(f"expected a EUR/MWh series, got {unit!r}")

    frame = (
        pl.DataFrame(
            {
                "stamp": pl.Series(stamps, dtype=pl.Int64),
                "price": pl.Series(prices, dtype=pl.Float64),
            }
        )
        .with_columns(seconds=pl.col("stamp").shift(-1) - pl.col("stamp"))
        .with_columns(seconds=pl.col("seconds").fill_null(strategy="forward").fill_null(3600))
        .drop_nulls("price")
    )
    if frame.is_empty():
        return empty_frame()

    unknown = sorted(set(frame["seconds"].to_list()) - set(RESOLUTION_BY_SECONDS))
    if unknown:
        raise ValueError(f"unexpected spacing of {unknown[0]}s between price points")

    start = pl.from_epoch(pl.col("stamp"), time_unit="s").dt.replace_time_zone("UTC")
    end = pl.from_epoch(pl.col("stamp") + pl.col("seconds"), time_unit="s")
    return validate(
        frame.select(
            source=pl.lit(SOURCE),
            bidding_zone=pl.lit(zone),
            eic_code=pl.lit(config.ZONES.get(zone, "")),
            mtu_start_utc=start,
            mtu_end_utc=end.dt.replace_time_zone("UTC"),
            resolution=pl.col("seconds").replace_strict(RESOLUTION_BY_SECONDS),
            price_eur_mwh=pl.col("price"),
            currency=pl.lit("EUR"),
            unit=pl.lit("MWH"),
            source_reference=pl.lit(""),
            revision_number=pl.lit(0),
            retrieved_at_utc=pl.lit(retrieved_at),
        )
    )


def fetch_day_ahead(
    zone: str,
    start: date,
    end: date,
    session: requests.Session | None = None,
    max_retries: int = 4,
) -> pl.DataFrame:
    """Fetch day-ahead prices for the local delivery days [start, end).

    The API reads both of its own bounds as inclusive, so the caller's exclusive end is
    translated here and the rest of the codebase keeps one convention.
    """
    session = session or requests.Session()
    params = {
        "bzn": zone,
        "start": start.isoformat(),
        "end": (end - timedelta(days=1)).isoformat(),
    }

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
            return empty_frame()
        if response.status_code == 429 and attempt < max_retries - 1:
            wait = int(response.headers.get("retry-after", DEFAULT_RETRY_AFTER)) + 1
            log.warning("rate limited, waiting %ss", wait)
            time.sleep(wait)
            continue
        if response.status_code >= 500 and attempt < max_retries - 1:
            time.sleep(2 ** (attempt + 1))
            continue
        response.raise_for_status()
    return empty_frame()
