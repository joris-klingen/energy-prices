from datetime import datetime, timedelta, timezone

import pytest
import requests

from energy_prices import cli
from energy_prices.model import PricePoint


def _point() -> PricePoint:
    start = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
    return PricePoint(
        source="entsoe", bidding_zone="NL", eic_code="10YNL----------L", mtu_start_utc=start,
        mtu_end_utc=start + timedelta(hours=1), resolution="PT60M", price_eur_mwh=85.0,
        currency="EUR", unit="MWH", source_reference="doc", revision_number=1,
        retrieved_at_utc=start,
    )


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("ENTSOE_API_TOKEN", "stub-token")


def test_check_token_reports_missing_token(monkeypatch):
    monkeypatch.delenv("ENTSOE_API_TOKEN", raising=False)
    monkeypatch.chdir("/")  # no .env to fall back on
    assert cli.main(["check-token"]) == 2


def test_check_token_succeeds_when_prices_come_back(monkeypatch, token):
    monkeypatch.setattr(cli.entsoe, "fetch_day_ahead", lambda *a, **k: [_point()])
    assert cli.main(["check-token"]) == 0


def test_check_token_fails_on_rejected_token(monkeypatch, token):
    def reject(*args, **kwargs):
        raise RuntimeError("ENTSO-E rejected the API token (401).")

    monkeypatch.setattr(cli.entsoe, "fetch_day_ahead", reject)
    assert cli.main(["check-token"]) == 1


def test_check_token_fails_when_platform_unreachable(monkeypatch, token):
    def unreachable(*args, **kwargs):
        raise requests.ConnectionError("nope")

    monkeypatch.setattr(cli.entsoe, "fetch_day_ahead", unreachable)
    assert cli.main(["check-token"]) == 1
