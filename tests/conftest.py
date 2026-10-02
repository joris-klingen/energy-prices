from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from energy_prices.schema import SCHEMA, validate

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return NOW


def price_frame(*prices: float, start: datetime, source: str = "entsoe",
                revision: int = 1, resolution: str = "PT60M") -> pl.DataFrame:
    """One hourly row per price, starting at `start`."""
    step = timedelta(hours=1)
    rows = [
        {
            "source": source,
            "bidding_zone": "NL",
            "eic_code": "10YNL----------L",
            "mtu_start_utc": start + index * step,
            "mtu_end_utc": start + (index + 1) * step,
            "resolution": resolution,
            "price_eur_mwh": price,
            "currency": "EUR",
            "unit": "MWH",
            "source_reference": "doc",
            "revision_number": revision,
            "retrieved_at_utc": NOW,
        }
        for index, price in enumerate(prices)
    ]
    return validate(pl.DataFrame(rows, schema=SCHEMA))
