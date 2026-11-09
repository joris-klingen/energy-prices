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


def _with_second_series(price: str) -> str:
    """The fixture plus a copy of its first series at a different price."""
    xml = (FIXTURES / "a44_nl.xml").read_text()
    start = xml.index("<TimeSeries>")
    end = xml.index("</TimeSeries>", start) + len("</TimeSeries>")
    copy = xml[start:end].replace("<price.amount>85.12</price.amount>", f"<price.amount>{price}</price.amount>")
    return xml[:end] + copy + xml[end:]


def test_a_repeated_identical_series_is_stored_once():
    frame = parse_price_document(_with_second_series("85.12"), "NL", NOW)
    assert frame.height == parse_price_document((FIXTURES / "a44_nl.xml").read_text(), "NL", NOW).height


def test_two_different_prices_for_one_interval_fail_loudly():
    import pytest

    with pytest.raises(ValueError, match="conflicting prices"):
        parse_price_document(_with_second_series("99.99"), "NL", NOW)


def test_de_lu_asks_for_the_sdac_sequence(monkeypatch):
    from datetime import datetime, timezone

    from energy_prices.sources import entsoe

    sent = {}

    def fake_request(params, *args, **kwargs):
        sent.update(params)
        return None

    monkeypatch.setattr(entsoe, "request", fake_request)
    start = datetime(2026, 11, 1, 23, tzinfo=timezone.utc)
    entsoe.fetch_day_ahead("DE-LU", start, start, "token")
    assert sent["classificationSequence_AttributeInstanceComponent.position"] == 1
    sent.clear()
    entsoe.fetch_day_ahead("NL", start, start, "token")
    assert "classificationSequence_AttributeInstanceComponent.position" not in sent
