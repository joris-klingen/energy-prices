"""Static configuration: bidding zones, API endpoint, storage layout."""

from __future__ import annotations

import os
from pathlib import Path

# ENTSO-E Transparency Platform RESTful API.
API_URL = "https://web-api.tp.entsoe.eu/api"

# EIC codes of the bidding zones we collect. Extend this dict to widen the scope;
# nothing else in the code is NL-specific.
ZONES: dict[str, str] = {
    "NL": "10YNL----------L",
}

# A44 = Price Document; A01 = day-ahead market agreement type.
DOCUMENT_TYPE = "A44"
CONTRACT_TYPE = "A01"

# ENTSO-E rejects A44 queries spanning more than one year.
MAX_QUERY_DAYS = 365

# Day-ahead prices for delivery day D are published around 12:45 CET on D-1 and are
# final thereafter, but late corrections happen. The daily job re-fetches this many
# days back so corrections land in the archive.
LOOKBACK_DAYS = 7

DATA_DIR = Path(os.environ.get("ENERGY_PRICES_DATA_DIR", "data"))
DUCKDB_PATH = Path(os.environ.get("ENERGY_PRICES_DUCKDB", DATA_DIR / "energy_prices.duckdb"))


def api_token() -> str:
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "ENTSOE_API_TOKEN is not set. Register at https://transparency.entsoe.eu "
            "and email transparency@entsoe.eu with subject 'Restful API access'."
        )
    return token
