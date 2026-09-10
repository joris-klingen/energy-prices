from datetime import datetime, timedelta, timezone

import duckdb

from energy_prices.entsoe import PricePoint
from energy_prices.store import build_duckdb, partition_path, write_points

START = datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc)


def _point(price: float, revision: int = 1, hours: int = 0) -> PricePoint:
    start = START + timedelta(hours=hours)
    return PricePoint(
        bidding_zone="NL",
        eic_code="10YNL----------L",
        mtu_start_utc=start,
        mtu_end_utc=start + timedelta(hours=1),
        resolution="PT60M",
        price_eur_mwh=price,
        currency="EUR",
        unit="MWH",
        document_mrid="doc",
        revision_number=revision,
        retrieved_at_utc=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )


def test_later_revision_replaces_earlier_one(tmp_path):
    write_points([_point(85.0, revision=1), _point(50.0, revision=1, hours=1)], tmp_path)
    write_points([_point(99.0, revision=2)], tmp_path)

    path = partition_path("NL", "2026-09", tmp_path)
    rows = duckdb.connect().execute(
        f"SELECT price_eur_mwh FROM read_parquet('{path.as_posix()}') ORDER BY mtu_start_utc"
    ).fetchall()
    assert rows == [(99.0,), (50.0,)]


def test_build_duckdb_adds_local_time_column(tmp_path):
    write_points([_point(85.0)], tmp_path)
    db = build_duckdb(tmp_path / "prices.duckdb", tmp_path)
    row = duckdb.connect(str(db)).execute(
        "SELECT mtu_start_local, price_eur_mwh FROM day_ahead_prices"
    ).fetchone()
    # 22:00 UTC in September is midnight in Amsterdam (CEST).
    assert row[0] == datetime(2026, 9, 10, 0, 0)
    assert row[1] == 85.0
