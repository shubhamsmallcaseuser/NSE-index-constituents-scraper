"""
NSE Index Constituents Scraper
===============================
Fetches live constituents for:
  - NIFTY 100
  - NIFTY MIDCAP 150
  - NIFTY SMALLCAP 250
  - NIFTY MICROCAP 250

Usage:
    python nse_index_constituents.py           # full run (CSV + Excel + DB)
    python nse_index_constituents.py --dry-run  # CSV + Excel only, no DB writes

Output files:
    ind_nifty100list.csv
    ind_niftymidcap150list.csv
    ind_niftysmallcap250list.csv
    ind_niftymicrocap250list.csv
    nse_all_constituents.xlsx   — all four indices in one Excel (one sheet each)
    nse_scraper.log             — rotating log file (5 MB x 3 backups)
"""

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import time
import urllib.request
import yaml
import pandas as pd
from datetime import datetime, date
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from curl_cffi import requests  # impersonates Chrome TLS — required for NSE

# ── Module-level context (used in error messages) ─────────────────────────────

_HOSTNAME    = socket.gethostname()
_WORKDIR     = os.getcwd()
_current_step = "not started"  # updated throughout _run() for error context

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    log = logging.getLogger("nse_scraper")
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    # Console — UTF-8 so emojis render correctly on Windows
    ch = logging.StreamHandler(stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False))
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    # Rotating file — 5 MB, keep 3 backups
    fh = logging.handlers.RotatingFileHandler(
        "nse_scraper.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    log.addHandler(ch)
    log.addHandler(fh)
    return log

log = setup_logging()

# ── Slack alerts ──────────────────────────────────────────────────────────────

_slack_webhook:  str | None = None
_SLACK_MENTIONS: str        = "@srivatsa.rao @shubham.shreshtha"

def init_slack(webhook_url: str | None):
    global _slack_webhook
    _slack_webhook = webhook_url

def slack(message: str, level: str = "info", mentions: bool = False):
    """
    Post a message to Slack.
    - level  : "info" | "success" | "warning" | "error"
    - mentions: if True, appends the configured user mention string so the
                right people get notified (use for start / complete / error).
    Silently skips if no webhook is configured.
    """
    if not _slack_webhook:
        return
    emoji = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}.get(level, "ℹ️")
    prefix = f"{_SLACK_MENTIONS}\n" if mentions else ""
    text   = f"{prefix}{emoji} {message}"
    payload = json.dumps({"text": text}).encode()
    try:
        req = urllib.request.Request(
            _slack_webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning("Slack alert failed: %s", e)

# ── DB Setup & Helpers ────────────────────────────────────────────────────────

def connect_db():
    """Create and return a DB engine from config (no DDL). Used by both normal and dry-run modes."""
    with open("db_config.yaml", "r") as f:
        config = yaml.safe_load(f)["wm_price_db"]
    init_slack(config.get("slack_webhook_url"))
    conn_str = "postgresql://{}:{}@{}:{}/{}".format(
        config["user"], config["password"],
        config["host"], config["port"], config["dbname"]
    )
    return create_engine(conn_str)

def init_db():
    """Connect to Postgres and create/migrate tables if missing. Normal mode only."""
    engine = connect_db()
    with engine.begin() as conn:
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS index_constituents_monthly (
                snapshot_date       DATE NOT NULL,
                index_name          VARCHAR(50) NOT NULL,
                symbol              VARCHAR(50) NOT NULL,
                isin                VARCHAR(15) NOT NULL,
                company_name        VARCHAR(255),
                market_cap_bucket   VARCHAR(20) NOT NULL,
                inserted_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (snapshot_date, index_name, isin)
            )
        '''))
        conn.execute(text('ALTER TABLE index_constituents_monthly DROP COLUMN IF EXISTS industry'))
        conn.execute(text('ALTER TABLE index_constituents_monthly DROP COLUMN IF EXISTS series'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_monthly_date_index ON index_constituents_monthly(snapshot_date, index_name)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_monthly_isin ON index_constituents_monthly(isin)'))
    return engine

def load_isin_map(engine) -> pd.DataFrame:
    """
    Query symbology_changes for the latest new_ticker → new_isin mapping.
    DISTINCT ON (new_ticker) ordered by updated_at DESC ensures we always
    use the most recently updated row when a ticker has multiple history rows.
    """
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT DISTINCT ON (new_ticker)
                   new_ticker AS symbol,
                   new_isin   AS isin
            FROM   symbology_changes
            WHERE  new_ticker IS NOT NULL
              AND  new_isin   IS NOT NULL
              AND  new_isin   != ''
            ORDER  BY new_ticker, updated_at DESC
        """))
        return pd.DataFrame(result.fetchall(), columns=["Symbol", "ISIN Code"])

def _dedup_symbols(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Drop duplicate symbol rows emitted by NSE's API (e.g. TARC appearing twice
    for the renamed ANANT RAJ GLOBAL entity). Keep the first occurrence.
    Returns (deduplicated DataFrame, list of dropped company names).
    """
    dupes = df[df.duplicated("Symbol", keep=False)]
    if dupes.empty:
        return df, []

    dropped = []
    for sym, group in dupes.groupby("Symbol"):
        names = group["Company Name"].tolist()
        dropped_names = names[1:]
        dropped.extend(dropped_names)
        log.warning("   Duplicate symbol '%s' — keeping '%s', dropping: %s",
                    sym, names[0], dropped_names)

    return df.drop_duplicates(subset="Symbol", keep="first").reset_index(drop=True), dropped


def get_market_cap_bucket(index_name: str) -> str:
    mapping = {
        "NIFTY 100":          "Large Cap",
        "NIFTY MIDCAP 150":   "Mid Cap",
        "NIFTY SMALLCAP 250": "Small Cap",
        "NIFTY MICROCAP 250": "Micro Cap",
    }
    return mapping.get(index_name, "Other")

# ── Config ────────────────────────────────────────────────────────────────────

INDICES = {
    "NIFTY 100":          (["NIFTY%20100"],                                          "ind_nifty100list.csv"),
    "NIFTY MIDCAP 150":   (["NIFTY%20MIDCAP%20150"],                                "ind_niftymidcap150list.csv"),
    "NIFTY SMALLCAP 250": (["NIFTY%20SMLCAP%20250",   "NIFTY%20SMALLCAP%20250"],    "ind_niftysmallcap250list.csv"),
    "NIFTY MICROCAP 250": (["NIFTY%20MICROCAP250",    "NIFTY%20MICROCAP%20250"],    "ind_niftymicrocap250list.csv"),
}

EXPECTED_COUNTS = {
    "NIFTY 100":           100,
    "NIFTY MIDCAP 150":    150,
    "NIFTY SMALLCAP 250":  250,
    "NIFTY MICROCAP 250":  250,
}

EXPECTED_TOTAL = 750

OUTPUT_COLS = ["Company Name", "Symbol", "ISIN Code"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
}

# ── Session setup ─────────────────────────────────────────────────────────────

def build_session() -> requests.Session:
    """
    curl_cffi impersonates Chrome's TLS fingerprint — NSE blocks standard
    requests/urllib at the handshake level even with correct headers.
    We warm the session by visiting homepage + market page to seed cookies.
    """
    session = requests.Session(impersonate="chrome120")
    session.headers.update(HEADERS)

    log.info("Establishing NSE session (Chrome TLS impersonation)...")

    r = session.get("https://www.nseindia.com", timeout=20)
    log.info("   Homepage    : HTTP %s", r.status_code)
    r.raise_for_status()
    time.sleep(2)

    r = session.get("https://www.nseindia.com/market-data/live-equity-market", timeout=20)
    log.info("   Market page : HTTP %s", r.status_code)
    r.raise_for_status()
    time.sleep(1)

    return session

# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_index(session: requests.Session, name: str, idx_aliases: list) -> pd.DataFrame:
    """Tries each symbol alias in order until one returns data."""
    log.info("Fetching %s ...", name)

    for alias in idx_aliases:
        url = (
            "https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi"
            f"?functionName=getIndicesData&symbol={alias}"
        )
        r = session.get(url, timeout=20)
        log.debug("   [%s] HTTP %s", alias, r.status_code)
        r.raise_for_status()

        payload = r.json()
        data = payload.get("data", {})
        if isinstance(data, dict):
            records = data.get("indexStockData", data.get("data", []))
        else:
            records = data

        if records:
            log.debug("   [%s] returned %d raw records", alias, len(records))
            break
        log.warning("   [%s] returned empty data — trying next alias...", alias)
    else:
        log.error("   All aliases exhausted for %s. Tried: %s", name, idx_aliases)
        return pd.DataFrame()

    # Drop the index-level aggregate row — it has series=None and companyName=None
    records = [r for r in records if r.get("series") is not None]

    rows = []
    for rec in records:
        meta = rec.get("meta", {})
        rows.append({
            "Company Name": rec.get("companyName", meta.get("companyName", "")),
            "Symbol":       rec.get("symbol", ""),
            "ISIN Code":    rec.get("isin",    meta.get("isin", "")),
        })

    df = pd.DataFrame(rows, columns=OUTPUT_COLS)
    log.info("   %d stock records after aggregate row removed", len(df))
    return df

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NSE Index Constituents Scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data and write CSV/Excel files but skip all database writes.",
    )
    args = parser.parse_args()

    mode = "DRY RUN" if args.dry_run else "FULL RUN"
    log.info("=" * 60)
    log.info("NSE Index Constituents Scraper — %s", mode)
    log.info("Host: %s | Dir: %s", _HOSTNAME, _WORKDIR)
    log.info("=" * 60)
    if args.dry_run:
        log.info("CSV/Excel will be written; DB will NOT be touched.")

    try:
        _run(args)
    except Exception as e:
        log.exception("Unhandled error — scraper aborted at step: %s", _current_step)
        slack(
            f"*NSE Scraper — {mode} FAILED*\n"
            f"Host: `{_HOSTNAME}` | {datetime.now().strftime('%d %b %Y, %H:%M:%S')}\n"
            f"*Step:* {_current_step}\n"
            f"*Error:* `{type(e).__name__}: {e}`\n"
            f"*Action:* Check `nse_scraper.log` on `{_HOSTNAME}` for full traceback",
            level="error",
            mentions=True,
        )
        sys.exit(1)

def _run(args):
    global _current_step
    mode     = "DRY RUN" if args.dry_run else "FULL RUN"
    run_date = date.today()
    start_ts = time.monotonic()

    # ── Startup alert ────────────────────────────────────────────────────────
    _current_step = "connecting to database"
    engine = connect_db() if args.dry_run else init_db()
    # init_slack is called inside connect_db, so the webhook is live from here

    slack(
        "NSE index constituents ingestion started"
        + (" _(dry run — no DB writes)_" if args.dry_run else ""),
        level="info",
        mentions=True,
    )

    # ── Session ──────────────────────────────────────────────────────────────
    _current_step = "establishing NSE session"
    log.info("Step: %s", _current_step)
    session = build_session()

    # ── ISIN map ─────────────────────────────────────────────────────────────
    _current_step = "loading ISIN map from symbology_changes"
    log.info("Step: %s", _current_step)
    isin_map = load_isin_map(engine)
    log.info("   %d symbol→ISIN mappings loaded", len(isin_map))

    all_sheets:    dict[str, pd.DataFrame] = {}
    index_summary: list[str]               = []   # per-index lines for final Slack

    # ── Per-index fetch → process → save ─────────────────────────────────────
    for name, (aliases, csv_filename) in INDICES.items():
        expected = EXPECTED_COUNTS[name]

        _current_step = f"fetching {name}"
        log.info("─" * 50)
        log.info("Step: %s", _current_step)
        df = fetch_index(session, name, aliases)

        if df.empty:
            log.error("No data returned for %s — skipping this index", name)
            slack(
                f"*NSE Scraper — {name}*\n"
                f"No data returned from NSE API.\n"
                f"Aliases tried: `{'`, `'.join(aliases)}`\n"
                f"This index will be *absent* from today's snapshot.",
                level="error",
            )
            index_summary.append(f"  {name:<25} : NO DATA ❌")
            continue

        # Dedup
        _current_step = f"deduplicating {name}"
        df, dropped_names = _dedup_symbols(df)

        # ISIN enrichment
        _current_step = f"enriching ISINs for {name}"
        df = df.drop(columns=["ISIN Code"], errors="ignore")
        df = df.merge(isin_map, on="Symbol", how="left")

        missing_isin_mask    = df["ISIN Code"].isna()
        missing_isin_count   = missing_isin_mask.sum()
        missing_isin_symbols = df.loc[missing_isin_mask, "Symbol"].tolist()

        # Per-index Slack status
        count      = len(df)
        count_flag = "✅" if count == expected else "⚠️"
        isin_flag  = "" if not missing_isin_count else f" | {missing_isin_count} missing ISIN"

        per_index_notes = []
        if dropped_names:
            per_index_notes.append(f"duplicate symbol dropped — {dropped_names}")
        if missing_isin_count:
            per_index_notes.append(f"symbols with no ISIN: `{'`, `'.join(missing_isin_symbols)}`")
            log.warning("   %d symbol(s) with no ISIN: %s", missing_isin_count, missing_isin_symbols)

        if per_index_notes or count != expected:
            slack_level = "warning"
            slack_body  = (
                f"*NSE Scraper — {name}*\n"
                f"{count_flag} {count} stocks (expected {expected}){isin_flag}"
            )
            if per_index_notes:
                slack_body += "\n• " + "\n• ".join(per_index_notes)
            slack(slack_body, level=slack_level)
        else:
            log.info("   %s — %d/%d stocks, ISINs complete", name, count, expected)

        index_summary.append(
            f"  {name:<25} : {count:>3} {count_flag}{isin_flag}"
        )

        # Write CSV
        _current_step = f"writing CSV for {name}"
        df[OUTPUT_COLS].to_csv(csv_filename, index=False)
        log.info("   Saved → %s", csv_filename)

        # DB write (skipped in dry-run)
        if not args.dry_run:
            _current_step = f"writing DB for {name}"
            db_df = df[OUTPUT_COLS].copy().rename(columns={
                "Company Name": "company_name",
                "Symbol":       "symbol",
                "ISIN Code":    "isin",
            })
            no_isin = db_df["isin"].isna().sum()
            if no_isin:
                log.warning("   Dropping %d row(s) with missing ISIN before DB write: %s",
                            no_isin, db_df.loc[db_df["isin"].isna(), "symbol"].tolist())
                db_df = db_df.dropna(subset=["isin"])

            db_df["snapshot_date"]     = run_date.isoformat()
            db_df["index_name"]        = name
            db_df["market_cap_bucket"] = get_market_cap_bucket(name)

            try:
                with engine.begin() as conn:
                    conn.execute(text("""
                        DELETE FROM index_constituents_monthly
                        WHERE index_name = :idx
                          AND DATE_TRUNC('month', snapshot_date) = DATE_TRUNC('month', CAST(:rdate AS DATE))
                    """), {"idx": name, "rdate": run_date})
                    db_df.to_sql("index_constituents_monthly", conn, if_exists="append", index=False)
                log.info("   DB upsert done — %s (%s)", name, run_date.strftime("%b %Y"))
            except Exception as e:
                log.error("   DB write failed for %s: %s", name, e)
                slack(
                    f"*NSE Scraper — DB write failed for {name}*\n"
                    f"`{type(e).__name__}: {e}`\n"
                    f"CSV was saved; re-run or insert manually from `{csv_filename}`",
                    level="error",
                )
        else:
            log.info("   DB write skipped (dry run)")

        all_sheets[name] = df
        time.sleep(1.5)

    # ── Excel ─────────────────────────────────────────────────────────────────
    if all_sheets:
        _current_step = "writing combined Excel"
        xlsx_path = "nse_all_constituents.xlsx"
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            for sheet_name, df in all_sheets.items():
                df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
        log.info("Excel → %s", xlsx_path)

    # ── Summary ───────────────────────────────────────────────────────────────
    _current_step = "finalizing"
    total    = sum(len(d) for d in all_sheets.values())
    elapsed  = time.monotonic() - start_ts
    duration = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

    total_flag = "✅" if total == EXPECTED_TOTAL else f"⚠️ (expected {EXPECTED_TOTAL})"
    index_summary.append(f"  {'TOTAL':<25} : {total:>3} {total_flag}")
    summary_block = "\n".join(index_summary)

    log.info("=" * 60)
    log.info("Run complete | %s | Duration: %s", run_date.strftime("%d %b %Y"), duration)
    for line in index_summary:
        log.info(line)
    log.info("=" * 60)

    if total != EXPECTED_TOTAL:
        log.warning("Total %d differs from expected %d", total, EXPECTED_TOTAL)

    if not all_sheets:
        slack(
            f"*NSE Scraper — {mode} finished with NO DATA*\n"
            f"Host: `{_HOSTNAME}` | Every index fetch failed.\n"
            f"Check `nse_scraper.log` on `{_HOSTNAME}`",
            level="error",
            mentions=True,
        )
    else:
        slack(
            f"NSE index constituents ingestion completed successfully\n"
            f"```\n{summary_block}\n```",
            level="success",
            mentions=True,
        )

    engine.dispose()

if __name__ == "__main__":
    main()
