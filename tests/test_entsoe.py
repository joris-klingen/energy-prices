from datetime import datetime, timezone
from pathlib import Path

from energy_prices.sources.entsoe import parse_price_document

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _parse(name: str):
    return parse_price_document((FIXTURES / name).read_text(), "NL", NOW)


def test_hourly_and_quarter_hourly_series_are_both_kept():
    frame = _parse("a44_nl.xml")
    hourly = frame.filter(resolution="PT60M")
    quarterly = frame.filter(resolution="PT15M")
    assert hourly.height == 2
    assert quarterly.height == 4

    first = hourly.row(0, named=True)
    assert first["mtu_start_utc"] == datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc)
    assert first["price_eur_mwh"] == 85.12
    assert first["eic_code"] == "10YNL----------L"
    assert first["revision_number"] == 1
    assert first["source"] == "entsoe"


def test_variable_sized_curve_carries_price_forward_over_skipped_positions():
    quarterly = _parse("a44_nl.xml").filter(resolution="PT15M")
    assert quarterly["price_eur_mwh"].to_list() == [90.0, 90.0, 70.0, 70.0]
    assert quarterly["mtu_end_utc"][-1] == datetime(2026, 9, 9, 23, 0, tzinfo=timezone.utc)


def test_acknowledgement_document_yields_no_rows():
    assert _parse("acknowledgement.xml").is_empty()
