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
    assert sorted(p.rsplit("/", 1)[-1] for p in written) == ["2026-09.parquet", "2026-10.parquet"]


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
