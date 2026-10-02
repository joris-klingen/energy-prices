"""Shared configuration: bidding zones, storage paths, the ENTSO-E token."""

from __future__ import annotations

import os
from pathlib import Path

# EIC codes of the bidding zones we collect. Extend this dict to widen the scope;
# nothing else in the code is NL-specific.
ZONES: dict[str, str] = {
    "NL": "10YNL----------L",
}

# Day-ahead prices for delivery day D are published around 12:45 CET on D-1 and are
# final thereafter, but late corrections happen. The daily job re-fetches this many
# days back so corrections land in the archive.
LOOKBACK_DAYS = 7

DATA_DIR = Path(os.environ.get("ENERGY_PRICES_DATA_DIR", "data"))
DUCKDB_PATH = Path(os.environ.get("ENERGY_PRICES_DUCKDB", DATA_DIR / "energy_prices.duckdb"))


def _token_from_dotenv() -> str:
    """Read ENTSOE_API_TOKEN from a local, git-ignored .env so interactive runs do
    not need the variable exported. The scheduled job uses the environment instead."""
    dotenv = Path(".env")
    if not dotenv.is_file():
        return ""
    for line in dotenv.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "ENTSOE_API_TOKEN":
            return value.strip().strip("\"'")
    return ""


def api_token() -> str:
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip() or _token_from_dotenv()
    if not token:
        raise RuntimeError(
            "ENTSOE_API_TOKEN is not set. Export it, or put it in a .env file. "
            "Generate one at https://transparency.entsoe.eu -> My Account Settings; "
            "if there is no token button yet, email transparency@entsoe.eu with "
            "subject 'Restful API access' and your account address in the body."
        )
    return token
