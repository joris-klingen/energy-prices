"""Upstream trouble must be distinguishable from our own bugs."""

from datetime import date, datetime, timezone

import pytest
import requests

from energy_prices.errors import SourceUnavailable
from energy_prices.sources import energycharts, entsoe

BASE = 1759269600


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeSession:
    """Replays the given responses, repeating the last one once exhausted."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        reply = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(energycharts.time, "sleep", lambda _: None)
    monkeypatch.setattr(entsoe.time, "sleep", lambda _: None)


# --- Energy-Charts -----------------------------------------------------------

def test_persistent_5xx_is_reported_as_unavailable_not_a_crash():
    session = FakeSession(FakeResponse(503, text="no server is available"))
    with pytest.raises(SourceUnavailable, match="503"):
        energycharts.fetch_day_ahead("NL", date(2026, 10, 1), date(2026, 10, 2), session, 3)
    assert session.calls == 3


def test_a_5xx_that_clears_is_retried_and_succeeds():
    good = FakeResponse(200, {"unit": "EUR / MWh", "unix_seconds": [BASE], "price": [85.0]})
    session = FakeSession(FakeResponse(503), good)
    frame = energycharts.fetch_day_ahead("NL", date(2026, 10, 1), date(2026, 10, 2), session, 3)
    assert frame.height == 1


def test_connection_failure_is_reported_as_unavailable():
    session = FakeSession(requests.ConnectionError("no route"))
    with pytest.raises(SourceUnavailable, match="unreachable"):
        energycharts.fetch_day_ahead("NL", date(2026, 10, 1), date(2026, 10, 2), session, 2)


def test_exhausted_rate_limiting_is_reported_as_unavailable():
    session = FakeSession(FakeResponse(429, headers={"retry-after": "0"}))
    with pytest.raises(SourceUnavailable, match="rate limiting"):
        energycharts.fetch_day_ahead("NL", date(2026, 10, 1), date(2026, 10, 2), session, 2)


def test_a_bad_request_still_fails_loudly_because_it_is_our_mistake():
    session = FakeSession(FakeResponse(400, text="bad parameter"))
    with pytest.raises(requests.HTTPError):
        energycharts.fetch_day_ahead("NL", date(2026, 10, 1), date(2026, 10, 2), session, 3)


def test_a_404_still_means_no_data_rather_than_an_outage():
    session = FakeSession(FakeResponse(404))
    frame = energycharts.fetch_day_ahead("NL", date(2026, 10, 1), date(2026, 10, 2), session, 3)
    assert frame.is_empty()


# --- ENTSO-E -----------------------------------------------------------------

START = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)


def test_entsoe_persistent_5xx_is_reported_as_unavailable():
    session = FakeSession(FakeResponse(503))
    with pytest.raises(SourceUnavailable, match="503"):
        entsoe.fetch_day_ahead("NL", START, END, "token", session, 3)


def test_entsoe_connection_failure_is_reported_as_unavailable():
    session = FakeSession(requests.ConnectionError("reset"))
    with pytest.raises(SourceUnavailable, match="unreachable"):
        entsoe.fetch_day_ahead("NL", START, END, "token", session, 2)


def test_a_rejected_token_is_not_an_outage():
    session = FakeSession(FakeResponse(401))
    with pytest.raises(RuntimeError, match="rejected the API token") as caught:
        entsoe.fetch_day_ahead("NL", START, END, "token", session, 3)
    # SourceUnavailable subclasses RuntimeError, so be explicit: a bad credential is
    # a configuration problem that must stay loud, not a transient outage.
    assert not isinstance(caught.value, SourceUnavailable)
    assert session.calls == 1
