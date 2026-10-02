"""Thin client for the ENTSO-E Transparency Platform day-ahead price endpoint.

The XML is parsed namespace-agnostically (ENTSO-E bumps the schema version from time
to time) and expanded into one row per market time unit.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import polars as pl
import requests

from .. import config
from ..schema import SCHEMA, empty_frame, validate

log = logging.getLogger(__name__)

RESOLUTION_MINUTES = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "P1D": 1440}

SOURCE = "entsoe"

# ENTSO-E Transparency Platform RESTful API.
API_URL = "https://web-api.tp.entsoe.eu/api"

# A44 = Price Document; A01 = day-ahead market agreement type.
DOCUMENT_TYPE = "A44"
CONTRACT_TYPE = "A01"

# ENTSO-E rejects A44 queries spanning more than one year.
CHUNK_DAYS = 365

# Day-ahead prices have been published since the start of the SDAC history.
ARCHIVE_START = date(2015, 1, 1)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _findall(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _text(element: ET.Element | None, name: str, default: str = "") -> str:
    if element is None:
        return default
    child = _find(element, name)
    return default if child is None or child.text is None else child.text.strip()


def _parse_utc(stamp: str) -> datetime:
    """ENTSO-E timestamps look like 2026-09-10T22:00Z."""
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def parse_price_document(xml: str, zone: str, retrieved_at: datetime) -> pl.DataFrame:
    """Expand a Publication_MarketDocument into one row per market time unit.

    The document is a tree rather than a table, so it is walked row by row and turned
    into a frame at the end.
    """
    root = ET.fromstring(xml)
    if _local_name(root.tag) == "Acknowledgement_MarketDocument":
        reason = _find(root, "Reason")
        log.info("no data for %s: %s %s", zone, _text(reason, "code"), _text(reason, "text"))
        return empty_frame()

    document_mrid = _text(root, "mRID")
    revision = int(_text(root, "revisionNumber", "0") or 0)
    rows: list[dict] = []

    for series in _findall(root, "TimeSeries"):
        eic = _text(series, "in_Domain.mRID")
        currency = _text(series, "currency_Unit.name", "EUR")
        unit = _text(series, "price_Measure_Unit.name", "MWH")
        curve_type = _text(series, "curveType", "A01")

        for period in _findall(series, "Period"):
            interval = _find(period, "timeInterval")
            period_start = _parse_utc(_text(interval, "start"))
            period_end = _parse_utc(_text(interval, "end"))
            resolution = _text(period, "resolution")
            minutes = RESOLUTION_MINUTES.get(resolution)
            if minutes is None:
                log.warning("skipping unknown resolution %r", resolution)
                continue
            step = timedelta(minutes=minutes)
            slots = int((period_end - period_start) / step)

            raw = []
            for point in _findall(period, "Point"):
                raw.append((int(_text(point, "position")), float(_text(point, "price.amount"))))
            raw.sort()

            # Curve type A03 omits repeated values: a price holds until the next
            # position. A01 is dense, so the fill below is a no-op for it.
            for index, (position, price) in enumerate(raw):
                last = raw[index + 1][0] - 1 if index + 1 < len(raw) else slots
                if curve_type == "A01":
                    last = position
                for slot in range(position, last + 1):
                    start = period_start + (slot - 1) * step
                    if start >= period_end:
                        break
                    rows.append(
                        {
                            "source": SOURCE,
                            "bidding_zone": zone,
                            "eic_code": eic or config.ZONES.get(zone, ""),
                            "mtu_start_utc": start,
                            "mtu_end_utc": start + step,
                            "resolution": resolution,
                            "price_eur_mwh": price,
                            "currency": currency,
                            "unit": unit,
                            "source_reference": document_mrid,
                            "revision_number": revision,
                            "retrieved_at_utc": retrieved_at,
                        }
                    )
    if not rows:
        return empty_frame()
    return validate(pl.DataFrame(rows, schema=SCHEMA))


def fetch_day_ahead(
    zone: str,
    start: datetime,
    end: datetime,
    token: str | None = None,
    session: requests.Session | None = None,
    max_retries: int = 4,
) -> pl.DataFrame:
    """Fetch day-ahead prices for [start, end) in UTC. Both hourly and 15-minute
    series are kept when ENTSO-E publishes both for the same delivery day."""
    eic = config.ZONES[zone]
    params = {
        "securityToken": token or config.api_token(),
        "documentType": DOCUMENT_TYPE,
        "contract_MarketAgreement.type": CONTRACT_TYPE,
        "in_Domain": eic,
        "out_Domain": eic,
        "periodStart": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
    }
    session = session or requests.Session()

    for attempt in range(max_retries):
        response = session.get(API_URL, params=params, timeout=60)
        if response.status_code == 200:
            return parse_price_document(response.text, zone, datetime.now(timezone.utc))
        if response.status_code == 401:
            raise RuntimeError("ENTSO-E rejected the API token (401).")
        if response.status_code == 400 and "No matching data" in response.text:
            log.info("no matching data for %s %s..%s", zone, start, end)
            return empty_frame()
        if response.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
            wait = 2 ** (attempt + 1)
            log.warning("HTTP %s, retrying in %ss", response.status_code, wait)
            time.sleep(wait)
            continue
        response.raise_for_status()
    return empty_frame()
