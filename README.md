# energy-prices

Archive of Dutch (NL bidding zone) day-ahead electricity prices in month-partitioned
Parquet files. The primary source is the **Energy-Charts API** (Fraunhofer ISE, CC BY 4.0,
no token); the **ENTSO-E Transparency Platform** is available as a second source for
cross-checking and as a fallback.

Current coverage: **2015-01-05 onwards**, 94,127 hourly points to the EU-wide MTU change
on 2025-10-01 and 35,232 quarter-hourly points after it.

## Is there an official archive? Yes.

For day-ahead prices there is no need to slowly accumulate data: the **ENTSO-E
Transparency Platform** is the official EU archive and it is free.

| Source | Covers | History | Access |
| --- | --- | --- | --- |
| [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) | Day-ahead prices (12.1.D) for every EU bidding zone, hourly to 2025-09-30, 15-minute from 2025-10-01 (EU-wide MTU change) | 2015 → | Free REST API (token by email) + bulk CSV extracts in the File Library (which replaced the SFTP server in September 2025) |
| [SMARD](https://www.smard.de/en/downloadcenter/download-market-data) (Bundesnetzagentur) | DE/LU and AT, re-published from ENTSO-E | 2015 → | Free, no token; useful cross-check |
| [Energy-Charts](https://api.energy-charts.info/) (Fraunhofer ISE) | Day-ahead prices for 40+ zones, CC BY 4.0 | 2015 → | Free API, no token; convenient fallback |
| [Nord Pool Data Portal](https://data.nordpoolgroup.com/) | Nordic/Baltic + NL day-ahead | 1992 → | Free viewing; redistribution licensed |

Both carry the same numbers: under SDAC market coupling there is a single clearing price
per bidding zone per MTU, and every publisher reports it. Energy-Charts is primary here
because it needs no token and returns years per request; note that its NL series is
re-published (its `license_info` credits *Bundesnetzagentur | SMARD.de*), so cite ENTSO-E
as the origin in anything you publish.

Either way the repo **backfills the full history in one run** and then tops up daily.

### What does *not* have a free archive

If the scope is later widened to **intraday continuous** trading (EPEX SPOT ID1/ID3/IDFull
indices and trades), the picture is different: EPEX shows only the last few days publicly
and sells the history through the
[EEX Group webshop](https://webshop.eex-group.com/epex-spot-public-market-data);
[Nord Pool intraday](https://www.nordpoolgroup.com/en/services/power-market-data-services/intraday-market-data/)
redistribution licences start at €3,000–7,000/year. *That* is the case where a daily
harvester genuinely builds something you cannot buy back later, and it is worth starting
early even if the analysis is years away. The intraday **auctions** (IDA1/2/3, 15-minute,
pan-European since June 2024) are on ENTSO-E and can be added here by querying document
type A44 with `contract_MarketAgreement.type=A07`.

## Layout

```
energy_prices/
  schema.py        # the declared row schema and the dedupe key, in one place
  sources/         # energycharts.py (default), entsoe.py; each returns a pl.DataFrame
  store.py         # polars writes the Parquet, duckdb builds the queryable database
  cli.py
data/day_ahead/zone=NL/YYYY-MM.parquet   # source of truth, one row per market time unit
data/energy_prices.duckdb                # derived, git-ignored, rebuilt on demand
```

Polars owns the data path — parse, validate, merge, write — and DuckDB owns querying.
Every source returns a frame validated against `schema.SCHEMA`, so a source cannot invent
a column or change a dtype without the write failing.

One row per (source, bidding zone, delivery interval, resolution). Keeping `source` in the
key lets both providers cover the same interval side by side, which is what makes a
cross-check possible — filter on `source = 'energy-charts'` for a single clean series.
`resolution` is `PT60M` or `PT15M`; Energy-Charts states no resolution, so it is inferred
from the spacing between timestamps.
Timestamps are UTC; `build-db` adds `mtu_start_local` in Europe/Amsterdam. Re-published
revisions replace earlier ones on `(source, bidding_zone, mtu_start_utc, resolution)`,
keeping the highest `revision_number`.

## Use

```bash
pip install -e .

energy-prices backfill                      # 2015 → tomorrow, ~7 minutes
energy-prices backfill --start 2024-01-01   # a narrower window
energy-prices daily                         # last 7 days + tomorrow
energy-prices build-db                      # rebuild the DuckDB file
energy-prices coverage                      # rows and date range per series

energy-prices daily --source entsoe         # the second source
energy-prices check-token                   # verify the ENTSO-E token
```

## Getting an ENTSO-E API token (optional)

Only needed for `--source entsoe`; the default source needs nothing.

1. Sign in at [transparency.entsoe.eu](https://transparency.entsoe.eu/) and open
   **My Account Settings**.
2. If a button to generate a *Web Api Security Token* is there, click it — that is the
   token. If it is not, API access has not been enabled for the account yet: email
   `transparency@entsoe.eu` with subject **Restful API access** and the account's email
   address in the body. The helpdesk aims to answer within three working days, after
   which the button appears.
3. Put the token where the code can find it, either way:

   ```bash
   echo 'ENTSOE_API_TOKEN=<token>' >> .env   # git-ignored, for local runs
   export ENTSOE_API_TOKEN=<token>           # or just the environment
   ```

4. Verify it, which costs one small request:

   ```bash
   energy-prices check-token
   ```

5. For the scheduled workflow, add the same value as the repository secret
   `ENTSOE_TOKEN` (*Settings → Secrets and variables → Actions → New repository
   secret*), or from a machine with the GitHub CLI:

   ```bash
   gh secret set ENTSOE_TOKEN --repo joris-klingen/energy-prices
   ```

The token goes out as the `securityToken` query parameter on every request to
`https://web-api.tp.entsoe.eu/api`. Treat it as a password: it is tied to the account,
and the `.env` file is git-ignored for that reason.

Be aware that ENTSO-E answers an invalid or not-yet-enabled token with an opaque
HTTP 500 — the same response a genuine outage gives — so `check-token` reports both
possibilities rather than pretending to know which it is.

## Reading the data

```r
library(duckdb)
library(data.table)

con <- dbConnect(duckdb())
prices <- setDT(dbGetQuery(con, "
  SELECT mtu_start_utc, resolution, price_eur_mwh
  FROM read_parquet('data/day_ahead/*/*.parquet', union_by_name = true)
  WHERE bidding_zone = 'NL' AND source = 'energy-charts'
"))
```

## Automation

`.github/workflows/collect.yml` runs at 12:17 and 16:47 UTC, fetches the last seven
delivery days plus tomorrow, and commits any changed Parquet partitions. No secret is
needed for the default source. Run it by hand with `start`/`end` inputs for a backfill. Two runs per day give a free retry, and the
seven-day lookback means a few missed days repair themselves. The odd minutes are
deliberate: GitHub defers scheduled workflows under load, and runs on the hour were
arriving around eight hours late.

## Licence and attribution

Energy-Charts data is **CC BY 4.0** and the NL series credits *Bundesnetzagentur |
SMARD.de*; the API returns that string in every response as `license_info`. ENTSO-E
Transparency Platform data is free to use under its
[terms of use](https://transparency.entsoe.eu/content/static_content/Static%20content/terms%20and%20conditions/terms%20and%20conditions.html).
Credit both the publisher you pulled from and ENTSO-E as the origin of the prices.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The parsers are pure functions over a payload, so the suites cover the awkward cases
directly: mixed hourly/quarter-hourly spacing across the MTU change, a trailing point with
no successor, holes in the series, revision precedence, and month-boundary partitioning.

## When a source is down

Upstream outages happen — Energy-Charts was unreachable for over a day in October 2026 —
and a daily collector should not cry wolf over them. So the two cases are kept apart:

- **The source is unavailable** (connection failure, 5xx, or rate limiting that outlasts
  the retries) — logged as a warning, and `daily` falls back to the other source. If that
  does not work either the run still exits 0, because the seven-day lookback repairs the
  gap by itself on the next successful run.
- **The archive has actually fallen behind** — the newest stored delivery day is more than
  two days behind today — the run exits non-zero. A healthy archive reaches *tomorrow*, so
  this leaves roughly three days for an outage to clear before anyone is told.

A failing run therefore means the data is genuinely going stale, not that Fraunhofer had a
bad afternoon. A bad request or a rejected credential is nobody's outage and still fails
immediately. Fallback is chosen with `--fallback` (`auto`, `none`, or a source name);
`backfill` has no such tolerance, since an incomplete backfill is a failed backfill.

## Rate limits

Energy-Charts answers a burst of large requests with `429` and a `Retry-After` header
(about 28 seconds). The client honours it and pauses 10 seconds between backfill chunks,
which is why a full backfill takes around seven minutes rather than one.
