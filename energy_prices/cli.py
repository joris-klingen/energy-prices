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
from .collectors import COLLECTORS, Collector
from .errors import SourceUnavailable
from .sources import DEFAULT, SOURCES, entsoe

log = logging.getLogger("energy_prices")
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# How far the archive may fall behind before a run is treated as a real problem.
# A healthy archive reaches tomorrow, so this leaves roughly three days of slack for
# an upstream outage to clear on its own before anyone needs to be told.
STALE_AFTER_DAYS = 2

DAY_AHEAD = "day-ahead"
DATASET_CHOICES = [DAY_AHEAD, *COLLECTORS]

# How long the primary source may produce nothing before that is a problem in its own
# right. The fallback keeps the archive fresh, which would otherwise mask a primary
# that has quietly stopped working - and ENTSO-E answers a revoked token with the same
# opaque 500 it returns during an outage, so nothing else would ever notice.
PRIMARY_STALE_AFTER_DAYS = 3


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
    seen = set()
    for collector in COLLECTORS.values():
        if collector.dataset.name in seen:
            continue
        seen.add(collector.dataset.name)
        for row in store.coverage(dataset=collector.dataset):
            *labels, rows, first, last = row
            log.info("coverage %s %s: %s rows, %s .. %s",
                     collector.dataset.name, " ".join(map(str, labels)), rows, first, last)


def run_collector(
    collector: Collector, command: str, start: date | None, end: date | None, today: date
) -> int:
    """Backfill or top up one non-day-ahead dataset. Returns an exit code.

    Mirrors the day-ahead rules: an outage only warns while the dataset is still within
    its staleness allowance, an incomplete backfill fails, a bad request fails loudly.
    """
    if collector.needs_entsoe_token and not config.has_api_token():
        log.warning("skipping %s: no ENTSO-E token configured", collector.name)
        return 0

    if command == "backfill":
        start = start or collector.archive_start
    else:
        start = start or today - timedelta(days=collector.lookback_days)
    end = end or today + timedelta(days=2)

    degraded = False
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=collector.chunk_days), end)
        log.info("fetching %s %s..%s", collector.name, cursor, chunk_end)
        try:
            frame = collector.fetch(cursor, chunk_end)
        except SourceUnavailable as exc:
            log.warning("  %s is unavailable: %s", collector.source, exc)
            degraded = True
            break
        log.info("  %s rows", frame.height)
        for path, rows in store.write_frame(frame, dataset=collector.dataset).items():
            log.info("  %s -> %s rows", path, rows)
        cursor = chunk_end

    if command == "backfill":
        if degraded:
            log.error("%s backfill incomplete: the source went unavailable", collector.name)
            return 1
        return 0
    return report_collector_staleness(collector, today)


def report_collector_staleness(collector: Collector, today: date) -> int:
    """Fail when a dataset's newest row is older than its allowance, outage or not.

    Checked on every daily run rather than only after an outage: a scrape whose page
    layout changed can keep answering 200 with nothing new, and only the age of the
    newest row would ever show it.
    """
    newest = store.newest_day(dataset=collector.dataset, source=collector.source)
    if newest is None:
        log.error("%s has never produced a row in this archive", collector.name)
        return 1
    behind = (today - newest).days
    if behind > collector.stale_after_days:
        log.error(
            "%s is stale: newest row is from %s, %s days old (allowance %s)",
            collector.name, newest, behind, collector.stale_after_days,
        )
        return 1
    return 0


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


def report_staleness(today: date, zones: list[str] | None = None) -> int:
    """Decide whether an incomplete run is worth failing over.

    An upstream outage is not our problem and the daily lookback repairs the gap by
    itself, so it only earns a warning. An archive that has actually fallen behind
    does need attention, and that is what the non-zero exit is reserved for. With
    `zones`, each is judged on its own, so one zone going quiet cannot hide behind the
    others staying fresh.
    """
    checks = [None] if not zones else list(zones)
    exit_code = 0
    for zone in checks:
        label = "the archive" if zone is None else zone
        newest = store.newest_delivery_day(zone=zone)
        if newest is None:
            log.error("%s is empty and the source is unavailable", label)
            exit_code = 1
            continue

        behind = (today - newest).days
        if behind > STALE_AFTER_DAYS:
            log.error(
                "%s is stale: newest delivery day is %s, %s days behind today - "
                "the source has been unavailable too long to shrug off",
                label, newest, behind,
            )
            exit_code = 1
            continue

        log.warning(
            "source unavailable, but %s is current through %s; the next run's "
            "%s-day lookback will fill the gap",
            label, newest, config.LOOKBACK_DAYS,
        )
    return exit_code


def report_primary_health(source: str, today: date) -> int:
    """Fail when the primary source has gone quiet for days, fallback or no fallback."""
    newest = store.newest_delivery_day(source=source)
    if newest is None:
        log.error("%s has never produced a row in this archive", source)
        return 1

    behind = (today - newest).days
    if behind > PRIMARY_STALE_AFTER_DAYS:
        log.error(
            "%s has produced nothing since %s (%s days behind) - the fallback is "
            "keeping the archive fresh and hiding a broken primary; check the "
            "credential and the service before trusting this",
            source, newest, behind,
        )
        return 1
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
    parser.add_argument(
        "--zones", nargs="*", choices=list(config.EIC_CODES), default=list(config.ZONES),
        metavar="ZONE", help=f"bidding zones (default: {' '.join(config.ZONES)})",
    )
    parser.add_argument("--start", type=date.fromisoformat, help="first delivery day (backfill)")
    parser.add_argument("--end", type=date.fromisoformat, help="exclusive last delivery day")
    parser.add_argument("--lookback", type=int, default=config.LOOKBACK_DAYS,
                        help="day-ahead lookback; other datasets use their own")
    parser.add_argument(
        "--datasets", nargs="*", choices=DATASET_CHOICES, default=DATASET_CHOICES,
        metavar="DATASET", help=f"what to collect (default: all of {' '.join(DATASET_CHOICES)})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    today = datetime.now(LOCAL_TZ).date()

    if args.command == "check-token":
        return check_token()
    if args.command == "coverage":
        _summary()
        return 0

    day_ahead = DAY_AHEAD in args.datasets
    degraded = False
    extra_exit = 0
    if args.command in ("backfill", "daily") and day_ahead:
        fallback = _resolve_fallback(args.source, args.fallback)
        if args.command == "backfill":
            start = args.start or SOURCES[args.source].ARCHIVE_START
        else:
            # Re-fetch a short window so late corrections are picked up, and reach into
            # tomorrow because D+1 prices are published around 12:45 CET.
            start = args.start or today - timedelta(days=args.lookback)
        end = args.end or today + timedelta(days=2)
        _, degraded = collect_with_fallback(args.source, fallback, args.zones, start, end)
    if args.command in ("backfill", "daily"):
        for name in args.datasets:
            if name != DAY_AHEAD:
                code = run_collector(COLLECTORS[name], args.command, args.start, args.end, today)
                extra_exit = extra_exit or code

    log.info("building duckdb at %s", store.build_duckdb())
    _summary()

    if args.command == "backfill":
        if degraded:
            log.error("backfill incomplete: the source went unavailable")
            return 1
        return extra_exit
    if args.command != "daily" or not day_ahead:
        return extra_exit

    # daily: an outage is forgiven while the archive keeps up, but a primary that has
    # gone quiet for days is reported even when the fallback is covering for it.
    exit_code = report_staleness(today, args.zones) if degraded else 0
    return exit_code or report_primary_health(args.source, today) or extra_exit
