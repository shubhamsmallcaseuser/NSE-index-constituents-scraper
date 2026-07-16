# NSE Index Constituents Scraper

This repository contains a Python job that downloads NSE index constituents, enriches them with ISINs, produces CSV and Excel files, and stores a monthly history in PostgreSQL.

This README is also the project handover (KT) document. A new owner should be able to use it to set up the project, run it safely, validate the result, and investigate the most common failures.

## Project purpose and business outcome

The project replaces the manual collection of index membership data for four NSE equity indices:

| NSE index | Internal bucket | Expected constituents |
|---|---|---:|
| NIFTY 100 | Large Cap | 100 |
| NIFTY MIDCAP 150 | Mid Cap | 150 |
| NIFTY SMALLCAP 250 | Small Cap | 250 |
| NIFTY MICROCAP 250 | Micro Cap | 250 |
| **Expected total** |  | **750** |

Each run has three outcomes:

1. A current CSV file is created for each index.
2. A combined Excel workbook is created with one sheet per index.
3. In a full run, a dated monthly snapshot is written to PostgreSQL for historical market-cap classification and point-in-time analysis.

For example, the database history can answer: “Which securities belonged to NIFTY SMALLCAP 250 in March 2026?”

## Current status

The production copy is deployed on the RDP host `EC2AMAZ-Q9NK0NG` through a Google Drive-mounted folder. Windows Task Scheduler runs it on the **first calendar day of every month**.

The latest retained production validation was a full run on **3 July 2026**. It completed successfully, updated PostgreSQL, and produced:

| Index | Rows produced | ISINs missing | Result |
|---|---:|---:|---|
| NIFTY 100 | 100 | 0 | Expected count |
| NIFTY MIDCAP 150 | 150 | 0 | Expected count |
| NIFTY SMALLCAP 250 | 250 | 0 | Expected count |
| NIFTY MICROCAP 250 | 250 | 0 | Expected count after one duplicate NSE row was removed |
| **Total** | **750** | **0** | Expected total |

The production CSVs were rechecked on 16 July 2026 and contained no duplicate symbols. The deployed Python script and `requirements.txt` were byte-for-byte identical to the Git repository at that time. Treat these figures as evidence from that run, not as permanent expected market data.

## How the job works

All application logic is in `nse_index_constituents.py`. The execution flow is:

1. Read database and optional Slack configuration from `db_config.yaml`.
2. Connect to PostgreSQL. A full run also creates or migrates the destination table.
3. Establish an NSE web session using `curl_cffi` with a Chrome TLS fingerprint and warm-up requests.
4. Read the latest ticker-to-ISIN mappings from `symbology_changes`.
5. Request each index from NSE's internal `marketWatchApi`, trying configured symbol aliases in order.
6. Remove NSE's index-level aggregate row and deduplicate repeated symbols.
7. Replace the API's ISIN values with the mappings from PostgreSQL.
8. Write the per-index CSV files and combined Excel workbook.
9. In full mode, replace that index's existing snapshot for the current calendar month.
10. Write a summary to the console and rotating log, and send Slack notifications when configured.

### Important implementation choices

- **ISIN source:** `symbology_changes` in PostgreSQL is the authoritative ISIN source for this job. NSE response ISINs are deliberately discarded before the database mapping is merged.
- **NSE access:** standard HTTP clients are blocked or unreliable for this endpoint. `curl_cffi` is used to impersonate a browser TLS fingerprint.
- **Alias fallback:** `INDICES` contains one or more NSE API symbols per index. This protects the job from some NSE naming changes.
- **Monthly replacement:** each index is written in its own database transaction. Existing rows for the same index and calendar month are deleted and replaced atomically.
- **Missing ISINs:** affected rows remain visible in CSV/Excel but are omitted from the database because `isin` is part of the primary key.

## Repository layout

| Path | Purpose | Version controlled? |
|---|---|---|
| `nse_index_constituents.py` | Scraper, transformation, database write, logging, and Slack alerts | Yes |
| `requirements.txt` | Pinned Python dependencies | Yes |
| `ind_nifty100list.csv` | Latest NIFTY 100 output | Yes |
| `ind_niftymidcap150list.csv` | Latest NIFTY MIDCAP 150 output | Yes |
| `ind_niftysmallcap250list.csv` | Latest NIFTY SMALLCAP 250 output | Yes |
| `ind_niftymicrocap250list.csv` | Latest NIFTY MICROCAP 250 output | Yes |
| `nse_all_constituents.xlsx` | Latest combined output, one sheet per index | Yes |
| `db_config.yaml` | Database credentials and optional Slack webhook | **No — ignored; contains secrets** |
| `nse_scraper.log` | Rotating execution log, 5 MB with three backups | No — ignored |
| `.venv/` | Local Python virtual environment | No — ignored |
| `nse_index_scraper_job.bat` | Production RDP launcher stored in the Google Drive copy | No — not currently in this Git repository |

## Prerequisites

- Windows or another environment capable of running Python
- Python 3.12 recommended (the retained virtual environment uses Python 3.12.10)
- For production: access to RDP host `EC2AMAZ-Q9NK0NG`
- Google Drive for desktop running on the RDP host with the production folder mounted as `G:`
- Access to the RDP Anaconda environment `C:\Users\wm-eikon-user\anaconda3\envs\aa_rebal`
- Network access to `www.nseindia.com`
- Network access and read permission to the `windmill` PostgreSQL database
- Read permission on `symbology_changes`
- For a full run, create/alter/write permission on `index_constituents_monthly`
- Optional: a Slack incoming-webhook URL for run notifications

The project is currently written with Windows commands and paths in mind, but the Python code is otherwise portable.

## Initial setup

Run these commands from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create `db_config.yaml` beside the Python script:

```yaml
wm_price_db:
  user: <postgres-user>
  password: <postgres-password>
  host: <postgres-host>
  port: 5432
  dbname: windmill
  # slack_webhook_url: https://hooks.slack.com/services/...
```

Do not commit this file. The repository's `.gitignore` already excludes YAML files, but check `git status` before every commit.

## Production deployment: RDP, Google Drive, and Task Scheduler

The Git repository is the source of truth for the code and documentation. The scheduled runtime is a separate Google Drive copy on the RDP server.

| Setting | Production value |
|---|---|
| RDP host | `EC2AMAZ-Q9NK0NG` |
| Schedule | Monthly, on the first calendar day of every month |
| Scheduler | Windows Task Scheduler |
| Launcher | `nse_index_scraper_job.bat` |
| Python executable | `C:\Users\wm-eikon-user\anaconda3\envs\aa_rebal\python.exe` |
| Execution mode | Full run: files plus PostgreSQL update |
| Production log | `nse_scraper.log` in the Google Drive project folder |

Production project directory:

```text
G:\.shortcut-targets-by-id\0B7f_UMMZHM_JNXljMVVXY2VPcHM\Investment Research_N\Shubham_Shreshtha\scrapper codes
```

The batch launcher contains:

```bat
@echo off
setlocal

set PROJECT_DIR="G:\.shortcut-targets-by-id\0B7f_UMMZHM_JNXljMVVXY2VPcHM\Investment Research_N\Shubham_Shreshtha\scrapper codes"
set PYTHON_EXE="C:\Users\wm-eikon-user\anaconda3\envs\aa_rebal\python.exe"
set SCRIPT="nse_index_constituents.py"

cd /d "%PROJECT_DIR%" || exit /b %ERRORLEVEL%
"%PYTHON_EXE%" "%SCRIPT%"
```

The launcher changes to the Drive directory before starting Python. This is required because the scraper reads `db_config.yaml` and writes logs and outputs using relative paths. It does not pass `--dry-run`, so every scheduled execution is a full database run.

### Important environment warning

The `.venv` present in the Google Drive folder is a copied developer environment. Its metadata points back to `D:\scrapper codes\.venv` and it is **not** the environment used by Task Scheduler. Production deliberately uses the RDP-owned `aa_rebal` Anaconda environment from the batch file. Do not change the scheduled action to use the copied `.venv`.

### Task Scheduler configuration

The scheduled task must:

- trigger monthly on day `1`;
- run under an RDP account that can access the `G:` Google Drive mount, database, and NSE website;
- start `nse_index_scraper_job.bat` or call it through `cmd.exe`;
- allow enough runtime for network and database operations;
- retain task history; and
- be monitored through Task Scheduler history, `nse_scraper.log`, Slack, and database validation.

The Task Scheduler task name, exact start time, run-as account, and credential owner are not stored in this repository or the Drive folder. The successor must record these values during handover by opening Task Scheduler on the RDP host.

### Updating the production copy

After a code or dependency change is merged into GitHub:

1. Copy the approved `nse_index_constituents.py` and `requirements.txt` into the Google Drive production directory.
2. Do **not** overwrite `db_config.yaml`; it contains production secrets.
3. Do not copy a local `.venv` to production.
4. If dependencies changed, install them into the interpreter used by the batch file:

   ```powershell
   C:\Users\wm-eikon-user\anaconda3\envs\aa_rebal\python.exe -m pip install -r requirements.txt
   ```

5. From the RDP host and production directory, perform a dry run with the same interpreter:

   ```powershell
   C:\Users\wm-eikon-user\anaconda3\envs\aa_rebal\python.exe nse_index_constituents.py --dry-run
   ```

6. Compare the deployed script and requirements with the approved Git version, review the log, and validate all output counts.
7. Use Task Scheduler's **Run** action for an attended full-run test only when a database update is intended.

## Running the scraper

Always run the script from the repository root. Output and configuration paths are relative to the current working directory.

### Safe validation run

```powershell
.\.venv\Scripts\python.exe nse_index_constituents.py --dry-run
```

This writes CSV and Excel output but does not create, alter, delete, or insert rows in the destination table.

> **Important:** dry-run mode still requires `db_config.yaml`, a working database connection, and read access to `symbology_changes`, because the job uses that table to enrich ISINs.

### Full monthly run

```powershell
.\.venv\Scripts\python.exe nse_index_constituents.py
```

This writes files and updates `index_constituents_monthly`. In production, Windows Task Scheduler performs this command through `nse_index_scraper_job.bat` on the first calendar day of every month.

The scheduler definition itself is not exported into this Git repository. Any change to its trigger, action, account, or retry settings must be documented here and in the handover record.

## Monthly operating runbook

### 1. Before the run

- Before the first day of the month, confirm the RDP host is available and Google Drive for desktop has mounted the production directory as `G:`.
- Check that the Windows Task Scheduler task is enabled and its next-run date is the first calendar day of the coming month.
- Confirm database and NSE network access from the execution host.
- Confirm `db_config.yaml` is present and has the correct database target.
- Check that the Slack webhook and `_SLACK_MENTIONS` still point to the owning team.
- Run `C:\Users\wm-eikon-user\anaconda3\envs\aa_rebal\python.exe -m pip check` if dependencies or the host recently changed.

### 2. Execute a dry run

Routine monthly production execution is automatic. Use this dry-run command manually on the RDP host after deployment or when investigating a problem:

```powershell
C:\Users\wm-eikon-user\anaconda3\envs\aa_rebal\python.exe nse_index_constituents.py --dry-run
```

Review the final console summary and `nse_scraper.log`. Investigate:

- an index with no data;
- a count that differs from its expected value;
- missing ISINs;
- duplicate symbols;
- HTTP/TLS errors; or
- an unexpected total.

### 3. Validate generated files

Each CSV and Excel sheet should contain exactly these columns:

```text
Company Name,Symbol,ISIN Code
```

Check that all four CSVs were updated, the workbook has four sheets, symbols are not duplicated, and ISINs are populated. Do not accept a count merely because it equals the expected value; review any warnings first.

### 4. Execute the full run

For the normal monthly cycle, allow Task Scheduler to launch `nse_index_scraper_job.bat`. For an attended recovery or deployment test, use Task Scheduler's **Run** action or run:

```powershell
& "G:\.shortcut-targets-by-id\0B7f_UMMZHM_JNXljMVVXY2VPcHM\Investment Research_N\Shubham_Shreshtha\scrapper codes\nse_index_scraper_job.bat"
```

On the first day of the month, verify that Task Scheduler reports completion and that `nse_scraper.log` contains a new `FULL RUN` entry from `EC2AMAZ-Q9NK0NG`. Do not rely on Task Scheduler status alone; continue with the file and database checks below.

### 5. Validate PostgreSQL

Use the following query to check the most recent stored snapshot:

```sql
SELECT
    snapshot_date,
    index_name,
    market_cap_bucket,
    COUNT(*) AS constituent_count
FROM index_constituents_monthly
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM index_constituents_monthly)
GROUP BY snapshot_date, index_name, market_cap_bucket
ORDER BY index_name;
```

Check for duplicate symbols and unexpected missing data:

```sql
SELECT snapshot_date, index_name, symbol, COUNT(*) AS row_count
FROM index_constituents_monthly
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM index_constituents_monthly)
GROUP BY snapshot_date, index_name, symbol
HAVING COUNT(*) > 1;
```

Finally, confirm the Slack completion message and archive or commit the refreshed CSV/XLSX outputs according to the team's release process.

## Database contract

The full run ensures this destination table exists:

| Column | Type | Meaning |
|---|---|---|
| `snapshot_date` | `DATE` | Date on which the job ran |
| `index_name` | `VARCHAR(50)` | NSE index name |
| `symbol` | `VARCHAR(50)` | NSE trading symbol |
| `isin` | `VARCHAR(15)` | ISIN from `symbology_changes` |
| `company_name` | `VARCHAR(255)` | Company name from NSE |
| `market_cap_bucket` | `VARCHAR(20)` | Large, Mid, Small, or Micro Cap |
| `inserted_at` | `TIMESTAMP` | Database insertion timestamp |

The primary key is `(snapshot_date, index_name, isin)`. Supporting indexes cover `(snapshot_date, index_name)` and `isin`.

For each successfully fetched index, a rerun in the same calendar month deletes all of that index's rows for the month and inserts the latest result. This makes normal same-month reruns repeatable while retaining only the latest run date for that index.

## Logs, notifications, and failure behavior

Logs are written to the console and `nse_scraper.log`. File logs rotate at 5 MB and retain three backups. `_current_step` records the active stage so an unhandled exception identifies where the run stopped.

When `slack_webhook_url` is configured, the job sends start, success, warning, and error messages. Ownership mentions are hard-coded in `_SLACK_MENTIONS`; update them during the handover.

Operationally important behavior:

- An unhandled top-level exception is logged, sent to Slack, and exits with status 1.
- A database write failure for one index is caught and logged; the loop continues to the next index.
- An NSE response with no data for one index is also logged and skipped; other indices continue.
- Count mismatches and missing ISINs are warnings, not fatal errors.
- Therefore, a process exit code or final “completed” message alone is not enough to prove that every index was stored. Always review the per-index summary and validate PostgreSQL.
- If an index fetch fails during a rerun, the job does not delete any older snapshot already stored for that index in the same month. Validation by `snapshot_date` will reveal that mismatch.

## Known issues and limitations

### NIFTY MICROCAP 250 may produce 249 unique symbols

On 2 July 2026, NSE returned `TARC` twice: once as `TARC LIMITED` and once under the legacy name `ANANT RAJ GLOBAL LIMITED`. `_dedup_symbols()` retained the first occurrence and dropped the second, resulting in 249 unique rows.

This is intentionally surfaced as a warning. If it recurs, confirm the current NSE data before adding a permanent exception or changing the expected count.

### Missing ISINs depend on upstream symbology

If a symbol has no current mapping in `symbology_changes`, the CSV and workbook contain a blank ISIN and the database row is dropped. Fix the upstream mapping, then rerun the scraper; do not invent an ISIN in this project.

### NSE API aliases can change

If all aliases are exhausted for an index, inspect the NSE index page's browser network requests and find the current `symbol` value used by `marketWatchApi`. Add the verified value to that index's alias list in `INDICES`, retaining useful older aliases as fallbacks.

### TLS/browser impersonation can become stale

The session currently uses `impersonate="chrome120"`. If NSE begins rejecting it or a future `curl_cffi` release removes it, update the impersonation target to a version supported by the installed `curl_cffi`, then perform a dry run.

### Relative paths require the correct working directory

The script opens `db_config.yaml` and writes outputs using relative paths. Running it from a different directory may cause a missing-config error or write files to the wrong location.

## Troubleshooting guide

| Symptom | Likely cause | Action |
|---|---|---|
| Scheduled task does not start or cannot find `G:` | Google Drive is not mounted for the task's run-as account | Sign in to the RDP host with the task account, confirm Google Drive for desktop is running, verify the `G:` path, and rerun the task |
| Batch reports that Python cannot be found | The `aa_rebal` environment moved or the task is running on the wrong host/account | Verify `C:\Users\wm-eikon-user\anaconda3\envs\aa_rebal\python.exe` and update the batch file only after confirming the replacement environment |
| `FileNotFoundError: db_config.yaml` | Wrong working directory or missing production secret file | Run from the project directory and restore the config through the approved secret channel |
| PostgreSQL authentication or connection error | Invalid credentials, VPN/network issue, or database unavailable | Test connectivity and obtain current credentials from the database owner |
| `symbology_changes` query fails | Missing table/columns or insufficient read permission | Confirm the upstream schema and grant read access; the query expects `new_ticker`, `new_isin`, and `updated_at` |
| NSE homepage or market page returns non-200 | NSE blocking, network/proxy issue, or stale TLS fingerprint | Check host connectivity, proxy policy, and the `curl_cffi` impersonation target |
| “All aliases exhausted” | NSE changed the internal symbol or response shape | Inspect NSE browser network traffic and update `INDICES`/response parsing after validation |
| Missing ISIN warning | No current upstream ticker mapping | Correct `symbology_changes`, then rerun |
| CSV is updated but DB is not | Dry-run mode or a caught per-index DB error | Check the command, logs, Slack message, and destination table |
| Excel file cannot be replaced | Workbook is open in Excel or directory is not writable | Close the workbook and rerun from a writable repository directory |

## Making changes safely

When changing an endpoint, alias, output field, database mapping, or dependency:

1. Keep secrets out of source control.
2. Run a Python syntax check:

   ```powershell
   .\.venv\Scripts\python.exe -m py_compile nse_index_constituents.py
   ```

3. Run `pip check` after dependency changes.
4. Run the scraper with `--dry-run` and compare counts, columns, duplicates, and ISIN coverage with the prior output.
5. Run the full job only after reviewing warnings.
6. Validate the database with the SQL above.
7. Update this README when behavior, ownership, scheduling, configuration, or downstream usage changes.

No automated test suite is currently included, so the dry run and data validation checks are the regression test for this project.
