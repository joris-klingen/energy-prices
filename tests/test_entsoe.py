from datetime import datetime, timezone
from pathlib import Path

from energy_prices.entsoe import parse_price_document

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _parse(name: str):
    return parse_price_document((FIXTURES / name).read_text(), "NL", NOW)


def test_hourly_and_quarter_hourly_series_are_both_kept():
    points = _parse("a44_nl.xml")
    hourly = [p for p in points if p.resolution == "PT60M"]
    quarterly = [p for p in points if p.resolution == "PT15M"]
    assert len(hourly) == 2
    assert len(quarterly) == 4
    assert hourly[0].mtu_start_utc == datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc)
    assert hourly[0].price_eur_mwh == 85.12
    assert hourly[0].eic_code == "10YNL----------L"
    assert hourly[0].revision_number == 1


def test_variable_sized_curve_carries_price_forward_over_skipped_positions():
    quarterly = [p for p in _parse("a44_nl.xml") if p.resolution == "PT15M"]
    assert [p.price_eur_mwh for p in quarterly] == [90.0, 90.0, 70.0, 70.0]
    assert quarterly[-1].mtu_end_utc == datetime(2026, 9, 9, 23, 0, tzinfo=timezone.utc)


def test_acknowledgement_document_yields_no_points():
    assert _parse("acknowledgement.xml") == []
