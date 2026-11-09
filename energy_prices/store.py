"""Storage: period-partitioned Parquet written with polars, queried with DuckDB.

Parquet is the source of truth — small, readable from polars, DuckDB or R, and
diff-friendly enough to keep in git. The .duckdb file is a derived convenience
artefact and is rebuilt from the Parquet files on demand.

Every function takes the `Dataset` it works on and defaults to the day-ahead prices.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import polars as pl

from . import config
from .schema import DATASETS, DAY_AHEAD, Dataset, validate

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def partition_path(
    value: str, period: str, data_dir: Path | None = None, dataset: Dataset = DAY_AHEAD
) -> Path:
    root = data_dir or config.DATA_DIR
    return root / dataset.name / f"{dataset.partition_label}={value}" / f"{period}.parquet"


def parquet_glob(data_dir: Path | None = None, dataset: Dataset = DAY_AHEAD) -> str:
    root = data_dir or config.DATA_DIR
    return (root / dataset.name / "*" / "*.parquet").as_posix()


def _has_files(data_dir: Path | None, dataset: Dataset) -> bool:
    root = (data_dir or config.DATA_DIR) / dataset.name
    return root.is_dir() and any(root.glob("*/*.parquet"))


def write_frame(
    frame: pl.DataFrame, data_dir: Path | None = None, dataset: Dataset = DAY_AHEAD
) -> dict[str, int]:
    """Merge rows into their partitions. Returns rows held per partition.

    A partition is rewritten whole: existing rows and new rows are concatenated, then
    reduced to one row per key keeping the highest revision and, within a revision, the
    most recent retrieval. The write goes to a temporary file and is moved into place,
    so an interrupted run cannot leave a half-written partition behind.
    """
    if frame.is_empty():
        return {}
    frame = validate(frame, dataset)

    written: dict[str, int] = {}
    partitions = frame.with_columns(
        _period=pl.col(dataset.time_column).dt.strftime(dataset.period_format)
    ).partition_by(dataset.partition_by, "_period", as_dict=True)

    for (value, period), part in sorted(partitions.items()):
        target = partition_path(value, period, data_dir, dataset)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = part.drop("_period")

        merged = pl.concat([part, pl.read_parquet(target)]) if target.exists() else part
        merged = (
            merged.sort(["revision_number", "retrieved_at_utc"], descending=True)
            # keep="first" only means "highest revision" because of the sort above,
            # and only holds that order with maintain_order set.
            .unique(subset=list(dataset.key), keep="first", maintain_order=True)
            .sort(dataset.sort_order, maintain_order=True)
        )

        tmp = target.with_suffix(".parquet.tmp")
        merged.write_parquet(tmp, compression="zstd")
        os.replace(tmp, target)
        written[str(target)] = merged.height
    return written


def build_duckdb(db_path: Path | None = None, data_dir: Path | None = None) -> Path:
    """(Re)build the DuckDB database from the Parquet partitions of every dataset."""
    db_path = db_path or config.DUCKDB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        if _has_files(data_dir, DAY_AHEAD):
            con.execute(
                f"""
                CREATE TABLE day_ahead_prices AS
                SELECT *,
                       mtu_start_utc AT TIME ZONE 'Europe/Amsterdam' AS mtu_start_local
                FROM read_parquet('{parquet_glob(data_dir)}', union_by_name = true)
                ORDER BY source, bidding_zone, mtu_start_utc, resolution
                """
            )
            con.execute(
                """
                CREATE VIEW day_ahead_hourly AS
                SELECT * FROM day_ahead_prices WHERE resolution = 'PT60M'
                """
            )
        for dataset in DATASETS.values():
            if dataset is DAY_AHEAD or not _has_files(data_dir, dataset):
                continue
            local = (
                f", {dataset.time_column} AT TIME ZONE 'Europe/Amsterdam' AS mtu_start_local"
                if dataset.time_column == "mtu_start_utc"
                else ""
            )
            con.execute(
                f"""
                CREATE TABLE {dataset.name}_prices AS
                SELECT *{local}
                FROM read_parquet('{parquet_glob(data_dir, dataset)}', union_by_name = true)
                ORDER BY {', '.join(dataset.key)}
                """
            )
    finally:
        con.close()
    return db_path


def coverage(data_dir: Path | None = None, dataset: Dataset = DAY_AHEAD) -> list[tuple]:
    """Rows and range per source, partition and coverage columns, for the run summary."""
    fmt = "%Y-%m-%d %H:%M" if dataset.schema[dataset.time_column] != pl.Date else "%Y-%m-%d"
    group = ["source", dataset.partition_by, *dataset.coverage_by]
    try:
        return (
            pl.scan_parquet(parquet_glob(data_dir, dataset))
            .group_by(group)
            .agg(
                pl.len().alias("rows"),
                pl.min(dataset.time_column).dt.strftime(fmt).alias("first"),
                pl.max(dataset.time_column).dt.strftime(fmt).alias("last"),
            )
            .sort(group)
            .collect()
            .rows()
        )
    except (FileNotFoundError, pl.exceptions.ComputeError):
        return []


def newest_day(
    data_dir: Path | None = None,
    dataset: Dataset = DAY_AHEAD,
    **filters: str | None,
) -> date | None:
    """The local day of the most recent stored row, or None if there is none.

    `filters` narrow it to rows whose columns equal the given values (None is ignored),
    e.g. `source="entsoe"` or `bidding_zone="NL"`.
    """
    frame = pl.scan_parquet(parquet_glob(data_dir, dataset))
    for column, value in filters.items():
        if value is not None:
            frame = frame.filter(pl.col(column) == value)
    try:
        newest = frame.select(pl.max(dataset.time_column)).collect().item()
    except (FileNotFoundError, pl.exceptions.ComputeError):
        return None
    if newest is None:
        return None
    if isinstance(newest, datetime):
        return newest.astimezone(LOCAL_TZ).date()
    return newest


def newest_delivery_day(
    data_dir: Path | None = None, source: str | None = None, zone: str | None = None
) -> date | None:
    """The local delivery day of the most recent stored day-ahead price, or None.

    This is what says whether the archive is keeping up: after a healthy run it is
    tomorrow, because D+1 prices are published around 12:45 CET. Narrow it to one
    `source` to ask the same question of a single provider, or one `zone` to ask it of a
    single bidding zone.
    """
    return newest_day(data_dir, DAY_AHEAD, source=source, bidding_zone=zone)
