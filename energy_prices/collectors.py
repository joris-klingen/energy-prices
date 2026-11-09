"""The datasets collected alongside day-ahead prices, and how each is kept fresh.

Day-ahead keeps its own path in cli.py - two interchangeable sources with fallback and
a primary-health check. Everything else has one source and is described here as a
`Collector`: where its history starts, how far back each daily run re-fetches to pick
up revisions, and how old its newest row may get before that is a real problem.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl

from . import config
from .schema import COMMODITY, IMBALANCE, Dataset, empty_frame
from .sources import eex_auctions, entsoe, worldbank

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


@dataclass(frozen=True)
class Collector:
    name: str
    dataset: Dataset
    source: str
    description: str
    archive_start: date
    # Each daily run re-fetches this many days back, so revisions land in the archive.
    lookback_days: int
    # A daily run fails once the newest stored row is older than this many days.
    stale_after_days: int
    # Longest range one fetch may span; longer backfills are cut into chunks.
    chunk_days: int
    # Fetch rows for days [start, end).
    fetch: Callable[[date, date], pl.DataFrame]
    needs_entsoe_token: bool = False


def _utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def _concat(frames: list[pl.DataFrame], dataset: Dataset) -> pl.DataFrame:
    frames = [frame for frame in frames if not frame.is_empty()]
    return pl.concat(frames) if frames else empty_frame(dataset)


def _imbalance(start: date, end: date) -> pl.DataFrame:
    return _concat(
        [
            entsoe.fetch_imbalance(zone, _utc_midnight(start), _utc_midnight(end))
            for zone in config.IMBALANCE_ZONES
        ],
        IMBALANCE,
    )


COLLECTORS: dict[str, Collector] = {
    collector.name: collector
    for collector in [
        Collector(
            name="imbalance",
            dataset=IMBALANCE,
            source=entsoe.SOURCE,
            description="NL imbalance settlement prices (ENTSO-E A85)",
            archive_start=date(2015, 1, 1),
            # TenneT publishes a day the next morning and revises until about the 15th
            # of the following month.
            lookback_days=45,
            stale_after_days=4,
            chunk_days=365,
            fetch=_imbalance,
            needs_entsoe_token=True,
        ),
        Collector(
            name="gas",
            dataset=COMMODITY,
            source=worldbank.SOURCE,
            description="TTF natural gas, monthly average (World Bank Pink Sheet)",
            archive_start=worldbank.ARCHIVE_START,
            lookback_days=worldbank.LOOKBACK_DAYS,
            stale_after_days=worldbank.STALE_AFTER_DAYS,
            chunk_days=100 * 365,  # one workbook holds everything
            fetch=worldbank.fetch,
        ),
        Collector(
            name="carbon",
            dataset=COMMODITY,
            source=eex_auctions.SOURCE,
            description="EU carbon allowances, primary auction prices (EEX)",
            archive_start=eex_auctions.ARCHIVE_START,
            lookback_days=eex_auctions.LOOKBACK_DAYS,
            stale_after_days=eex_auctions.STALE_AFTER_DAYS,
            chunk_days=100 * 365,  # one request per yearly report regardless
            fetch=eex_auctions.fetch,
        ),
    ]
}
