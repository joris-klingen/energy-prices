# energy-prices

Archive of Dutch (NL bidding zone) day-ahead electricity prices in month-partitioned
Parquet, topped up twice a day. Hourly from 2015-01-05, quarter-hourly from the EU-wide
15-minute MTU change on 2025-10-01. `energy-prices coverage` prints what is in it.

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
2015, so `backfill` fetches the lot in one run. Intraday *continuous* prices (EPEX
ID1/ID3/IDFull) are the opposite case: only the last few days are public and the history
is sold, so those can only be harvested from the day you start.

## Use

```bash
pip install -e .

energy-prices backfill                    # 2015 → tomorrow, ~7 minutes
energy-prices daily                       # last 7 days + tomorrow
energy-prices coverage                    # rows and date range per series
energy-prices build-db                    # rebuild the DuckDB file
energy-prices check-token                 # verify the ENTSO-E token
```

Options: `--source {entsoe,energy-charts}` (default `entsoe`),
`--fallback {auto,none,<source>}`, `--start`, `--end` (exclusive), `--zones`,
`--lookback`.

## Reading the data

```python
import polars as pl

prices = pl.read_parquet("data/day_ahead/*/*.parquet").filter(
    (pl.col("bidding_zone") == "NL") & (pl.col("source") == "energy-charts")
)
```

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
  schema.py      # the row schema and dedupe key, declared once
  sources/       # energycharts.py (default), entsoe.py; each returns a pl.DataFrame
  store.py       # polars writes the Parquet, duckdb builds the queryable database
  cli.py
data/day_ahead/zone=NL/YYYY-MM.parquet   # source of truth, one row per market time unit
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
`fallback`, `start` and `end` inputs for a backfill.

An upstream outage is not treated as a failure: a connection error, 5xx or rate limiting
that outlasts the retries makes `daily` warn and fall back to the other source, and the
run still exits 0, because the seven-day lookback repairs the gap next time. A non-zero
exit is reserved for the archive genuinely falling behind — newest delivery day more than
two days back, or the **primary alone** going quiet for more than three days while the
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
response's `license_info`. ENTSO-E data is free to use under its
[terms of use](https://transparency.entsoe.eu/content/static_content/Static%20content/terms%20and%20conditions/terms%20and%20conditions.html).
Credit the publisher you pulled from and ENTSO-E as the origin.

## Contributing

Open a pull request against `main`; the collector's own data commits are the only thing
that goes straight in.

```bash
pip install -e ".[dev]"
pytest
```
