"""The one declared shape of a price row.

Every source returns a frame validated against `SCHEMA`, so a source cannot invent a
column or quietly change a dtype, and the Parquet files on disk stay uniform.
"""

from __future__ import annotations

import polars as pl

SCHEMA: dict[str, pl.DataType] = {
    "source": pl.Utf8,
    "bidding_zone": pl.Utf8,
    "eic_code": pl.Utf8,
    "mtu_start_utc": pl.Datetime("us", "UTC"),
    "mtu_end_utc": pl.Datetime("us", "UTC"),
    "resolution": pl.Utf8,
    "price_eur_mwh": pl.Float64,
    "currency": pl.Utf8,
    "unit": pl.Utf8,
    # Provenance within the source: the document mRID for ENTSO-E, empty elsewhere.
    "source_reference": pl.Utf8,
    "revision_number": pl.Int32,
    "retrieved_at_utc": pl.Datetime("us", "UTC"),
}

COLUMNS = list(SCHEMA)

# A price point is uniquely identified by source, zone, delivery interval and
# resolution; later revisions of the same interval replace earlier ones. Keeping the
# source in the key lets two sources cover the same interval side by side, which is
# what makes a cross-check possible.
KEY = ["source", "bidding_zone", "mtu_start_utc", "resolution"]


def empty_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMA)


def validate(frame: pl.DataFrame) -> pl.DataFrame:
    """Return the frame with exactly the schema's columns, in order, correctly typed."""
    missing = [name for name in SCHEMA if name not in frame.columns]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")
    unexpected = [name for name in frame.columns if name not in SCHEMA]
    if unexpected:
        raise ValueError(f"unexpected columns: {', '.join(unexpected)}")
    return frame.select(pl.col(name).cast(dtype) for name, dtype in SCHEMA.items())
