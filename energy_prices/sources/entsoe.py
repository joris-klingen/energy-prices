"""Thin client for the ENTSO-E Transparency Platform: day-ahead prices (A44) and
imbalance prices (A85).

The XML is parsed namespace-agnostically (ENTSO-E bumps the schema version from time
to time) and expanded into one row per market time unit.
"""

from __future__ import annotations

import io
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone

import polars as pl
import requests

from .. import config
from ..errors import SourceUnavailable
from ..schema import IMBALANCE, SCHEMA, empty_frame, validate

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

# Backoff between retries of a failing request: 4s, 8s, 16s over four attempts.
BACKOFF_SECONDS = 4


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
                            "eic_code": eic or config.EIC_CODES.get(zone, ""),
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


def request(
    params: dict,
    token: str | None = None,
    session: requests.Session | None = None,
    max_retries: int = 4,
) -> requests.Response | None:
    """GET the API with retries. Returns None when ENTSO-E has no matching data.

    Shared by every ENTSO-E dataset, so they all draw the outage/mistake line the same
    way: 429 and 5xx are retried and then reported as unavailable, a rejected token and
    any other 4xx fail loudly.
    """
    params = {"securityToken": token or config.api_token(), **params}
    session = session or requests.Session()

    for attempt in range(max_retries):
        try:
            response = session.get(API_URL, params=params, timeout=60)
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                raise SourceUnavailable(f"{API_URL} unreachable: {exc}") from exc
            log.warning("%s, retrying", exc.__class__.__name__)
            time.sleep(BACKOFF_SECONDS * 2**attempt)
            continue

        if response.status_code == 200:
            return response
        if response.status_code == 401:
            raise RuntimeError("ENTSO-E rejected the API token (401).")
        if response.status_code == 400 and "No matching data" in response.text:
            return None
        if response.status_code in (429, 500, 502, 503, 504):
            if attempt == max_retries - 1:
                raise SourceUnavailable(
                    f"{API_URL} returned {response.status_code} on {max_retries} tries"
                )
            wait = BACKOFF_SECONDS * 2**attempt
            log.warning("HTTP %s, retrying in %ss", response.status_code, wait)
            time.sleep(wait)
            continue
        # Anything else is a bad request on our side, so let it fail loudly.
        response.raise_for_status()
    return None


def _window(start: datetime, end: datetime) -> dict:
    return {
        "periodStart": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
    }


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
    eic = config.eic(zone)
    params = {
        "documentType": DOCUMENT_TYPE,
        "contract_MarketAgreement.type": CONTRACT_TYPE,
        "in_Domain": eic,
        "out_Domain": eic,
        **_window(start, end),
    }
    response = request(params, token, session, max_retries)
    if response is None:
        log.info("no matching data for %s %s..%s", zone, start, end)
        return empty_frame()
    return parse_price_document(response.text, zone, datetime.now(timezone.utc))


# --- imbalance prices (A85, transparency item 17.1.G) ------------------------
#
# Answered as a ZIP of Balancing_MarketDocument XML files, or a bare
# Acknowledgement_MarketDocument when there is nothing. Each point carries a price and
# a category: A04 settles a long (surplus) position, A05 a short (deficit) one, and no
# category means one price for both. The Dutch scheme sends A04 and A05 for every
# period; they only differ when TenneT activated balancing energy both ways.

IMBALANCE_DOCUMENT_TYPE = "A85"
PRICE_TYPES = {"A04": "long", "A05": "short", "": "single"}


def parse_imbalance_document(
    xml: str | bytes, zone: str, retrieved_at: datetime
) -> pl.DataFrame:
    root = ET.fromstring(xml)
    if _local_name(root.tag) == "Acknowledgement_MarketDocument":
        reason = _find(root, "Reason")
        log.info("no imbalance data for %s: %s %s", zone, _text(reason, "code"), _text(reason, "text"))
        return empty_frame(IMBALANCE)

    document_mrid = _text(root, "mRID")
    revision = int(_text(root, "revisionNumber", "0") or 0)
    eic = _text(root, "area_Domain.mRID") or config.EIC_CODES.get(zone, "")
    rows: list[dict] = []

    for series in _findall(root, "TimeSeries"):
        currency = _text(series, "currency_Unit.name", "EUR")
        unit = _text(series, "price_Measurement_Unit.name", "MWH")
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

            # Group by category first: with curve type A03 only changes are sent, and
            # each category must be forward-filled on its own.
            by_category: dict[str, list[tuple[int, float]]] = {}
            for point in _findall(period, "Point"):
                amount = _text(point, "imbalance_Price.amount")
                if not amount:
                    continue
                category = _text(point, "imbalance_Price.category")
                if category not in PRICE_TYPES:
                    raise ValueError(f"unknown imbalance price category {category!r}")
                by_category.setdefault(category, []).append(
                    (int(_text(point, "position")), float(amount))
                )

            for category, raw in by_category.items():
                raw.sort()
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
                                "eic_code": eic,
                                "mtu_start_utc": start,
                                "mtu_end_utc": start + step,
                                "resolution": resolution,
                                "price_type": PRICE_TYPES[category],
                                "price_eur_mwh": price,
                                "currency": currency,
                                "unit": unit,
                                "source_reference": document_mrid,
                                "revision_number": revision,
                                "retrieved_at_utc": retrieved_at,
                            }
                        )
    if not rows:
        return empty_frame(IMBALANCE)
    return validate(pl.DataFrame(rows, schema=IMBALANCE.schema), IMBALANCE)


def parse_imbalance_payload(content: bytes, zone: str, retrieved_at: datetime) -> pl.DataFrame:
    """Unpack the ZIP the API answers with, or parse a bare XML acknowledgement."""
    if not content.startswith(b"PK"):
        return parse_imbalance_document(content, zone, retrieved_at)
    frames = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in sorted(archive.namelist()):
            if member.lower().endswith(".xml"):
                frames.append(parse_imbalance_document(archive.read(member), zone, retrieved_at))
    frames = [frame for frame in frames if not frame.is_empty()]
    return pl.concat(frames) if frames else empty_frame(IMBALANCE)


def fetch_imbalance(
    zone: str,
    start: datetime,
    end: datetime,
    token: str | None = None,
    session: requests.Session | None = None,
    max_retries: int = 4,
) -> pl.DataFrame:
    """Imbalance prices for [start, end) in UTC; the API takes at most a year."""
    params = {
        "documentType": IMBALANCE_DOCUMENT_TYPE,
        "controlArea_Domain": config.eic(zone),
        **_window(start, end),
    }
    response = request(params, token, session, max_retries)
    if response is None:
        log.info("no imbalance data for %s %s..%s", zone, start, end)
        return empty_frame(IMBALANCE)
    return parse_imbalance_payload(response.content, zone, datetime.now(timezone.utc))
