# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Automated Google PageSpeed Insights (PSI) checker that runs on a schedule, tests a list of URLs for performance metrics, and writes results to a Google Sheet with conditional color formatting (green/yellow/red).

## Commands

```powershell
# Install dependencies
python -m pip install -r requirements.txt

# Run a one-off check immediately (all URLs in config.py)
python psi_checker.py --once

# Run the scheduler (keeps running, fires on SCHEDULE_DAY_OF_MONTH each month)
python psi_checker.py

# Create / refresh the Knowledge Hub reference tab in the Google Sheet
python create_knowledge_hub.py
```

## Architecture

Two files do all the work:

**`config.py`** — single source of truth for every tunable value:
- `PSI_API_KEY`, `SHEET_ID`, `SERVICE_ACCOUNT_FILE` — credentials
- `URLS` — list of URLs to check
- `STRATEGIES` — `["mobile", "desktop"]`
- `SCHEDULE_MODE` / `SCHEDULE_TIME` / `SCHEDULE_DAY_OF_MONTH` / `SCHEDULE_WEEKDAY`
- `REQUEST_DELAY` — seconds between API calls (avoids rate limiting)

**`psi_checker.py`** — all logic:
- `get_sheets_service()` — builds Google Sheets API client from service account file
- `get_or_create_tab(service, tab_name)` — creates a new sheet tab if it doesn't exist
- `ensure_header(service, tab_name)` — writes header row if tab is empty
- `append_rows(service, rows, tab_name)` — appends result rows
- `apply_conditional_formatting(service, tab_name)` — applies green/yellow/red rules to columns D–K based on Core Web Vitals thresholds
- `fetch_psi(url, strategy)` — calls the PSI API, returns raw JSON or `None` on error
- `parse_result(data, url, strategy, timestamp)` — extracts a flat row from PSI JSON
- `run_check()` — orchestrates a full run: determines tab name as `YYYY-MM`, creates tab, writes all rows, applies formatting
- `start_scheduler()` — wraps `schedule` library; for `monthly` mode it fires `run_check()` daily but guards with a day-of-month check

## Google Sheet structure

Each monthly run writes to a tab named `YYYY-MM` (e.g. `2026-07`). The first run ever wrote to `PSI Results` (legacy tab). Column layout (A–Q):

`Timestamp | URL | Strategy | Performance Score | FCP | LCP | CLS | TBT | INP | TTFB | Speed Index | FCP Label | LCP Label | CLS Label | TBT Label | INP Label | TTFB Label`

Conditional formatting covers columns D–K (indices 3–10). Thresholds follow Google's published Core Web Vitals guidelines.

## Credentials

- **PSI API key** — stored in `config.py` as `PSI_API_KEY`
- **Google Service Account** — file `service_account.json` (path set via env `SERVICE_ACCOUNT_FILE`), or paste full JSON content into env `SERVICE_ACCOUNT_JSON` (used for cloud deploy).
- The service account must have Editor access to the target Google Sheet.

## Known issues / quirks

- PSI API occasionally returns 500 or times out (~60 s timeout). Errors are written as `ERROR` cells in the sheet and do not abort the run.
- `file_cache is only supported with oauth2client<4.0.0` warning on startup is harmless — Google's discovery cache falls back to memory.
- Run `python psi_checker.py` on a machine that stays on, or schedule it via Task Scheduler / cron. There is no cloud deployment; the scheduler lives in-process.
