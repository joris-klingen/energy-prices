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


# --- behaviour when a source is unavailable ----------------------------------

def _unavailable(*args, **kwargs):
    from energy_prices.errors import SourceUnavailable

    raise SourceUnavailable("503 on 4 tries")


def test_collect_reports_degradation_and_stops_hammering(monkeypatch, tmp_path):
    from datetime import date

    source = cli.SOURCES["energy-charts"]
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(source, "PAUSE_SECONDS", 0.0)
    calls = []

    def fail_after_one(zone, start, end, *args, **kwargs):
        calls.append((start, end))
        if len(calls) > 1:
            _unavailable()
        return price_frame(10.0, start=START)

    monkeypatch.setattr(source, "fetch_day_ahead", fail_after_one)
    rows, degraded = cli.collect("energy-charts", ["NL"], date(2015, 1, 1), date(2026, 1, 1))
    assert degraded is True
    # Four chunks were due; it gave up after the second failed rather than trying all.
    assert len(calls) == 2
    assert rows > 0


def test_fallback_is_used_when_the_primary_is_unavailable(monkeypatch, tmp_path):
    from datetime import date

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli.SOURCES["energy-charts"], "PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(cli.SOURCES["energy-charts"], "fetch_day_ahead", _unavailable)
    monkeypatch.setattr(
        cli.entsoe, "fetch_day_ahead", lambda *a, **k: price_frame(85.0, start=START)
    )
    rows, degraded = cli.collect_with_fallback(
        "energy-charts", "entsoe", ["NL"], date(2026, 10, 1), date(2026, 10, 2)
    )
    assert (rows, degraded) == (1, False)


def test_an_unusable_fallback_leaves_the_run_degraded(monkeypatch, tmp_path):
    from datetime import date

    def no_token(*args, **kwargs):
        raise RuntimeError("ENTSOE_API_TOKEN is not set.")

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli.SOURCES["energy-charts"], "PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(cli.SOURCES["energy-charts"], "fetch_day_ahead", _unavailable)
    monkeypatch.setattr(cli.entsoe, "fetch_day_ahead", no_token)
    rows, degraded = cli.collect_with_fallback(
        "energy-charts", "entsoe", ["NL"], date(2026, 10, 1), date(2026, 10, 2)
    )
    assert (rows, degraded) == (0, True)


@pytest.mark.parametrize(
    "source, choice, expected",
    [
        ("energy-charts", "auto", "entsoe"),
        ("entsoe", "auto", "energy-charts"),
        ("energy-charts", "none", None),
        ("energy-charts", "entsoe", "entsoe"),
        ("entsoe", "entsoe", None),  # never fall back to the source that just failed
    ],
)
def test_fallback_resolution(source, choice, expected):
    assert cli._resolve_fallback(source, choice) == expected


# --- when an outage is worth failing over ------------------------------------

def test_a_brief_outage_does_not_fail_the_run(monkeypatch, tmp_path):
    from datetime import date

    from energy_prices.store import write_frame

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    write_frame(price_frame(85.0, start=START), tmp_path)  # delivery day 2026-10-02
    assert cli.report_staleness(date(2026, 10, 4)) == 0


def test_a_long_outage_fails_the_run(monkeypatch, tmp_path):
    from datetime import date

    from energy_prices.store import write_frame

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    write_frame(price_frame(85.0, start=START), tmp_path)
    assert cli.report_staleness(date(2026, 10, 10)) == 1


def test_an_empty_archive_plus_an_outage_fails_the_run(monkeypatch, tmp_path):
    from datetime import date

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    assert cli.report_staleness(date(2026, 10, 4)) == 1


def test_daily_exits_zero_when_the_source_is_down_but_the_archive_is_fresh(monkeypatch, tmp_path):
    from datetime import datetime, timedelta

    from energy_prices.store import write_frame

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli.config, "DUCKDB_PATH", tmp_path / "prices.duckdb")
    monkeypatch.setattr(cli.SOURCES["energy-charts"], "PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(cli.SOURCES["energy-charts"], "fetch_day_ahead", _unavailable)
    monkeypatch.setattr(cli.entsoe, "fetch_day_ahead", _unavailable)

    today = datetime.now(cli.LOCAL_TZ)
    recent = (today - timedelta(hours=12)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    write_frame(price_frame(85.0, start=recent), tmp_path)
    assert cli.main(["daily"]) == 0


# --- a broken primary must not hide behind a working fallback ----------------

def test_primary_health_passes_while_the_primary_is_producing(monkeypatch, tmp_path):
    from datetime import date

    from energy_prices.store import write_frame

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    write_frame(price_frame(85.0, start=START, source="entsoe"), tmp_path)
    assert cli.report_primary_health("entsoe", date(2026, 10, 4)) == 0


def test_primary_health_fails_when_only_the_fallback_is_producing(monkeypatch, tmp_path):
    from datetime import date, timedelta

    from energy_prices.store import write_frame

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    # The primary stopped a week ago; the fallback has been covering ever since, so the
    # archive looks perfectly fresh. That is exactly the case this check exists for.
    write_frame(price_frame(85.0, start=START, source="entsoe"), tmp_path)
    write_frame(
        price_frame(85.0, start=START + timedelta(days=7), source="energy-charts"), tmp_path
    )
    assert cli.report_staleness(date(2026, 10, 9)) == 0
    assert cli.report_primary_health("entsoe", date(2026, 10, 9)) == 1


def test_primary_health_fails_when_the_primary_has_never_worked(monkeypatch, tmp_path):
    from datetime import date

    from energy_prices.store import write_frame

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    write_frame(price_frame(85.0, start=START, source="energy-charts"), tmp_path)
    assert cli.report_primary_health("entsoe", date(2026, 10, 2)) == 1


def test_daily_fails_when_the_fallback_masks_a_dead_primary(monkeypatch, tmp_path):
    from datetime import datetime, timedelta

    from energy_prices.store import write_frame

    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli.config, "DUCKDB_PATH", tmp_path / "prices.duckdb")
    monkeypatch.setattr(cli.SOURCES["energy-charts"], "PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(cli.entsoe, "fetch_day_ahead", _unavailable)

    now = datetime.now(cli.LOCAL_TZ).astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    # The fallback collects today, so the archive is fresh and report_staleness is happy.
    monkeypatch.setattr(
        cli.SOURCES["energy-charts"], "fetch_day_ahead",
        lambda *a, **k: price_frame(85.0, start=now, source="energy-charts"),
    )
    # ENTSO-E's last contribution was long ago.
    write_frame(price_frame(85.0, start=now - timedelta(days=30), source="entsoe"), tmp_path)

    assert cli.main(["daily", "--source", "entsoe"]) == 1


def test_the_default_source_is_entsoe():
    assert cli.DEFAULT == "entsoe"
    assert cli._resolve_fallback("entsoe", "auto") == "energy-charts"
