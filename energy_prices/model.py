"""The one row type every source produces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PricePoint:
    source: str
    bidding_zone: str
    eic_code: str
    mtu_start_utc: datetime
    mtu_end_utc: datetime
    resolution: str
    price_eur_mwh: float
    currency: str
    unit: str
    # Provenance within the source: the document mRID for ENTSO-E, empty elsewhere.
    source_reference: str
    revision_number: int
    retrieved_at_utc: datetime
