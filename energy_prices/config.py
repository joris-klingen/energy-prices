"""Shared configuration: bidding zones, storage paths, the ENTSO-E token."""

from __future__ import annotations

import os
from pathlib import Path

# EIC codes of the bidding zones the daily job collects. Keys double as the
# Energy-Charts `bzn` code, so use its spelling (DE-LU, not DE_LU). Extend this dict to
# widen the scope; nothing else in the code is NL-specific. NL plus the zones it is
# coupled to by interconnector (DE-LU, BE, NorNed to NO2, COBRAcable to DK1) and FR as
# the other large continental reference price. GB is left out: it left SDAC with
# Brexit and ENTSO-E stopped publishing its prices in 2021.
ZONES: dict[str, str] = {
    "NL": "10YNL----------L",
    "DE-LU": "10Y1001A1001A82H",
    "BE": "10YBE----------2",
    "FR": "10YFR-RTE------C",
    "DK1": "10YDK-1--------W",
    "NO2": "10YNO-2--------T",
}

# Zones that no longer clear but whose history is worth having. Germany and Austria
# were one zone until the split on 2018-10-01; DE-LU starts the day after. These are
# backfilled on request (--zones DE-AT-LU) and never topped up by the daily job.
RETIRED_ZONES: dict[str, str] = {
    "DE-AT-LU": "10Y1001A1001A63L",
}

EIC_CODES: dict[str, str] = {**ZONES, **RETIRED_ZONES}

# Imbalance prices are collected for the Dutch control area only.
IMBALANCE_ZONES: list[str] = ["NL"]


def eic(zone: str) -> str:
    try:
        return EIC_CODES[zone]
    except KeyError:
        raise RuntimeError(
            f"unknown bidding zone {zone!r}; known zones: {', '.join(EIC_CODES)}"
        ) from None

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


def has_api_token() -> bool:
    return bool(os.environ.get("ENTSOE_API_TOKEN", "").strip() or _token_from_dotenv())


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
