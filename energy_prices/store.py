"""Storage: month-partitioned Parquet written with polars, queried with DuckDB.

Parquet is the source of truth — small, readable from polars, DuckDB or R, and
diff-friendly enough to keep in git. The .duckdb file is a derived convenience
artefact and is rebuilt from the Parquet files on demand.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import polars as pl

from . import config
from .schema import KEY, validate

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def partition_path(zone: str, month: str, data_dir: Path | None = None) -> Path:
    root = data_dir or config.DATA_DIR
    return root / "day_ahead" / f"zone={zone}" / f"{month}.parquet"


def parquet_glob(data_dir: Path | None = None) -> str:
    root = data_dir or config.DATA_DIR
    return (root / "day_ahead" / "*" / "*.parquet").as_posix()


def write_frame(frame: pl.DataFrame, data_dir: Path | None = None) -> dict[str, int]:
    """Merge rows into their monthly partitions. Returns rows held per partition.

    A partition is rewritten whole: existing rows and new rows are concatenated, then
    reduced to one row per key keeping the highest revision and, within a revision, the
    most recent retrieval. The write goes to a temporary file and is moved into place,
    so an interrupted run cannot leave a half-written partition behind.
    """
    if frame.is_empty():
        return {}
    frame = validate(frame)

    written: dict[str, int] = {}
    partitions = frame.with_columns(
        _month=pl.col("mtu_start_utc").dt.strftime("%Y-%m")
    ).partition_by("bidding_zone", "_month", as_dict=True)

    for (zone, month), part in sorted(partitions.items()):
        target = partition_path(zone, month, data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = part.drop("_month")

        merged = pl.concat([part, pl.read_parquet(target)]) if target.exists() else part
        merged = (
            merged.sort(["revision_number", "retrieved_at_utc"], descending=True)
            # keep="first" only means "highest revision" because of the sort above,
            # and only holds that order with maintain_order set.
            .unique(subset=KEY, keep="first", maintain_order=True)
            .sort(["mtu_start_utc", "resolution"])
        )

        tmp = target.with_suffix(".parquet.tmp")
        merged.write_parquet(tmp, compression="zstd")
        os.replace(tmp, target)
        written[str(target)] = merged.height
    return written


def build_duckdb(db_path: Path | None = None, data_dir: Path | None = None) -> Path:
    """(Re)build the DuckDB database from the Parquet partitions."""
    db_path = db_path or config.DUCKDB_PATH
    pattern = parquet_glob(data_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            f"""
            CREATE TABLE day_ahead_prices AS
            SELECT *,
                   mtu_start_utc AT TIME ZONE 'Europe/Amsterdam' AS mtu_start_local
            FROM read_parquet('{pattern}', union_by_name = true)
            ORDER BY source, bidding_zone, mtu_start_utc, resolution
            """
        )
        con.execute(
            """
            CREATE VIEW day_ahead_hourly AS
            SELECT * FROM day_ahead_prices WHERE resolution = 'PT60M'
            """
        )
    finally:
        con.close()
    return db_path


def coverage(data_dir: Path | None = None) -> list[tuple]:
    """Rows and date range per source, zone and resolution, for the run summary."""
    try:
        return (
            pl.scan_parquet(parquet_glob(data_dir))
            .group_by("source", "bidding_zone", "resolution")
            .agg(
                pl.len().alias("rows"),
                pl.min("mtu_start_utc").dt.strftime("%Y-%m-%d %H:%M").alias("first_mtu"),
                pl.max("mtu_start_utc").dt.strftime("%Y-%m-%d %H:%M").alias("last_mtu"),
            )
            .sort("source", "bidding_zone", "resolution")
            .collect()
            .rows()
        )
    except (FileNotFoundError, pl.exceptions.ComputeError):
        return []


def newest_delivery_day(
    data_dir: Path | None = None, source: str | None = None
) -> date | None:
    """The local delivery day of the most recent stored price, or None if empty.

    This is what says whether the archive is keeping up: after a healthy run it is
    tomorrow, because D+1 prices are published around 12:45 CET. Narrow it to one
    `source` to ask the same question of a single provider.
    """
    frame = pl.scan_parquet(parquet_glob(data_dir))
    if source is not None:
        frame = frame.filter(pl.col("source") == source)
    try:
        newest = frame.select(pl.max("mtu_start_utc")).collect().item()
    except (FileNotFoundError, pl.exceptions.ComputeError):
        return None
    return None if newest is None else newest.astimezone(LOCAL_TZ).date()
