# energy-prices

Archive of Dutch (NL bidding zone) day-ahead electricity prices, collected from the
ENTSO-E Transparency Platform into month-partitioned Parquet files.

## Is there an official archive? Yes.

For day-ahead prices there is no need to slowly accumulate data: the **ENTSO-E
Transparency Platform** is the official EU archive and it is free.

| Source | Covers | History | Access |
| --- | --- | --- | --- |
| [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) | Day-ahead prices (12.1.D) for every EU bidding zone, hourly to 2025-09-30, 15-minute from 2025-10-01 (EU-wide MTU change) | 2015 → | Free REST API (token by email) + bulk CSV extracts in the File Library (which replaced the SFTP server in September 2025) |
| [SMARD](https://www.smard.de/en/downloadcenter/download-market-data) (Bundesnetzagentur) | DE/LU and AT, re-published from ENTSO-E | 2015 → | Free, no token; useful cross-check |
| [Energy-Charts](https://api.energy-charts.info/) (Fraunhofer ISE) | Day-ahead prices for 40+ zones, CC BY 4.0 | 2015 → | Free API, no token; convenient fallback |
| [Nord Pool Data Portal](https://data.nordpoolgroup.com/) | Nordic/Baltic + NL day-ahead | 1992 → | Free viewing; redistribution licensed |

So this repo **backfills the full history in one run** and then tops up daily.

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
data/day_ahead/zone=NL/YYYY-MM.parquet   # source of truth, one row per market time unit
data/energy_prices.duckdb                # derived, git-ignored, rebuilt on demand
```

One row per (bidding zone, delivery interval, resolution). Both the hourly and the
15-minute series are stored where ENTSO-E publishes both; `resolution` distinguishes them.
Timestamps are UTC; `build-db` adds `mtu_start_local` in Europe/Amsterdam. Re-published
revisions replace earlier ones on `(bidding_zone, mtu_start_utc, resolution)`, keeping the
highest `revision_number`.

## Use

```bash
pip install -r requirements.txt
export ENTSOE_API_TOKEN=...        # see below

python -m energy_prices check-token                   # verify the token works
python -m energy_prices backfill                      # 2015-01-01 → tomorrow
python -m energy_prices backfill --start 2024-01-01   # a narrower window
python -m energy_prices daily                         # last 7 days + tomorrow
python -m energy_prices build-db                      # rebuild the DuckDB file
python -m energy_prices coverage                      # rows and date range per series
```

## Getting an API token

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
   python -m energy_prices check-token
   ```

5. For the scheduled workflow, add the same value as the repository secret
   `ENTSOE_API_TOKEN` (*Settings → Secrets and variables → Actions → New repository
   secret*), or from a machine with the GitHub CLI:

   ```bash
   gh secret set ENTSOE_API_TOKEN --repo joris-klingen/energy-prices
   ```

The token goes out as the `securityToken` query parameter on every request to
`https://web-api.tp.entsoe.eu/api`. Treat it as a password: it is tied to the account,
and the `.env` file is git-ignored for that reason.

## Reading the data

```r
library(duckdb)
library(data.table)

con <- dbConnect(duckdb())
prices <- setDT(dbGetQuery(con, "
  SELECT mtu_start_utc, resolution, price_eur_mwh
  FROM read_parquet('data/day_ahead/*/*.parquet', union_by_name = true)
  WHERE bidding_zone = 'NL'
"))
```

## Automation

`.github/workflows/collect.yml` runs at 12:00 and 16:00 UTC, fetches the last seven
delivery days plus tomorrow, and commits any changed Parquet partitions. Run it with
`start`/`end` inputs for a manual backfill. Two runs per day give a free retry, and the
seven-day lookback means a few missed days repair themselves.

## Licence and attribution

ENTSO-E Transparency Platform data is free to use under its
[terms of use](https://transparency.entsoe.eu/content/static_content/Static%20content/terms%20and%20conditions/terms%20and%20conditions.html);
cite ENTSO-E as the source in any publication.
