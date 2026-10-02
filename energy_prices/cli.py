"""Command line entry points: backfill the archive, or run the daily top-up."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl
import requests

from . import config, store
from .sources import DEFAULT, SOURCES, entsoe

log = logging.getLogger("energy_prices")
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def _utc_midnight(day: date) -> datetime:
    """Start of a Dutch delivery day, expressed in UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def _fetch(source: str, zone: str, start: date, end: date) -> pl.DataFrame:
    """Fetch delivery days [start, end) from one source."""
    if source == entsoe.SOURCE:
        # ENTSO-E takes UTC instants rather than delivery days.
        return entsoe.fetch_day_ahead(zone, _utc_midnight(start), _utc_midnight(end))
    return SOURCES[source].fetch_day_ahead(zone, start, end)


def collect(source: str, zones: list[str], start: date, end: date) -> int:
    """Fetch and store prices for delivery days [start, end)."""
    chunk_days = SOURCES[source].CHUNK_DAYS
    pause = getattr(SOURCES[source], "PAUSE_SECONDS", 0.0)
    total = 0
    first_request = True

    for zone in zones:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=chunk_days), end)
            if not first_request and pause:
                time.sleep(pause)
            first_request = False

            log.info("fetching %s %s %s..%s", source, zone, cursor, chunk_end)
            frame = _fetch(source, zone, cursor, chunk_end)
            log.info("  %s price points", frame.height)
            if not frame.is_empty():
                for path, rows in store.write_frame(frame).items():
                    log.info("  %s -> %s rows", path, rows)
                total += frame.height
            cursor = chunk_end
    return total


def check_token() -> int:
    """Verify the ENTSO-E API token with a one-day request and report what came back."""
    try:
        token = config.api_token()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    log.info("token found (%s characters)", len(token))

    yesterday = datetime.now(LOCAL_TZ).date() - timedelta(days=1)
    try:
        frame = entsoe.fetch_day_ahead(
            "NL", _utc_midnight(yesterday), _utc_midnight(yesterday + timedelta(days=1)), token
        )
    except RuntimeError as exc:
        log.error("%s", exc)
        log.error("the platform knows the account but has not enabled API access for it; "
                  "email transparency@entsoe.eu with subject 'Restful API access'")
        return 1
    except requests.RequestException as exc:
        log.error("could not reach %s: %s", entsoe.API_URL, exc)
        return 1

    if frame.is_empty():
        log.error("the API answered but returned no prices for %s - unexpected for NL", yesterday)
        return 1

    log.info("OK: %s price points for NL on %s", frame.height, yesterday)
    for row in frame.head(3).iter_rows(named=True):
        log.info("  %s %s %7.2f EUR/MWh",
                 row["mtu_start_utc"], row["resolution"], row["price_eur_mwh"])
    log.info("the token works. Next: energy-prices backfill --source entsoe")
    return 0


def _summary() -> None:
    for source, zone, resolution, rows, first, last in store.coverage():
        log.info("coverage %s %s %s: %s rows, %s .. %s", source, zone, resolution, rows, first, last)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="energy-prices")
    parser.add_argument(
        "command", choices=["check-token", "backfill", "daily", "build-db", "coverage"]
    )
    parser.add_argument("--source", choices=sorted(SOURCES), default=DEFAULT)
    parser.add_argument("--zones", nargs="*", default=list(config.ZONES))
    parser.add_argument("--start", type=date.fromisoformat, help="first delivery day (backfill)")
    parser.add_argument("--end", type=date.fromisoformat, help="exclusive last delivery day")
    parser.add_argument("--lookback", type=int, default=config.LOOKBACK_DAYS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    today = datetime.now(LOCAL_TZ).date()

    if args.command == "check-token":
        return check_token()

    if args.command == "backfill":
        start = args.start or SOURCES[args.source].ARCHIVE_START
        collect(args.source, args.zones, start, args.end or today + timedelta(days=2))
    elif args.command == "daily":
        # Re-fetch a short window so late corrections are picked up, and reach into
        # tomorrow because D+1 prices are published around 12:45 CET.
        start = args.start or today - timedelta(days=args.lookback)
        collect(args.source, args.zones, start, args.end or today + timedelta(days=2))
    elif args.command == "coverage":
        _summary()
        return 0

    log.info("building duckdb at %s", store.build_duckdb())
    _summary()
    return 0
