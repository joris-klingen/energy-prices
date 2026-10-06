# energy-prices

Archive of Dutch day-ahead electricity prices. See README.md for what it collects and how
to run it; this file records how to work on it.

## Changes go through a pull request

Open a PR against `main` — do not push to `main` directly. The one exception is the
collector workflow itself, which commits new Parquet partitions straight to `main` on
every scheduled run; that is the job doing its work, not a code change.

## Shape of the code

- `schema.py` declares the row schema and the dedupe key **once**. Every source returns a
  frame through `validate()`, so adding a column means changing it there and nowhere else.
- Polars owns the data path — parse, validate, merge, write. DuckDB owns querying: the
  derived `.duckdb` file and ad-hoc SQL. Keep that split.
- A source module exposes `SOURCE`, `ARCHIVE_START`, `CHUNK_DAYS` and
  `fetch_day_ahead(...) -> pl.DataFrame`, and is registered in `sources/__init__.py`.
  The parse step is a pure function over a payload, which is what makes it testable.

## Upstream trouble is not a failure

Connection errors, 5xx and exhausted rate limits raise `SourceUnavailable`: `daily` warns,
falls back to the other source, and still exits 0, because the seven-day lookback repairs
the gap on the next run. A non-zero exit is reserved for the archive actually falling
behind — more than `STALE_AFTER_DAYS` behind today. A 4xx or a rejected credential is our
mistake and must keep failing loudly. Do not blur those cases.

## Touching stored data

The committed Parquet cost real time and upstream rate limit to fetch, so never re-download
it to prove a refactor is safe. Fingerprint it before and after instead, and require the two
to match:

```sql
SELECT count(*), sum(hash(source || bidding_zone || mtu_start_utc::VARCHAR
                          || resolution || price_eur_mwh::VARCHAR))
FROM read_parquet('data/day_ahead/*/*.parquet', union_by_name = true);
```

`retrieved_at_utc` is provenance and changes on every re-fetch — that is expected and the
fingerprint deliberately ignores it.

## Checks

```bash
pip install -e ".[dev]"
pytest
```
