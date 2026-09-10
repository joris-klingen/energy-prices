"""Storage layer: month-partitioned Parquet files plus a DuckDB view over them.

Parquet is the source of truth (small, diff-friendly enough to keep in git, readable
from R, Python or DuckDB). The .duckdb file is a derived convenience artefact and is
rebuilt from the Parquet files on demand.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb

from . import config
from .entsoe import PricePoint

log = logging.getLogger(__name__)

COLUMNS = [
    ("bidding_zone", "VARCHAR"),
    ("eic_code", "VARCHAR"),
    ("mtu_start_utc", "TIMESTAMPTZ"),
    ("mtu_end_utc", "TIMESTAMPTZ"),
    ("resolution", "VARCHAR"),
    ("price_eur_mwh", "DOUBLE"),
    ("currency", "VARCHAR"),
    ("unit", "VARCHAR"),
    ("document_mrid", "VARCHAR"),
    ("revision_number", "INTEGER"),
    ("retrieved_at_utc", "TIMESTAMPTZ"),
]
COLUMN_NAMES = [name for name, _ in COLUMNS]

# A price point is uniquely identified by zone, delivery interval and resolution;
# later revisions of the same interval replace earlier ones.
KEY = ["bidding_zone", "mtu_start_utc", "resolution"]


def partition_path(zone: str, month: str, data_dir: Path | None = None) -> Path:
    root = data_dir or config.DATA_DIR
    return root / "day_ahead" / f"zone={zone}" / f"{month}.parquet"


def _month(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


def _staging_table(con: duckdb.DuckDBPyConnection, points: list[PricePoint]) -> None:
    columns = ", ".join(f"{name} {sql_type}" for name, sql_type in COLUMNS)
    con.execute(f"CREATE OR REPLACE TABLE staging ({columns})")
    placeholders = ", ".join("?" for _ in COLUMNS)
    con.executemany(
        f"INSERT INTO staging VALUES ({placeholders})",
        [
            (
                p.bidding_zone, p.eic_code, p.mtu_start_utc, p.mtu_end_utc, p.resolution,
                p.price_eur_mwh, p.currency, p.unit, p.document_mrid, p.revision_number,
                p.retrieved_at_utc,
            )
            for p in points
        ],
    )


def write_points(points: list[PricePoint], data_dir: Path | None = None) -> dict[str, int]:
    """Merge points into their monthly partitions. Returns rows written per partition."""
    by_partition: dict[tuple[str, str], list[PricePoint]] = defaultdict(list)
    for point in points:
        by_partition[(point.bidding_zone, _month(point.mtu_start_utc))].append(point)

    written: dict[str, int] = {}
    for (zone, month), chunk in sorted(by_partition.items()):
        target = partition_path(zone, month, data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        try:
            _staging_table(con, chunk)
            sources = ["SELECT * FROM staging"]
            if target.exists():
                sources.append(f"SELECT * FROM read_parquet('{target.as_posix()}')")
            union = " UNION ALL BY NAME ".join(sources)
            partition_by = ", ".join(KEY)
            tmp = target.with_suffix(".parquet.tmp")
            con.execute(
                f"""
                COPY (
                    SELECT {", ".join(COLUMN_NAMES)} FROM (
                        SELECT *, row_number() OVER (
                            PARTITION BY {partition_by}
                            ORDER BY revision_number DESC, retrieved_at_utc DESC
                        ) AS _rn
                        FROM ({union})
                    ) WHERE _rn = 1
                    ORDER BY mtu_start_utc, resolution
                ) TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
            os.replace(tmp, target)
            written[str(target)] = con.execute(
                f"SELECT count(*) FROM read_parquet('{target.as_posix()}')"
            ).fetchone()[0]
        finally:
            con.close()
    return written


def build_duckdb(db_path: Path | None = None, data_dir: Path | None = None) -> Path:
    """(Re)build the DuckDB database from the Parquet partitions."""
    db_path = db_path or config.DUCKDB_PATH
    root = data_dir or config.DATA_DIR
    pattern = (root / "day_ahead" / "*" / "*.parquet").as_posix()
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
            ORDER BY bidding_zone, mtu_start_utc, resolution
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
    """Rows and date range per zone and resolution, for the run summary."""
    root = data_dir or config.DATA_DIR
    pattern = (root / "day_ahead" / "*" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        return con.execute(
            f"""
            -- Cast the timestamps to text: returning TIMESTAMPTZ to Python would
            -- pull in an optional pytz dependency just for this summary.
            SELECT bidding_zone, resolution, count(*) AS rows,
                   strftime(min(mtu_start_utc), '%Y-%m-%d %H:%M') AS first_mtu,
                   strftime(max(mtu_start_utc), '%Y-%m-%d %H:%M') AS last_mtu
            FROM read_parquet('{pattern}', union_by_name = true)
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()
    except duckdb.IOException:
        return []
    finally:
        con.close()
