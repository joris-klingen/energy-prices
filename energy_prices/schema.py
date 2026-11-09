"""The declared shape of every dataset the archive holds.

Each dataset is declared here **once**: its row schema, the key that deduplicates it,
and how it is partitioned on disk. Every source returns a frame validated against its
dataset's schema, so a source cannot invent a column or quietly change a dtype, and the
Parquet files on disk stay uniform.

`SCHEMA`, `KEY`, `empty_frame()` and `validate()` without a dataset argument refer to the
day-ahead prices, which is what the archive held first.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

UTC = pl.Datetime("us", "UTC")


@dataclass(frozen=True)
class Dataset:
    """One table in the archive.

    Rows are written to `data/<name>/<partition_by>=<value>/<period>.parquet`, where the
    period is `time_column` formatted with `period_format` - monthly for the dense
    electricity series, yearly for the sparse daily commodity ones.
    """

    name: str
    schema: dict[str, pl.DataType]
    # A row is uniquely identified by these columns; later revisions replace earlier
    # ones. Keeping the source in every key lets two sources cover the same interval
    # side by side, which is what makes a cross-check possible.
    key: tuple[str, ...]
    time_column: str
    partition_by: str
    partition_label: str
    period_format: str = "%Y-%m"
    # Columns that `coverage` groups by, after source and the partition column.
    coverage_by: tuple[str, ...] = ()
    # Row order within a partition file. Defaults to time, then the key.
    order: tuple[str, ...] = ()

    @property
    def sort_order(self) -> list[str]:
        return list(self.order) or [self.time_column, *self.key]

    @property
    def columns(self) -> list[str]:
        return list(self.schema)


DAY_AHEAD = Dataset(
    name="day_ahead",
    schema={
        "source": pl.Utf8,
        "bidding_zone": pl.Utf8,
        "eic_code": pl.Utf8,
        "mtu_start_utc": UTC,
        "mtu_end_utc": UTC,
        "resolution": pl.Utf8,
        "price_eur_mwh": pl.Float64,
        "currency": pl.Utf8,
        "unit": pl.Utf8,
        # Provenance within the source: the document mRID for ENTSO-E, empty elsewhere.
        "source_reference": pl.Utf8,
        "revision_number": pl.Int32,
        "retrieved_at_utc": UTC,
    },
    key=("source", "bidding_zone", "mtu_start_utc", "resolution"),
    time_column="mtu_start_utc",
    partition_by="bidding_zone",
    partition_label="zone",
    coverage_by=("resolution",),
    order=("mtu_start_utc", "resolution"),
)

DATASETS: dict[str, Dataset] = {DAY_AHEAD.name: DAY_AHEAD}


def register(dataset: Dataset) -> Dataset:
    DATASETS[dataset.name] = dataset
    return dataset


# Clearing prices of the pan-European intraday auctions (SIDC IDA1-3, since 2024-06-13):
# the day-ahead row shape plus which of the three auctions cleared the price.
INTRADAY_AUCTION = register(
    Dataset(
        name="intraday_auction",
        schema={**DAY_AHEAD.schema, "auction": pl.Utf8},
        key=("source", "bidding_zone", "mtu_start_utc", "resolution", "auction"),
        time_column="mtu_start_utc",
        partition_by="bidding_zone",
        partition_label="zone",
        coverage_by=("auction", "resolution"),
    )
)

# Imbalance (settlement) prices: what a balance responsible party pays or receives for
# its deviation in each imbalance settlement period. A period can carry more than one
# price - the Dutch scheme has separate prices for a short and a long position when
# TenneT activated balancing energy in both directions - so `price_type` is part of the
# key alongside the interval.
IMBALANCE = register(
    Dataset(
        name="imbalance",
        schema={
            "source": pl.Utf8,
            "bidding_zone": pl.Utf8,
            "eic_code": pl.Utf8,
            "mtu_start_utc": UTC,
            "mtu_end_utc": UTC,
            "resolution": pl.Utf8,
            # "short", "long" or "single": which position the price settles.
            "price_type": pl.Utf8,
            "price_eur_mwh": pl.Float64,
            "currency": pl.Utf8,
            "unit": pl.Utf8,
            "source_reference": pl.Utf8,
            "revision_number": pl.Int32,
            "retrieved_at_utc": UTC,
        },
        key=("source", "bidding_zone", "mtu_start_utc", "resolution", "price_type"),
        time_column="mtu_start_utc",
        partition_by="bidding_zone",
        partition_label="zone",
        coverage_by=("resolution", "price_type"),
    )
)

# Daily commodity prices - natural gas, carbon allowances. One row per product,
# contract and trading day; the contract says what was priced (a spot index, the front
# month, an auction clearing price), since those are different numbers for one day.
COMMODITY = register(
    Dataset(
        name="commodity",
        schema={
            "source": pl.Utf8,
            "product": pl.Utf8,
            "contract": pl.Utf8,
            "trade_date": pl.Date,
            "price": pl.Float64,
            "currency": pl.Utf8,
            "unit": pl.Utf8,
            "source_reference": pl.Utf8,
            "revision_number": pl.Int32,
            "retrieved_at_utc": UTC,
        },
        key=("source", "product", "contract", "trade_date"),
        time_column="trade_date",
        partition_by="product",
        partition_label="product",
        period_format="%Y",
        coverage_by=("contract",),
    )
)

SCHEMA = DAY_AHEAD.schema
COLUMNS = DAY_AHEAD.columns
KEY = list(DAY_AHEAD.key)


def empty_frame(dataset: Dataset = DAY_AHEAD) -> pl.DataFrame:
    return pl.DataFrame(schema=dataset.schema)


def validate(frame: pl.DataFrame, dataset: Dataset = DAY_AHEAD) -> pl.DataFrame:
    """Return the frame with exactly the dataset's columns, in order, correctly typed."""
    missing = [name for name in dataset.schema if name not in frame.columns]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")
    unexpected = [name for name in frame.columns if name not in dataset.schema]
    if unexpected:
        raise ValueError(f"unexpected columns: {', '.join(unexpected)}")
    return frame.select(pl.col(name).cast(dtype) for name, dtype in dataset.schema.items())
