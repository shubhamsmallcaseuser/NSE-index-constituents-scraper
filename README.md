# NSE Index Constituents Scraper

A knowledge-transfer document for this project. It explains what the scraper does, how it's built, how to run and maintain it, and what to watch out for.

## 1. Purpose

This script fetches the current constituent lists (stock + ISIN + company name) for four NSE indices directly from the NSE website, and:

1. Saves each index to its own CSV file.
2. Saves all four indices together into one Excel workbook (one sheet per index).
3. Writes a monthly snapshot into the `windmill` Postgres database (table `index_constituents_monthly`), so we have a historical record of index membership over time (used for large/mid/small/micro cap classification of stocks).

Indices covered:

| Index | Bucket | Expected count |
|---|---|---|
| NIFTY 100 | Large Cap | 100 |
| NIFTY MIDCAP 150 | Mid Cap | 150 |
| NIFTY SMALLCAP 250 | Small Cap | 250 |
| NIFTY MICROCAP 250 | Micro Cap | 250 |

## 2. Why it exists / outcome

NSE does not offer a stable public API for index constituents (the official CSV download links change/rotate index codes over time — that's why some indices have multiple "aliases" configured, see `INDICES` in the script). Before this scraper, index membership had to be updated manually. This script automates that and keeps a dated history in Postgres so we can answer "what was in NIFTY SMALLCAP 250 as of March 2026?" without redoing manual work.

The database write is idempotent per calendar month: re-running it in the same month deletes and re-inserts that month's rows for each index, so it's safe to re-run if a run fails partway.

## 3. How it works (architecture)

All logic lives in `nse_index_constituents.py`. High level flow (see `_run()`):

1. **Connect to DB** (`connect_db` / `init_db`) — reads credentials from `db_config.yaml`, creates the `index_constituents_monthly` table if it doesn't exist, and picks up an optional Slack webhook URL from the same config.
2. **Establish an NSE session** (`build_session`) — NSE blocks plain `requests`/`urllib` at the TLS handshake level, so this uses [`curl_cffi`](https://github.com/lexiforest/curl_cffi) to impersonate a real Chrome TLS fingerprint. It "warms up" by visiting the homepage and a market-data page first (to seed cookies), like a browser would.
3. **Load the ISIN map** (`load_isin_map`) — NSE's index API doesn't reliably return ISINs for every stock, so ISINs are instead sourced from our own `symbology_changes` table in Postgres (latest `new_ticker → new_isin` mapping per symbol) and merged in after fetching.
4. **Fetch each index** (`fetch_index`) — hits NSE's internal `marketWatchApi` endpoint per index. Some indices have had more than one internal symbol code over time, so the script tries a list of aliases in order until one returns data.
5. **Clean the data**:
   - Drops the aggregate/index-level row NSE includes in the response.
   - Deduplicates symbols that NSE occasionally emits twice under a renamed company (see "Known issues" below).
   - Merges in ISINs from the symbology map; rows with no ISIN match are logged and flagged.
6. **Write outputs** — CSV per index, one combined Excel workbook, and (unless `--dry-run`) rows into Postgres.
7. **Slack notifications** — if a webhook URL is configured, posts start/success/warning/error messages, including per-index anomalies (unexpected counts, missing ISINs, dropped duplicates).
8. **Summary + exit** — logs a per-index and total count summary; a non-fatal warning is logged/Slacked if the total doesn't match the expected 750.

### Error handling philosophy

Any unhandled exception anywhere in `_run()` is caught in `main()`, logged with a full traceback, reported to Slack with the specific step it failed on (tracked via the module-level `_current_step` variable), and the process exits with code 1. This means a failure always shows up in Slack with enough context to know which stage broke, without needing to dig through logs first — though the log file has the full traceback if needed.

## 4. Project layout

```
db_config.yaml                     # DB connection + Slack webhook config (contains secrets — see §7)
nse_index_constituents.py          # the entire scraper
requirements.txt                   # pinned dependencies
ind_nifty100list.csv               # output: NIFTY 100 constituents
ind_niftymidcap150list.csv         # output: NIFTY MIDCAP 150 constituents
ind_niftysmallcap250list.csv       # output: NIFTY SMALLCAP 250 constituents
ind_niftymicrocap250list.csv       # output: NIFTY MICROCAP 250 constituents
nse_all_constituents.xlsx          # output: all four indices, one workbook
nse_scraper.log                    # rotating log (5 MB x 3 backups), overwritten/rotated on each run
.venv/                             # local Python virtual environment (not portable — recreate, don't copy)
```

## 5. Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`db_config.yaml` must exist alongside the script with this shape:

```yaml
wm_price_db:
  user: <postgres user>
  password: <postgres password>
  host: <postgres host>
  port: 5432
  dbname: windmill
  #slack_webhook_url: https://hooks.slack.com/...   # optional, uncomment to enable Slack alerts
```

## 6. Running it

```
python nse_index_constituents.py             # full run: CSV + Excel + DB writes
python nse_index_constituents.py --dry-run    # CSV + Excel only, no DB writes (safe to test with)
```

Recommended cadence: **monthly**, since the DB table stores one snapshot per index per calendar month (re-running mid-month overwrites that month's snapshot, it doesn't create a duplicate). There is currently no scheduler wired up in this project directory — if this needs to run automatically, it should be hooked into whatever job scheduler/cron/Windmill flow the team uses, pointed at this script with the venv's Python.

## 7. Credentials and secrets — read before sharing this repo

`db_config.yaml` contains a **live Postgres password and, if uncommented, a Slack webhook URL** in plaintext. This file:

- Should **not** be committed to any shared/public git repo as-is.
- Should be handed off securely (not pasted into chat/tickets) as part of offboarding, and the receiving engineer should consider whether the password should be rotated after handoff, per normal offboarding practice.

## 8. Known issues / quirks

- **NSE MICROCAP 250 total sometimes ≠ 250.** In the last recorded dry run, NSE's MICROCAP 250 API returned two rows for the same underlying company (`TARC LIMITED` listed as both `TARC` and under a legacy name `ANANT RAJ GLOBAL LIMITED`) — `_dedup_symbols()` keeps the first and drops the rest, which brought that index down to 249 and the grand total to 749 against the expected 750. This is a data quirk on NSE's side (a renamed/merged entity still appearing under both identities), not a bug — the expected-count check exists specifically to surface issues like this so they're visible in Slack/logs rather than silently ingested wrong. If this keeps recurring for the same symbol, it may be worth confirming with NSE data whether it should be permanently excluded rather than logged each run.
- **Missing ISINs.** Any symbol not present (yet) in `symbology_changes` will have a null ISIN. Those rows are logged/Slacked as warnings and, for DB writes, dropped entirely (see `no_isin` handling in `_run()`) since `isin` is part of the primary key. The fix is to add the missing symbol to `symbology_changes`, not to change this script.
- **NSE index alias drift.** NSE has changed the internal symbol code for at least SMALLCAP 250 and MICROCAP 250 in the past (see the multiple entries per index in `INDICES`). If a run suddenly logs "All aliases exhausted" for an index, NSE has likely changed the code again — check the NSE website's index page network requests for the current `symbol=` value and add it as a new alias.
- **TLS impersonation is brittle by design.** `curl_cffi`'s `impersonate="chrome120"` mimics a specific Chrome version's TLS fingerprint. If NSE starts blocking that specific fingerprint, or `curl_cffi` drops support for it in a future version, session establishment will start failing with non-200s from the homepage/market-data warm-up requests. Check the [curl_cffi releases](https://github.com/lexiforest/curl_cffi/releases) for newer supported impersonation targets if this happens.

## 9. Contacts / escalation

Slack alerts (when the webhook is configured) mention `@srivatsa.rao` and `@shubham.shreshtha` — update `_SLACK_MENTIONS` in the script once ownership transfers, so alerts reach the right people.
