from datetime import datetime, timezone

import pytest
import requests

from conftest import price_frame
from energy_prices import cli
from energy_prices.schema import empty_frame

START = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("ENTSOE_API_TOKEN", "stub-token")


def test_check_token_reports_missing_token(monkeypatch):
    monkeypatch.delenv("ENTSOE_API_TOKEN", raising=False)
    monkeypatch.chdir("/")  # no .env to fall back on
    assert cli.main(["check-token"]) == 2


def test_check_token_succeeds_when_prices_come_back(monkeypatch, token):
    monkeypatch.setattr(cli.entsoe, "fetch_day_ahead", lambda *a, **k: price_frame(85.0, start=START))
    assert cli.main(["check-token"]) == 0


def test_check_token_fails_when_no_prices_come_back(monkeypatch, token):
    monkeypatch.setattr(cli.entsoe, "fetch_day_ahead", lambda *a, **k: empty_frame())
    assert cli.main(["check-token"]) == 1


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


def test_collect_chunks_a_long_range_and_stores_each_chunk(monkeypatch, tmp_path):
    source = cli.SOURCES["energy-charts"]
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(source, "PAUSE_SECONDS", 0.0)
    calls = []

    def fake_fetch(zone, start, end, *args, **kwargs):
        calls.append((start, end))
        return price_frame(10.0, start=START)

    monkeypatch.setattr(source, "fetch_day_ahead", fake_fetch)
    from datetime import date

    cli.collect("energy-charts", ["NL"], date(2015, 1, 1), date(2026, 1, 1))
    # 11 years at a 3-year chunk size is four requests, contiguous and non-overlapping.
    assert len(calls) == 4
    assert calls[0][0] == date(2015, 1, 1)
    assert all(calls[i][1] == calls[i + 1][0] for i in range(len(calls) - 1))
    assert calls[-1][1] == date(2026, 1, 1)
