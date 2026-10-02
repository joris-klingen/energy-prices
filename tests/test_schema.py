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
