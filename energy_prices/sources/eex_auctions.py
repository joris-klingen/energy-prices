"""EU carbon allowance prices from the EEX primary-market auction reports.

EEX is the common auction platform for the EU ETS and must publish every auction's
clearing price under the Auctioning Regulation. The reports carry no open licence; they
are archived here as published regulatory results, with EEX credited. Secondary-market
EUA prices (EEX spot, ICE futures) are licensed data and are deliberately not used.

One report per year. From 2020 each year is its own .xlsx; 2012-2019 exist only inside
an archive zip of legacy .xls files. Layouts drift between years - the header row moves,
the price column is "Auction Price €/tCO2" or "Auction Price EUR/tCO2", the sheet was
renamed to "EU ETS1" in 2026 - so columns are found by name, never by position.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import date, datetime, timezone

import fastexcel
import polars as pl
import requests

from ..schema import COMMODITY, empty_frame, validate
from . import _http

log = logging.getLogger(__name__)

SOURCE = "eex"
DATASET = COMMODITY
YEAR_URL = (
    "https://public.eex-group.com/eex/eua-auction-report/"
    "emission-spot-primary-market-auction-report-{year}-data.xlsx"
)
ARCHIVE_URL = (
    "https://www.eex.com/fileadmin/EEX/Downloads/Markets/Environmentals/"
    "EUA_Emission_Spot_Primary_Market_Auction_Report/Archive_Reports/"
    "emission-spot-primary-market-auction-report-2012-2025-data.zip"
)
# Years only available inside the archive zip.
FIRST_YEARLY_FILE = 2020

ARCHIVE_START = date(2012, 1, 1)
LOOKBACK_DAYS = 14
# Auctions pause over Christmas and New Year for about three weeks.
STALE_AFTER_DAYS = 25

# Contract codes: T2PA/T3PA are general allowances (phase 2, phase 3 onward), EAA2/EAA3
# aviation allowances.
PRODUCTS = {"T2PA": "EUA", "T3PA": "EUA", "EAA2": "EUAA", "EAA3": "EUAA"}


def auctioneer(name: str) -> str:
    """Reduce an auction name to who auctioned it: EU, DE, PL, NIR, ...

    'Auction 4. Period CAP3 EU' -> 'EU', 'Auction 3. Period CAP2-PL' -> 'PL',
    'Auction 3. Period DE' -> 'DE', 'EUAA Auction CAP3 PL' -> 'PL', 'EU (TCAP)' -> 'EU',
    'NL' -> 'NL'. The product (EUA or EUAA) comes from the contract code, not the name.
    """
    name = re.sub(r"^Auction \d+\. Period\s*", "", name.strip())
    name = re.sub(r"^EUAA Auction\s*", "", name)
    name = re.sub(r"^CAP\d[\s-]*", "", name)
    name = re.sub(r"\s*\(.*\)$", "", name)
    return name.strip()


def _header_row(sheet: pl.DataFrame) -> int | None:
    for index in range(min(15, sheet.height)):
        if sheet.row(index)[0] == "Date":
            return index
    return None


def parse_report(content: bytes, retrieved_at: datetime, reference: str = "") -> pl.DataFrame:
    """Every successful auction in one yearly report, one row per auction."""
    reader = fastexcel.read_excel(content)
    frames = []
    for sheet_name in reader.sheet_names:
        sheet = reader.load_sheet_by_name(sheet_name, header_row=None).to_polars()
        header = _header_row(sheet)
        if header is None:  # e.g. the "Caption" sheet listing country codes
            continue
        names = [str(value) if value is not None else "" for value in sheet.row(header)]
        price_column = next((i for i, n in enumerate(names) if n.startswith("Auction Price")), None)
        if price_column is None:
            raise ValueError(f"no auction price column in sheet {sheet_name!r}")
        if "/tCO2" not in names[price_column]:
            raise ValueError(f"unexpected price unit in {names[price_column]!r}")
        columns = {
            "date": names.index("Date"),
            "name": names.index("Auction Name"),
            "contract": names.index("Contract"),
            "price": price_column,
        }
        status = names.index("Status") if "Status" in names else None
        body = sheet.slice(header + 1).select(
            date=pl.nth(columns["date"]).cast(pl.Utf8),
            name=pl.nth(columns["name"]).cast(pl.Utf8),
            code=pl.nth(columns["contract"]).cast(pl.Utf8),
            price=pl.nth(columns["price"]).cast(pl.Utf8).cast(pl.Float64, strict=False),
            status=pl.nth(status).cast(pl.Utf8) if status is not None else pl.lit("successful"),
        )
        frames.append(body)

    if not frames:
        raise ValueError("no auction sheet in the report")
    rows = (
        pl.concat(frames)
        .filter(pl.col("date").str.contains(r"^\d{4}-\d{2}-\d{2}"))
        # Cancelled auctions have no clearing price.
        .filter(pl.col("status").str.to_lowercase().str.contains("success") & pl.col("price").is_not_null())
    )
    unknown = sorted(set(rows["code"].to_list()) - set(PRODUCTS))
    if unknown:
        raise ValueError(f"unknown auction contract code(s): {', '.join(unknown)}")
    if rows.is_empty():
        return empty_frame(COMMODITY)

    frame = rows.select(
        source=pl.lit(SOURCE),
        product=pl.col("code").replace_strict(PRODUCTS),
        contract=pl.lit("primary-auction-")
        + pl.col("name").map_elements(auctioneer, return_dtype=pl.Utf8),
        trade_date=pl.col("date").str.slice(0, 10).str.to_date("%Y-%m-%d"),
        price=pl.col("price"),
        currency=pl.lit("EUR"),
        unit=pl.lit("TCO2"),
        source_reference=pl.lit(reference) + pl.lit(":") + pl.col("code"),
        revision_number=pl.lit(0),
        retrieved_at_utc=pl.lit(retrieved_at),
    )
    duplicated = frame.filter(frame.select(COMMODITY.key).is_duplicated())
    if not duplicated.is_empty():
        raise ValueError(f"two auctions share a key, e.g. {duplicated.row(0, named=True)}")
    return validate(frame, COMMODITY)


def _yearly(year: int, session: requests.Session) -> pl.DataFrame:
    url = YEAR_URL.format(year=year)
    response = _http.get(url, session=session)
    if response.status_code == 404:
        log.info("  no %s auction report yet", year)
        return empty_frame(COMMODITY)
    response.raise_for_status()
    return parse_report(response.content, datetime.now(timezone.utc), f"report-{year}")


def _archived(years: set[int], session: requests.Session) -> pl.DataFrame:
    response = _http.get(ARCHIVE_URL, session=session, timeout=300)
    response.raise_for_status()
    retrieved = datetime.now(timezone.utc)
    frames = []
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for member in archive.namelist():
            match = re.search(r"report-(\d{4})-data\.xlsx?$", member)
            if match and int(match.group(1)) in years:
                frames.append(parse_report(archive.read(member), retrieved, f"report-{match.group(1)}"))
    return pl.concat(frames) if frames else empty_frame(COMMODITY)


def fetch(start: date, end: date, session: requests.Session | None = None) -> pl.DataFrame:
    """Auction results for trade dates in [start, end)."""
    session = session or requests.Session()
    years = set(range(start.year, end.year + 1))
    old = {year for year in years if year < FIRST_YEARLY_FILE}
    frames = [_archived(old, session)] if old else []
    frames += [_yearly(year, session) for year in sorted(years - old)]
    frame = pl.concat(frames) if frames else empty_frame(COMMODITY)
    return frame.filter((pl.col("trade_date") >= start) & (pl.col("trade_date") < end))
