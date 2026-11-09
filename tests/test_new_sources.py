import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from conftest import NOW
from energy_prices import cli, collectors, store
from energy_prices.errors import SourceUnavailable
from energy_prices.schema import COMMODITY, IMBALANCE, INTRADAY_AUCTION, empty_frame
from energy_prices.sources import eex_auctions, entsoe, worldbank

FIXTURES = Path(__file__).parent / "fixtures"


# --- ENTSO-E imbalance (A85) --------------------------------------------------

def _imbalance() -> pl.DataFrame:
    return entsoe.parse_imbalance_document((FIXTURES / "a85_nl.xml").read_bytes(), "NL", NOW)


def test_imbalance_categories_become_long_and_short_rows():
    frame = _imbalance()
    assert frame.columns == IMBALANCE.columns
    assert sorted(set(frame["price_type"])) == ["long", "short"]
    assert frame.height == 8  # four quarter-hours, two prices each
    assert set(frame["resolution"]) == {"PT15M"}
    assert set(frame["revision_number"]) == {2}


def test_imbalance_a03_points_are_filled_per_category():
    frame = _imbalance().sort("mtu_start_utc")
    long = frame.filter(pl.col("price_type") == "long")["price_eur_mwh"].to_list()
    short = frame.filter(pl.col("price_type") == "short")["price_eur_mwh"].to_list()
    # Position 2 repeats position 1; the prices only part ways in the third slot.
    assert long == [78.27, 78.27, -12.5, 80.1]
    assert short == [78.27, 78.27, 210.0, 80.1]


def test_imbalance_payload_is_unzipped():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc_1.xml", (FIXTURES / "a85_nl.xml").read_bytes())
    frame = entsoe.parse_imbalance_payload(buffer.getvalue(), "NL", NOW)
    assert frame.height == 8


def test_imbalance_acknowledgement_means_no_rows():
    payload = (FIXTURES / "acknowledgement.xml").read_bytes()
    assert entsoe.parse_imbalance_payload(payload, "NL", NOW).is_empty()


def test_imbalance_rows_with_different_price_types_are_both_stored(tmp_path):
    store.write_frame(_imbalance(), tmp_path, IMBALANCE)
    stored = pl.read_parquet(store.partition_path("NL", "2026-11", tmp_path, IMBALANCE))
    assert stored.height == 8


# --- ENTSO-E intraday auctions -----------------------------------------------

def test_intraday_auction_rows_carry_the_auction():
    day_ahead = entsoe.parse_price_document((FIXTURES / "a44_nl.xml").read_text(), "NL", NOW)
    frame = entsoe.label_auction(day_ahead, "IDA2")
    assert frame.columns == INTRADAY_AUCTION.columns
    assert set(frame["auction"]) == {"IDA2"}
    assert frame.height == day_ahead.height


# --- World Bank TTF ------------------------------------------------------------

def test_pink_sheet_yields_ttf_from_april_2015():
    frame = worldbank.parse_pink_sheet((FIXTURES / "pink_sheet.xlsx").read_bytes(), NOW)
    assert frame.columns == COMMODITY.columns
    # March 2015 predates the TTF basis and May 2015 is an ellipsis gap.
    assert frame["trade_date"].to_list() == [date(2015, 4, 1), date(2026, 10, 1)]
    # The Europe column, not its neighbours.
    assert frame["price"].to_list() == [6.77, 28.5]
    assert set(frame["unit"]) == {"MMBTU"} and set(frame["currency"]) == {"USD"}


def test_pink_sheet_link_is_found_on_the_page():
    html = '<a href="https://thedocs.worldbank.org/en/doc/abc-0050012026/related/CMO-Historical-Data-Monthly.xlsx">x</a>'
    assert worldbank.workbook_url(html).endswith("abc-0050012026/related/CMO-Historical-Data-Monthly.xlsx")
    with pytest.raises(RuntimeError, match="page layout changed"):
        worldbank.workbook_url("<html></html>")


# --- EEX auctions ---------------------------------------------------------------

@pytest.mark.parametrize(
    "name, expected",
    [
        ("Auction 4. Period CAP3 EU", "EU"),
        ("Auction 3. Period CAP2-PL", "PL"),
        ("Auction 3. Period DE", "DE"),
        ("Auction 4. Period CAP3 NIR", "NIR"),
        ("EUAA Auction CAP3 PL", "PL"),
        ("EUAA Auction DE", "DE"),
        ("EU (TCAP)", "EU"),
        ("NL", "NL"),
    ],
)
def test_auctioneer(name, expected):
    assert eex_auctions.auctioneer(name) == expected


def test_auction_report_keeps_successful_auctions_by_product():
    frame = eex_auctions.parse_report((FIXTURES / "eex_auctions.xlsx").read_bytes(), NOW, "report-2026")
    assert frame.columns == COMMODITY.columns
    rows = sorted(zip(frame["trade_date"], frame["product"], frame["contract"], frame["price"]))
    assert rows == [
        (date(2026, 11, 4), "EUAA", "primary-auction-EU", 85.0),
        (date(2026, 11, 6), "EUA", "primary-auction-DE", 87.51),
        (date(2026, 11, 9), "EUA", "primary-auction-EU", 86.91),
    ]  # the cancelled PL auction is dropped
    assert set(frame["unit"]) == {"TCO2"}


# --- collectors in the daily run ------------------------------------------------

def _commodity(day: date, source: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "source": [source], "product": ["TTF"], "contract": ["monthly-average"],
            "trade_date": [day], "price": [30.0], "currency": ["USD"], "unit": ["MMBTU"],
            "source_reference": [""], "revision_number": [0], "retrieved_at_utc": [NOW],
        }
    ).pipe(lambda f: store.validate(f, COMMODITY))


def _collector(fetch, stale_after_days=10, **overrides):
    base = collectors.COLLECTORS["gas"]
    fields = {**base.__dict__, "fetch": fetch, "stale_after_days": stale_after_days, **overrides}
    return collectors.Collector(**fields)


def test_daily_collector_fails_once_its_data_is_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    today = date(2026, 11, 9)
    fresh = _collector(lambda s, e: _commodity(today - timedelta(days=3), "worldbank"))
    assert cli.run_collector(fresh, "daily", None, None, today) == 0
    stale = _collector(lambda s, e: empty_frame(COMMODITY), stale_after_days=2)
    assert cli.run_collector(stale, "daily", None, None, today) == 1


def test_daily_collector_outage_is_forgiven_while_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    today = date(2026, 11, 9)
    store.write_frame(_commodity(today - timedelta(days=1), "worldbank"), tmp_path, COMMODITY)

    def down(start, end):
        raise SourceUnavailable("503")

    assert cli.run_collector(_collector(down), "daily", None, None, today) == 0
    assert cli.run_collector(_collector(down), "backfill", None, None, today) == 1


def test_entsoe_collectors_are_skipped_without_a_token(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.delenv("ENTSOE_API_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env
    called = []
    collector = _collector(lambda s, e: called.append(1), needs_entsoe_token=True)
    assert cli.run_collector(collector, "daily", None, None, date(2026, 11, 9)) == 0
    assert not called


def test_backfill_is_cut_into_chunks(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return empty_frame(COMMODITY)

    collector = _collector(fetch, chunk_days=365, archive_start=date(2024, 1, 1))
    assert cli.run_collector(collector, "backfill", None, date(2026, 1, 1), date(2026, 1, 1)) == 0
    assert calls == [(date(2024, 1, 1), date(2024, 12, 31)), (date(2024, 12, 31), date(2025, 12, 31)),
                     (date(2025, 12, 31), date(2026, 1, 1))]
