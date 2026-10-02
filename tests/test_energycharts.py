from datetime import datetime, timezone

import pytest

from energy_prices.energycharts import parse_price_series

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
# 2025-09-30T22:00Z, the first hour of the Dutch delivery day.
BASE = 1759269600


def _parse(payload):
    return parse_price_series({"unit": "EUR / MWh", **payload}, "NL", NOW)


def test_hourly_spacing_is_read_as_pt60m():
    points = _parse({"unix_seconds": [BASE, BASE + 3600], "price": [85.1, 79.4]})
    assert [p.resolution for p in points] == ["PT60M", "PT60M"]
    assert points[0].mtu_start_utc == datetime(2025, 9, 30, 22, 0, tzinfo=timezone.utc)
    assert points[0].mtu_end_utc == datetime(2025, 9, 30, 23, 0, tzinfo=timezone.utc)
    assert points[0].source == "energy-charts"
    assert points[0].price_eur_mwh == 85.1


def test_mixed_spacing_across_the_mtu_change_is_resolved_per_point():
    # Two hourly points, then the 15-minute series the EU switched to on 2025-10-01.
    stamps = [BASE, BASE + 3600, BASE + 7200, BASE + 8100, BASE + 9000]
    points = _parse({"unix_seconds": stamps, "price": [10.0, 11.0, 12.0, 13.0, 14.0]})
    assert [p.resolution for p in points] == ["PT60M", "PT60M", "PT15M", "PT15M", "PT15M"]
    # The last point has nothing after it, so it inherits the preceding spacing.
    assert points[-1].mtu_end_utc.timestamp() - points[-1].mtu_start_utc.timestamp() == 900


def test_missing_prices_are_dropped_rather_than_stored_as_null():
    points = _parse({"unix_seconds": [BASE, BASE + 3600], "price": [None, 79.4]})
    assert [p.price_eur_mwh for p in points] == [79.4]


def test_empty_response_yields_no_points():
    assert _parse({"unix_seconds": [], "price": []}) == []


def test_unexpected_spacing_is_an_error_rather_than_a_silent_guess():
    with pytest.raises(ValueError, match="spacing"):
        _parse({"unix_seconds": [BASE, BASE + 123], "price": [1.0, 2.0]})


def test_a_non_mwh_series_is_refused():
    with pytest.raises(ValueError, match="EUR/MWh"):
        parse_price_series(
            {"unit": "EUR / kWh", "unix_seconds": [BASE], "price": [0.08]}, "NL", NOW
        )


def test_mismatched_array_lengths_are_an_error():
    with pytest.raises(ValueError, match="timestamps"):
        _parse({"unix_seconds": [BASE, BASE + 3600], "price": [1.0]})
