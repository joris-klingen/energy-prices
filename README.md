# energy-prices

Archive of Dutch energy prices in partitioned Parquet, topped up twice a day.
`energy-prices coverage` prints what is in it.

| Dataset (`--datasets`) | What | Source | From | Resolution |
| --- | --- | --- | --- | --- |
| `day-ahead` | SDAC day-ahead electricity, NL and neighbours | ENTSO-E, Energy-Charts | 2015-01-05 | hourly; 15 min from 2025-10-01 |
| `imbalance` | NL imbalance settlement prices, long and short | ENTSO-E (A85) | 2015 | 15 min |
| `gas` | Dutch TTF natural gas, monthly average, USD/mmbtu | World Bank Pink Sheet | 2015-04 | monthly |
| `carbon` | EU ETS allowances (EUA, aviation EUAA), auction clearing price, EUR/tCO2 | EEX | 2012-01 | per auction, ~4 a week |

### Day-ahead zones

Hourly from 2015-01-05, quarter-hourly from the EU-wide 15-minute MTU change on
2025-10-01.

| Zone | Why it is here |
| --- | --- |
| `NL` | the subject |
| `DE-LU` | largest coupled neighbour; from 2018-10-01 |
| `DE-AT-LU` | the joint zone before the 2018 split; history only, backfill with `--zones DE-AT-LU` |
| `BE` | coupled neighbour |
| `NO2` | NorNed interconnector |
| `DK1` | COBRAcable interconnector |
| `FR` | the other large continental reference price |

GB (BritNed) is left out: it left SDAC with Brexit and ENTSO-E stopped publishing its
prices in 2021. Zones live in `config.ZONES`; adding one is a one-line change.

## Sources

| Source | Role | Access |
| --- | --- | --- |
| [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) | primary | free REST API, token by request |
| [Energy-Charts](https://api.energy-charts.info/) (Fraunhofer ISE) | fallback and cross-check | free, no token, CC BY 4.0 |

Under SDAC market coupling there is one clearing price per bidding zone per market time
unit, so both publish the same numbers — on the intervals the archive holds from both,
they agree exactly. ENTSO-E is primary because it is the origin of the prices rather
than a re-publisher — the Energy-Charts NL series arrives third-hand, its `license_info`
crediting *Bundesnetzagentur | SMARD.de* — and because it states the resolution instead
of leaving it to be inferred from the spacing between timestamps. Energy-Charts needs no
credential, so `--source energy-charts` is the route to collect without one.

Day-ahead prices need no slow accumulation — ENTSO-E is the official EU archive back to
2015, so `backfill` fetches the lot in one run.

### The other datasets, and what is deliberately missing

- **Imbalance** comes from ENTSO-E A85 for the Dutch control area. Every quarter-hour has
  a `long` and a `short` price (`price_type`); they differ only when TenneT activated
  balancing energy in both directions. TenneT publishes a day the next morning and revises
  until about mid next month, so the daily run re-fetches 45 days. TenneT's own API has
  more detail (regulation state, dispatch prices) but its terms forbid sharing the data,
  so it cannot feed a public archive.
- **Intraday prices are not collected.** ENTSO-E answers "no matching data" for the
  SIDC intraday auctions (IDA1-3) in every zone here, Energy-Charts has none, and the
  continuous indices (EPEX ID1/ID3/IDFull) are licensed — EPEX blocks scripts and sells
  the history. There is no free source to archive.
- **Gas** is monthly because there is no free daily TTF series that may be archived —
  ICE Endex and EEX license their settlements, and the free mirrors (Yahoo and the like)
  forbid redistribution. The World Bank's "Natural gas, Europe" column is TTF from April
  2015 (a border-price blend before that, so the archive starts there), in USD/mmbtu,
  published around the 3rd of each month. FRED's `PNGASEUUSDM` (IMF) tracks it to within
  a few cents and is a handy cross-check.
- **Carbon** is the primary-auction clearing price from EEX, the EU ETS common auction
  platform, not the secondary-market (futures) price, which is licensed. `contract` says
  who auctioned it (`primary-auction-EU`, `-DE`, `-PL`, `-NIR`, ...), `product` whether
  it was a general (`EUA`) or aviation (`EUAA`) allowance. Cancelled auctions are dropped.

## Use

```bash
pip install -e .

energy-prices backfill                    # 2015 → tomorrow, ~7 minutes
energy-prices daily                       # last 7 days + tomorrow
energy-prices coverage                    # rows and date range per series
energy-prices build-db                    # rebuild the DuckDB file
energy-prices check-token                 # verify the ENTSO-E token
```

Options: `--datasets` (default: all four), `--source {entsoe,energy-charts}` (day-ahead
only, default `entsoe`),
`--fallback {auto,none,<source>}`, `--start`, `--end` (exclusive), `--zones` (default:
every active zone), `--lookback`.

## Reading the data

```python
import polars as pl

prices = pl.read_parquet("data/day_ahead/*/*.parquet").filter(
    (pl.col("bidding_zone") == "NL") & (pl.col("source") == "energy-charts")
)
```

Each dataset lives in its own folder: `data/imbalance/zone=NL/YYYY-MM.parquet`,
and gas and carbon together in
`data/commodity/product=<TTF|EUA|EUAA>/YYYY.parquet`, one row per product, contract and
`trade_date`. `build-db` turns each into a table (`imbalance_prices`,
`commodity_prices`).

```r
library(duckdb); library(data.table)

prices <- setDT(dbGetQuery(dbConnect(duckdb()), "
  SELECT mtu_start_utc, resolution, price_eur_mwh
  FROM read_parquet('data/day_ahead/*/*.parquet', union_by_name = true)
  WHERE bidding_zone = 'NL' AND source = 'energy-charts'"))
```

## Layout

```
energy_prices/
  schema.py      # each dataset's row schema, dedupe key and partitioning, declared once
  collectors.py  # the non-day-ahead datasets: start, lookback, staleness allowance
  sources/       # entsoe.py, energycharts.py, worldbank.py, eex_auctions.py
  store.py       # polars writes the Parquet, duckdb builds the queryable database
  cli.py
data/day_ahead/zone=<ZONE>/YYYY-MM.parquet   # source of truth, one row per market time unit
data/energy_prices.duckdb                # derived, git-ignored, rebuilt on demand
```

One row per (source, bidding zone, delivery interval, resolution). Keeping `source` in the
key lets both providers cover the same interval side by side, which is what makes the
cross-check possible — filter on one `source` for a single clean series. Timestamps are
UTC; `build-db` adds `mtu_start_local` in Europe/Amsterdam. `resolution` is `PT60M` or
`PT15M`; Energy-Charts states none, so it is inferred from the spacing between timestamps.
A re-published revision replaces the earlier one, keeping the highest `revision_number`.

## Automation

`.github/workflows/collect.yml` runs at 12:17 and 16:47 UTC — deliberately off the hour,
since GitHub defers scheduled workflows under load and runs on the hour were arriving
around eight hours late. Each run fetches the last seven delivery days plus tomorrow and
**commits any changed Parquet directly to `main`**. It also takes manual `source`,
`fallback`, `start`, `end` and `zones` inputs for a backfill.

The other datasets follow the same rule with their own allowance (imbalance 4 days,
carbon 25 to ride out the Christmas pause, gas 100), and are checked
on every run, not only after an outage: a scraped page whose layout changed can keep
answering 200 with nothing new, and only the age of the newest row would show it.

An upstream outage is not treated as a failure: a connection error, 5xx or rate limiting
that outlasts the retries makes `daily` warn and fall back to the other source, and the
run still exits 0, because the seven-day lookback repairs the gap next time. A non-zero
exit is reserved for the archive genuinely falling behind — newest delivery day of any one
zone more than two days back, or the **primary alone** going quiet for more than three days while the
fallback keeps the archive fresh — otherwise a fallback doing its job would mask a
primary that has stopped working, and ENTSO-E answers a revoked token with the same
opaque 500 it returns during an outage. A bad request or rejected credential still fails
immediately. `backfill` has no such tolerance; an incomplete backfill is a failed one.

Energy-Charts answers a burst of large requests with `429` and a `Retry-After` of about
28 seconds, so the client pauses 10 seconds between backfill chunks — which is why a full
backfill takes about seven minutes rather than one.

## ENTSO-E token

Needed for the default source. Sign in at
[transparency.entsoe.eu](https://transparency.entsoe.eu/) → **My Account Settings** and
generate a *Web Api Security Token*. If there is no button, API access is not enabled yet:
email `transparency@entsoe.eu`, subject **Restful API access**, with the account address
in the body (about three working days).

Put it in `ENTSOE_API_TOKEN` — the environment or a git-ignored `.env` — and check it with
`energy-prices check-token`. CI reads it from the `ENTSOE_TOKEN` repository secret. Note
that ENTSO-E answers an invalid or not-yet-enabled token with an opaque HTTP 500, exactly
what a real outage returns, so `check-token` cannot tell you which it is.

## Licence and attribution

Energy-Charts data is CC BY 4.0, credited to *Bundesnetzagentur | SMARD.de* in every
response's `license_info` — but only for the zones SMARD republishes (all the active
zones here); other Energy-Charts zones are for private use only. ENTSO-E data is free to
use under its
[terms of use](https://transparency.entsoe.eu/content/static_content/Static%20content/terms%20and%20conditions/terms%20and%20conditions.html);
the TSO data among it, imbalance prices included, is open data. Credit the publisher you
pulled from and ENTSO-E as the origin.

The World Bank Pink Sheet is CC BY 4.0 (*World Bank Commodity Price Data*). The EEX
auction results are regulatory disclosures the auction platform must publish; they carry
no explicit open licence, so credit EEX and check its terms before redistributing them
beyond research use.

## Contributing

Open a pull request against `main`; the collector's own data commits are the only thing
that goes straight in.

```bash
pip install -e ".[dev]"
pytest
```
