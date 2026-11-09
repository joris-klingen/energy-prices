"""Monthly Dutch TTF gas price from the World Bank Pink Sheet, CC BY 4.0.

There is no free daily TTF series that may be archived: the exchanges (ICE Endex, EEX)
license their settlement data and the free mirrors forbid redistribution. The Pink
Sheet's "Natural gas, Europe" column is the TTF monthly average in USD/mmbtu from April
2015 - before that it is a border-price blend, so the archive starts there rather than
splicing two definitions together.

The workbook's URL changes with every monthly release (and stale URLs keep serving old
data), so the current link is read from the commodity-markets page on every run.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime, timezone

import fastexcel
import polars as pl
import requests

from ..schema import COMMODITY, empty_frame, validate
from . import _http

log = logging.getLogger(__name__)

SOURCE = "worldbank"
DATASET = COMMODITY
PAGE_URL = "https://www.worldbank.org/en/research/commodity-markets"
LINK_PATTERN = re.compile(r'href="(https://[^"]*CMO-Historical-Data-Monthly\.xlsx)"')
SHEET = "Monthly Prices"
COLUMN = "Natural gas, Europe"

PRODUCT = "TTF"
CONTRACT = "monthly-average"
ARCHIVE_START = date(2015, 4, 1)
# Published around the 3rd of the month for the month before; three months without a
# new row means the scrape has broken, not that the World Bank is late.
STALE_AFTER_DAYS = 100
LOOKBACK_DAYS = 400  # the whole file comes down anyway; keep revisions to the last year


def workbook_url(page_html: str) -> str:
    match = LINK_PATTERN.search(page_html)
    if not match:
        raise RuntimeError(
            f"no CMO-Historical-Data-Monthly.xlsx link on {PAGE_URL} - the page layout changed"
        )
    return match.group(1)


def parse_pink_sheet(content: bytes, retrieved_at: datetime, reference: str = "") -> pl.DataFrame:
    """Pull the TTF column out of the Pink Sheet workbook."""
    sheet = fastexcel.read_excel(content).load_sheet_by_name(SHEET, header_row=None).to_polars()
    header_row = next(
        (i for i in range(min(20, sheet.height)) if COLUMN in [str(v) for v in sheet.row(i)]),
        None,
    )
    if header_row is None:
        raise ValueError(f"no {COLUMN!r} column in the {SHEET!r} sheet")
    headers = [str(v) for v in sheet.row(header_row)]
    column = headers.index(COLUMN)
    unit = str(sheet.row(header_row + 1)[column] or "")
    if "mmbtu" not in unit:
        raise ValueError(f"expected {COLUMN!r} in $/mmbtu, got {unit!r}")

    body = sheet.slice(header_row + 2).select(
        period=pl.nth(0).cast(pl.Utf8), value=pl.nth(column).cast(pl.Utf8)
    )
    frame = (
        body.filter(pl.col("period").str.contains(r"^\d{4}M\d{2}$"))
        .with_columns(
            trade_date=pl.col("period").str.replace("M", "-").add("-01").str.to_date("%Y-%m-%d"),
            # Missing months are an ellipsis character; anything non-numeric is a gap.
            price=pl.col("value").cast(pl.Float64, strict=False),
        )
        .drop_nulls("price")
        .filter(pl.col("trade_date") >= ARCHIVE_START)
    )
    if frame.is_empty():
        return empty_frame(COMMODITY)
    return validate(
        frame.select(
            source=pl.lit(SOURCE),
            product=pl.lit(PRODUCT),
            contract=pl.lit(CONTRACT),
            trade_date=pl.col("trade_date"),
            price=pl.col("price"),
            currency=pl.lit("USD"),
            unit=pl.lit("MMBTU"),
            source_reference=pl.lit(reference),
            revision_number=pl.lit(0),
            retrieved_at_utc=pl.lit(retrieved_at),
        ),
        COMMODITY,
    )


def fetch(start: date, end: date, session: requests.Session | None = None) -> pl.DataFrame:
    """Monthly TTF averages for months starting in [start, end)."""
    session = session or requests.Session()
    page = _http.get(PAGE_URL, session=session)
    page.raise_for_status()
    url = workbook_url(page.text)
    log.info("  pink sheet at %s", url)
    workbook = _http.get(url, session=session)
    workbook.raise_for_status()
    frame = parse_pink_sheet(workbook.content, datetime.now(timezone.utc), url.rsplit("/", 3)[-3])
    return frame.filter((pl.col("trade_date") >= start) & (pl.col("trade_date") < end))
