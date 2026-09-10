"""Command line entry points: backfill the archive, or run the daily top-up."""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, store
from .entsoe import fetch_day_ahead

log = logging.getLogger("energy_prices")
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# ENTSO-E holds day-ahead prices from the start of the SDAC publication history.
ARCHIVE_START = date(2015, 1, 1)


def _utc_midnight(day: date) -> datetime:
    """Start of a Dutch delivery day, expressed in UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def collect(zones: list[str], start: date, end: date) -> int:
    """Fetch and store prices for delivery days [start, end)."""
    token = config.api_token()
    total = 0
    for zone in zones:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=config.MAX_QUERY_DAYS), end)
            log.info("fetching %s %s..%s", zone, cursor, chunk_end)
            points = fetch_day_ahead(zone, _utc_midnight(cursor), _utc_midnight(chunk_end), token)
            log.info("  %s price points", len(points))
            if points:
                for path, rows in store.write_points(points).items():
                    log.info("  %s -> %s rows", path, rows)
                total += len(points)
            cursor = chunk_end
    return total


def _summary() -> None:
    for zone, resolution, rows, first, last in store.coverage():
        log.info("coverage %s %s: %s rows, %s .. %s", zone, resolution, rows, first, last)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="energy_prices")
    parser.add_argument("command", choices=["backfill", "daily", "build-db", "coverage"])
    parser.add_argument("--zones", nargs="*", default=list(config.ZONES))
    parser.add_argument("--start", type=date.fromisoformat, help="first delivery day (backfill)")
    parser.add_argument("--end", type=date.fromisoformat, help="exclusive last delivery day")
    parser.add_argument("--lookback", type=int, default=config.LOOKBACK_DAYS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    today = datetime.now(LOCAL_TZ).date()

    if args.command == "backfill":
        start = args.start or ARCHIVE_START
        end = args.end or today + timedelta(days=2)
        collect(args.zones, start, end)
    elif args.command == "daily":
        # Re-fetch a short window so late corrections are picked up, and reach into
        # tomorrow because D+1 prices are published around 12:45 CET.
        start = args.start or today - timedelta(days=args.lookback)
        end = args.end or today + timedelta(days=2)
        collect(args.zones, start, end)
    elif args.command == "coverage":
        _summary()
        return 0

    if args.command != "coverage":
        log.info("building duckdb at %s", store.build_duckdb())
        _summary()
    return 0
