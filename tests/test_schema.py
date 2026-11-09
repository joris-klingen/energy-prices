import polars as pl
import pytest

from energy_prices.schema import SCHEMA, empty_frame, validate


def test_empty_frame_carries_the_declared_schema():
    assert dict(empty_frame().schema) == SCHEMA


def test_validate_reorders_and_casts_to_the_declared_schema():
    frame = empty_frame().select(reversed(list(SCHEMA)))
    assert list(validate(frame).columns) == list(SCHEMA)


def test_validate_rejects_a_missing_column():
    with pytest.raises(ValueError, match="missing columns: price_eur_mwh"):
        validate(empty_frame().drop("price_eur_mwh"))


def test_validate_rejects_an_unexpected_column():
    with pytest.raises(ValueError, match="unexpected columns: surprise"):
        validate(empty_frame().with_columns(surprise=pl.lit(1)))


def test_every_zone_has_a_well_formed_eic_code():
    from energy_prices import config

    for zone, code in config.EIC_CODES.items():
        assert len(code) == 16 and code.startswith("10Y"), zone
    assert not set(config.ZONES) & set(config.RETIRED_ZONES)
