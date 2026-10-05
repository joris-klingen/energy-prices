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
from .errors import SourceUnavailable
from .sources import DEFAULT, SOURCES, entsoe

log = logging.getLogger("energy_prices")
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# How far the archive may fall behind before a run is treated as a real problem.
# A healthy archive reaches tomorrow, so this leaves roughly three days of slack for
# an upstream outage to clear on its own before anyone needs to be told.
STALE_AFTER_DAYS = 2


def _utc_midnight(day: date) -> datetime:
    """Start of a Dutch delivery day, expressed in UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def _fetch(source: str, zone: str, start: date, end: date) -> pl.DataFrame:
    """Fetch delivery days [start, end) from one source."""
    if source == entsoe.SOURCE:
        # ENTSO-E takes UTC instants rather than delivery days.
        return entsoe.fetch_day_ahead(zone, _utc_midnight(start), _utc_midnight(end))
    return SOURCES[source].fetch_day_ahead(zone, start, end)


def collect(source: str, zones: list[str], start: date, end: date) -> tuple[int, bool]:
    """Fetch and store prices for delivery days [start, end).

    Returns the rows stored and whether the source went unavailable part-way. An
    unavailable source ends the attempt for that source rather than hammering a
    service that has already said it has nothing to give.
    """
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
            try:
                frame = _fetch(source, zone, cursor, chunk_end)
            except SourceUnavailable as exc:
                log.warning("  %s is unavailable: %s", source, exc)
                return total, True

            log.info("  %s price points", frame.height)
            if not frame.is_empty():
                for path, rows in store.write_frame(frame).items():
                    log.info("  %s -> %s rows", path, rows)
                total += frame.height
            cursor = chunk_end
    return total, False


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
    except SourceUnavailable as exc:
        log.error("%s", exc)
        # ENTSO-E answers an invalid or not-yet-enabled token with an opaque HTTP 500,
        # byte-for-byte the same as a genuine outage, so this cannot be narrowed down
        # from here. Saying so beats guessing.
        log.error("a persistent 500 means either the token is not valid yet or the "
                  "platform is down - ENTSO-E returns the same error for both")
        log.error("if it is the token: check it against My Account Settings, and that "
                  "API access was granted (transparency@entsoe.eu, 'Restful API access')")
        return 1
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


def _resolve_fallback(source: str, choice: str) -> str | None:
    """"auto" means the other source; "none" disables falling back."""
    if choice == "none":
        return None
    if choice != "auto":
        return None if choice == source else choice
    others = [name for name in sorted(SOURCES) if name != source]
    return others[0] if others else None


def collect_with_fallback(
    source: str, fallback: str | None, zones: list[str], start: date, end: date
) -> tuple[int, bool]:
    """Collect from `source`, and if it is unavailable try `fallback` instead."""
    rows, degraded = collect(source, zones, start, end)
    if not degraded or fallback is None:
        return rows, degraded

    log.info("falling back to %s", fallback)
    try:
        extra, still_degraded = collect(fallback, zones, start, end)
    except RuntimeError as exc:
        # Typically the fallback has no credential configured.
        log.warning("  fallback %s unusable: %s", fallback, exc)
        return rows, True
    return rows + extra, still_degraded


def report_staleness(today: date) -> int:
    """Decide whether an incomplete run is worth failing over.

    An upstream outage is not our problem and the daily lookback repairs the gap by
    itself, so it only earns a warning. An archive that has actually fallen behind
    does need attention, and that is what the non-zero exit is reserved for.
    """
    newest = store.newest_delivery_day()
    if newest is None:
        log.error("the archive is empty and the source is unavailable")
        return 1

    behind = (today - newest).days
    if behind > STALE_AFTER_DAYS:
        log.error(
            "archive is stale: newest delivery day is %s, %s days behind today - "
            "the source has been unavailable too long to shrug off",
            newest, behind,
        )
        return 1

    log.warning(
        "source unavailable, but the archive is current through %s; the next run's "
        "%s-day lookback will fill the gap",
        newest, config.LOOKBACK_DAYS,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except SourceUnavailable as exc:
        log.error("%s", exc)
        return 1
    except RuntimeError as exc:
        # Misconfiguration, typically a missing credential. One clear line beats a
        # traceback in a CI log nobody can scroll.
        log.error("%s", exc)
        return 1


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="energy-prices")
    parser.add_argument(
        "command", choices=["check-token", "backfill", "daily", "build-db", "coverage"]
    )
    parser.add_argument("--source", choices=sorted(SOURCES), default=DEFAULT)
    parser.add_argument(
        "--fallback", choices=["auto", "none", *sorted(SOURCES)], default="auto",
        help="source to try when the primary is unavailable (default: the other one)",
    )
    parser.add_argument("--zones", nargs="*", default=list(config.ZONES))
    parser.add_argument("--start", type=date.fromisoformat, help="first delivery day (backfill)")
    parser.add_argument("--end", type=date.fromisoformat, help="exclusive last delivery day")
    parser.add_argument("--lookback", type=int, default=config.LOOKBACK_DAYS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    today = datetime.now(LOCAL_TZ).date()

    if args.command == "check-token":
        return check_token()
    if args.command == "coverage":
        _summary()
        return 0

    degraded = False
    if args.command in ("backfill", "daily"):
        fallback = _resolve_fallback(args.source, args.fallback)
        if args.command == "backfill":
            start = args.start or SOURCES[args.source].ARCHIVE_START
        else:
            # Re-fetch a short window so late corrections are picked up, and reach into
            # tomorrow because D+1 prices are published around 12:45 CET.
            start = args.start or today - timedelta(days=args.lookback)
        end = args.end or today + timedelta(days=2)
        _, degraded = collect_with_fallback(args.source, fallback, args.zones, start, end)

    log.info("building duckdb at %s", store.build_duckdb())
    _summary()

    if degraded:
        if args.command == "backfill":
            log.error("backfill incomplete: the source went unavailable")
            return 1
        return report_staleness(today)
    return 0
