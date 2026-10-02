"""Command line entry points: backfill the archive, or run the daily top-up."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from . import config, energycharts, entsoe, store
from .model import PricePoint

log = logging.getLogger("energy_prices")
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

DEFAULT_SOURCE = energycharts.SOURCE
SOURCES = (energycharts.SOURCE, entsoe.SOURCE)

# Each source serves a different maximum span per request: Energy-Charts returns five
# years in one response, ENTSO-E rejects anything over a year.
CHUNK_DAYS = {energycharts.SOURCE: 3 * 365, entsoe.SOURCE: config.MAX_QUERY_DAYS}
ARCHIVE_START = {energycharts.SOURCE: energycharts.ARCHIVE_START, entsoe.SOURCE: date(2015, 1, 1)}


def _utc_midnight(day: date) -> datetime:
    """Start of a Dutch delivery day, expressed in UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def _fetch(source: str, zone: str, start: date, end: date) -> list[PricePoint]:
    """Fetch delivery days [start, end) from one source."""
    if source == energycharts.SOURCE:
        # The API reads both bounds as inclusive local delivery days.
        return energycharts.fetch_day_ahead(zone, start, end - timedelta(days=1))
    return entsoe.fetch_day_ahead(zone, _utc_midnight(start), _utc_midnight(end))


def collect(source: str, zones: list[str], start: date, end: date) -> int:
    """Fetch and store prices for delivery days [start, end)."""
    total = 0
    first_request = True
    for zone in zones:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=CHUNK_DAYS[source]), end)
            if not first_request and source == energycharts.SOURCE:
                time.sleep(energycharts.PAUSE_SECONDS)
            first_request = False

            log.info("fetching %s %s %s..%s", source, zone, cursor, chunk_end)
            points = _fetch(source, zone, cursor, chunk_end)
            log.info("  %s price points", len(points))
            if points:
                for path, rows in store.write_points(points).items():
                    log.info("  %s -> %s rows", path, rows)
                total += len(points)
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
        points = entsoe.fetch_day_ahead(
            "NL", _utc_midnight(yesterday), _utc_midnight(yesterday + timedelta(days=1)), token
        )
    except RuntimeError as exc:
        log.error("%s", exc)
        log.error("the platform knows the account but has not enabled API access for it; "
                  "email transparency@entsoe.eu with subject 'Restful API access'")
        return 1
    except requests.RequestException as exc:
        log.error("could not reach %s: %s", config.API_URL, exc)
        return 1

    if not points:
        log.error("the API answered but returned no prices for %s - unexpected for NL", yesterday)
        return 1

    log.info("OK: %s price points for NL on %s", len(points), yesterday)
    for point in points[:3]:
        log.info("  %s %s %7.2f EUR/MWh", point.mtu_start_utc, point.resolution, point.price_eur_mwh)
    log.info("the token works. Next: python -m energy_prices backfill --source entsoe")
    return 0


def _summary() -> None:
    for source, zone, resolution, rows, first, last in store.coverage():
        log.info("coverage %s %s %s: %s rows, %s .. %s", source, zone, resolution, rows, first, last)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="energy_prices")
    parser.add_argument(
        "command", choices=["check-token", "backfill", "daily", "build-db", "coverage"]
    )
    parser.add_argument("--source", choices=SOURCES, default=DEFAULT_SOURCE)
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
        start = args.start or ARCHIVE_START[args.source]
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
