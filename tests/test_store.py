from pathlib import Path
from datetime import datetime, timedelta, timezone

import polars as pl

from conftest import price_frame
from energy_prices.store import build_duckdb, coverage, partition_path, write_frame

START = datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc)


def _read(tmp_path) -> pl.DataFrame:
    return pl.read_parquet(partition_path("NL", "2026-09", tmp_path))


def test_rows_are_written_to_a_monthly_partition(tmp_path):
    written = write_frame(price_frame(85.0, 50.0, start=START), tmp_path)
    assert list(written.values()) == [2]
    assert _read(tmp_path)["price_eur_mwh"].to_list() == [85.0, 50.0]


def test_later_revision_replaces_earlier_one(tmp_path):
    write_frame(price_frame(85.0, 50.0, start=START), tmp_path)
    write_frame(price_frame(99.0, start=START, revision=2), tmp_path)
    assert _read(tmp_path)["price_eur_mwh"].to_list() == [99.0, 50.0]


def test_an_earlier_revision_does_not_overwrite_a_later_one(tmp_path):
    write_frame(price_frame(99.0, start=START, revision=2), tmp_path)
    write_frame(price_frame(85.0, start=START, revision=1), tmp_path)
    assert _read(tmp_path)["price_eur_mwh"].to_list() == [99.0]


def test_rewriting_the_same_rows_is_idempotent(tmp_path):
    frame = price_frame(85.0, 50.0, start=START)
    write_frame(frame, tmp_path)
    write_frame(frame, tmp_path)
    assert _read(tmp_path).height == 2


def test_two_sources_can_cover_the_same_interval(tmp_path):
    write_frame(price_frame(85.0, start=START, source="entsoe"), tmp_path)
    write_frame(price_frame(85.1, start=START, source="energy-charts"), tmp_path)
    assert sorted(_read(tmp_path)["source"].to_list()) == ["energy-charts", "entsoe"]


def test_rows_spanning_a_month_boundary_land_in_separate_partitions(tmp_path):
    frame = price_frame(1.0, 2.0, start=datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc))
    written = write_frame(frame, tmp_path)
    assert sorted(Path(p).name for p in written) == ["2026-09.parquet", "2026-10.parquet"]


def test_an_empty_frame_writes_nothing(tmp_path):
    from energy_prices.schema import empty_frame

    assert write_frame(empty_frame(), tmp_path) == {}
    assert not (tmp_path / "day_ahead").exists()


def test_coverage_reports_rows_and_range(tmp_path):
    write_frame(price_frame(85.0, 50.0, start=START), tmp_path)
    assert coverage(tmp_path) == [("entsoe", "NL", "PT60M", 2, "2026-09-09 22:00", "2026-09-09 23:00")]


def test_coverage_of_an_empty_archive_is_empty(tmp_path):
    assert coverage(tmp_path) == []


def test_build_duckdb_adds_local_time_column(tmp_path):
    import duckdb

    write_frame(price_frame(85.0, start=START), tmp_path)
    db = build_duckdb(tmp_path / "prices.duckdb", tmp_path)
    row = duckdb.connect(str(db)).execute(
        "SELECT mtu_start_local, price_eur_mwh FROM day_ahead_prices"
    ).fetchone()
    # 22:00 UTC in September is midnight in Amsterdam (CEST).
    assert row[0] == datetime(2026, 9, 10, 0, 0)
    assert row[1] == 85.0


# --- datasets other than day-ahead -------------------------------------------

def _commodity_frame(*prices: float, start, contract: str = "day-ahead", revision: int = 0):
    from datetime import timedelta as td

    from energy_prices.schema import COMMODITY, validate

    rows = [
        {
            "source": "test", "product": "TTF", "contract": contract,
            "trade_date": start + td(days=index), "price": price, "currency": "EUR",
            "unit": "MWH", "source_reference": "", "revision_number": revision,
            "retrieved_at_utc": START,
        }
        for index, price in enumerate(prices)
    ]
    return validate(pl.DataFrame(rows), COMMODITY)


def test_commodity_rows_are_partitioned_by_product_and_year(tmp_path):
    from datetime import date

    from energy_prices.schema import COMMODITY

    frame = _commodity_frame(30.0, 31.0, start=date(2025, 12, 31))
    written = write_frame(frame, tmp_path, COMMODITY)
    assert sorted(Path(p).relative_to(tmp_path).as_posix() for p in written) == [
        "commodity/product=TTF/2025.parquet",
        "commodity/product=TTF/2026.parquet",
    ]


def test_commodity_contracts_on_the_same_day_are_kept_apart(tmp_path):
    from datetime import date

    from energy_prices.schema import COMMODITY

    day = date(2026, 3, 2)
    write_frame(_commodity_frame(30.0, start=day), tmp_path, COMMODITY)
    write_frame(_commodity_frame(32.5, start=day, contract="front-month"), tmp_path, COMMODITY)
    write_frame(_commodity_frame(30.4, start=day, revision=1), tmp_path, COMMODITY)
    stored = pl.read_parquet(partition_path("TTF", "2026", tmp_path, COMMODITY))
    assert sorted(zip(stored["contract"], stored["price"])) == [
        ("day-ahead", 30.4), ("front-month", 32.5)
    ]
    assert coverage(tmp_path, COMMODITY) == [
        ("test", "TTF", "day-ahead", 1, "2026-03-02", "2026-03-02"),
        ("test", "TTF", "front-month", 1, "2026-03-02", "2026-03-02"),
    ]


def test_build_duckdb_includes_every_dataset_present(tmp_path):
    from datetime import date

    import duckdb

    from energy_prices.schema import COMMODITY

    write_frame(price_frame(85.0, start=START), tmp_path)
    write_frame(_commodity_frame(30.0, start=date(2026, 3, 2)), tmp_path, COMMODITY)
    db = build_duckdb(tmp_path / "prices.duckdb", tmp_path)
    con = duckdb.connect(str(db))
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    assert {"day_ahead_prices", "commodity_prices"} <= tables
    assert "imbalance_prices" not in tables  # nothing stored, so no empty table
