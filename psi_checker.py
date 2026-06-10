"""
PageSpeed Insights Checker
Kiểm tra hiệu suất website và ghi kết quả vào Google Sheets.
"""

import os
import time
import requests
import schedule
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

WORKERS = int(os.getenv("PSI_WORKERS", "3"))  # số luồng check song song

from config import (
    PSI_API_KEY, SHEET_ID, SHEET_TAB, SERVICE_ACCOUNT_FILE, SERVICE_ACCOUNT_JSON,
    URLS, STRATEGIES, SCHEDULE_TIME, SCHEDULE_MODE,
    SCHEDULE_WEEKDAY, SCHEDULE_DAY_OF_MONTH, REQUEST_DELAY
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("psi_checker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Các cột header trong Google Sheet
HEADERS = [
    "Timestamp", "URL", "Strategy",
    "Performance Score",
    "FCP (ms)", "LCP (ms)", "CLS", "TBT (ms)", "INP (ms)", "TTFB (ms)", "Speed Index (ms)",
    "FCP Category", "LCP Category", "CLS Category", "TBT Category", "INP Category", "TTFB Category",
]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheets_service():
    # Lazy import: server vẫn khởi động được nếu thiếu google libs (chỉ fail khi thật sự gọi)
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if SERVICE_ACCOUNT_JSON:
        import json

        creds = service_account.Credentials.from_service_account_info(
            json.loads(SERVICE_ACCOUNT_JSON), scopes=SCOPES
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    # cache_discovery=False: tắt warning "file_cache is only supported with oauth2client<4.0.0"
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_or_create_tab(service, tab_name: str):
    """Tạo tab mới nếu chưa tồn tại, trả về tab_name."""
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab_name not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ).execute()
        log.info(f"Đã tạo tab mới: {tab_name}")
    return tab_name


def ensure_header(service, tab_name: str):
    """Tạo header row nếu tab còn trống."""
    sheet = service.spreadsheets()
    result = sheet.values().get(
        spreadsheetId=SHEET_ID,
        range=f"{tab_name}!A1:Z1",
    ).execute()
    if not result.get("values"):
        sheet.values().update(
            spreadsheetId=SHEET_ID,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        log.info(f"Đã tạo header row trong tab '{tab_name}'.")


def append_rows(service, rows: list[list], tab_name: str):
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{tab_name}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def apply_conditional_formatting(service, tab_name: str):
    """Tô màu xanh/đỏ cho các cột chỉ số theo ngưỡng Core Web Vitals."""
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    sheet_id = next(
        s["properties"]["sheetId"]
        for s in meta["sheets"]
        if s["properties"]["title"] == tab_name
    )

    GREEN  = {"red": 0.714, "green": 0.843, "blue": 0.659}  # #b6d7a8
    YELLOW = {"red": 1.000, "green": 0.898, "blue": 0.600}  # #ffe599
    RED    = {"red": 0.918, "green": 0.600, "blue": 0.600}  # #ea9999

    def col_range(col_index):
        return {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 1000,
            "startColumnIndex": col_index, "endColumnIndex": col_index + 1,
        }

    def rule(col_index, good_cond, mid_cond, bad_cond):
        return [
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [col_range(col_index)],
                        "booleanRule": {"condition": good_cond, "format": {"backgroundColor": GREEN}},
                    },
                    "index": 0,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [col_range(col_index)],
                        "booleanRule": {"condition": mid_cond, "format": {"backgroundColor": YELLOW}},
                    },
                    "index": 0,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [col_range(col_index)],
                        "booleanRule": {"condition": bad_cond, "format": {"backgroundColor": RED}},
                    },
                    "index": 0,
                }
            },
        ]

    def gte(val):     return {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": str(val)}]}
    def lte(val):     return {"type": "NUMBER_LESS_THAN_EQ",   "values": [{"userEnteredValue": str(val)}]}
    def gt(val):      return {"type": "NUMBER_GREATER",        "values": [{"userEnteredValue": str(val)}]}
    def lt(val):      return {"type": "NUMBER_LESS",           "values": [{"userEnteredValue": str(val)}]}
    def between(a,b): return {"type": "NUMBER_BETWEEN",        "values": [{"userEnteredValue": str(a)}, {"userEnteredValue": str(b)}]}

    # col, good,          mid (vàng),           bad
    thresholds = [
        (3,  gte(90),      between(50, 89),      lt(50)),     # Performance Score
        (4,  lte(1800),    between(1801, 3000),  gt(3000)),   # FCP ms
        (5,  lte(2500),    between(2501, 4000),  gt(4000)),   # LCP ms
        (6,  lte(0.1),     between(0.101, 0.25), gt(0.25)),   # CLS
        (7,  lte(200),     between(201, 600),    gt(600)),    # TBT ms
        (8,  lte(200),     between(201, 500),    gt(500)),    # INP ms
        (9,  lte(800),     between(801, 1800),   gt(1800)),   # TTFB ms
        (10, lte(3400),    between(3401, 5800),  gt(5800)),   # Speed Index
    ]

    requests = []
    for col, good, mid, bad in thresholds:
        requests.extend(rule(col, good, mid, bad))

    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": requests},
    ).execute()
    log.info(f"Đã áp dụng conditional formatting cho tab '{tab_name}'.")


# ── PageSpeed Insights API ────────────────────────────────────────────────────

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

METRIC_KEYS = {
    "first-contentful-paint":   "FCP",
    "largest-contentful-paint": "LCP",
    "cumulative-layout-shift":  "CLS",
    "total-blocking-time":      "TBT",
    "interaction-to-next-paint": "INP",
    "server-response-time":     "TTFB",
    "speed-index":              "Speed Index",
}


def fetch_psi(url: str, strategy: str) -> tuple[dict | None, str | None]:
    """Gọi PSI 1 lần. Trả về (json, None) hoặc (None, "lý do lỗi")."""
    params = {
        "url": url,
        "strategy": strategy,
        "key": PSI_API_KEY,
    }
    try:
        resp = requests.get(PSI_URL, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json(), None
    except requests.HTTPError as e:
        reason = f"HTTP {e.response.status_code}"
        log.error(f"Lỗi khi gọi PSI cho {url} [{strategy}]: {reason}")
        return None, reason
    except requests.RequestException as e:
        reason = type(e).__name__
        log.error(f"Lỗi khi gọi PSI cho {url} [{strategy}]: {e}")
        return None, reason


def check_one(url: str, strategy: str, timestamp: str, delay: int,
              retries: int = 3) -> dict:
    """Check 1 URL+strategy với retry. Trả về dict kết quả (row + metadata)."""
    t0 = time.time()
    err = None
    for attempt in range(1, retries + 1):
        data, err = fetch_psi(url, strategy)
        if data:
            row = parse_result(data, url, strategy, timestamp)
            time.sleep(delay)
            return {"row": row, "score": row[3], "error": None, "attempts": attempt,
                    "elapsed": round(time.time() - t0, 1)}
        if attempt < retries:
            wait = min(2 ** attempt, 5)
            log.info(f"  Retry {attempt + 1}/{retries} cho {url} [{strategy}] sau {wait}s...")
            time.sleep(wait)
    row = [timestamp, url, strategy.upper()] + ["ERROR"] * (len(HEADERS) - 3)
    time.sleep(delay)
    return {"row": row, "score": None, "error": err, "attempts": retries,
            "elapsed": round(time.time() - t0, 1)}


def parse_result(data: dict, url: str, strategy: str, timestamp: str) -> list:
    cats = data.get("lighthouseResult", {}).get("categories", {})
    audits = data.get("lighthouseResult", {}).get("audits", {})

    perf_score = round((cats.get("performance", {}).get("score") or 0) * 100)

    def metric_value(key):
        audit = audits.get(key, {})
        # CLS — nilai desimal, bukan milidetik
        if key == "cumulative-layout-shift":
            return round(audit.get("numericValue", 0), 3)
        return round(audit.get("numericValue", 0))

    def metric_category(key):
        return audits.get(key, {}).get("displayValue", "N/A")

    row = [
        timestamp,
        url,
        strategy.upper(),
        perf_score,
        metric_value("first-contentful-paint"),
        metric_value("largest-contentful-paint"),
        metric_value("cumulative-layout-shift"),
        metric_value("total-blocking-time"),
        metric_value("interaction-to-next-paint"),
        metric_value("server-response-time"),
        metric_value("speed-index"),
        metric_category("first-contentful-paint"),
        metric_category("largest-contentful-paint"),
        metric_category("cumulative-layout-shift"),
        metric_category("total-blocking-time"),
        metric_category("interaction-to-next-paint"),
        metric_category("server-response-time"),
    ]
    return row


# ── Main job ──────────────────────────────────────────────────────────────────

def run_check_iter():
    """Generator: yield tiến trình từng bước (dùng cho streaming real-time).

    Events: {"event":"start","total","tab"} → {"event":"check","i","total","url","strategy","score"}*
            → {"event":"saved","rows","tab"}
    """
    # Đọc config động (URLs/strategies/delay có thể đã chỉnh qua UI/API/chat)
    import runtime_config

    cfg = runtime_config.current()
    urls, strategies, delay = cfg["urls"], cfg["strategies"], cfg["request_delay"]
    total = len(urls) * len(strategies)

    log.info("=" * 60)
    log.info(f"Bắt đầu kiểm tra PSI — {len(urls)} URL × {len(strategies)} strategy")
    log.info("=" * 60)

    now = datetime.now()
    tab_name = now.strftime("%Y-%m")   # ví dụ: "2026-06"
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    yield {"event": "start", "total": total, "tab": tab_name}

    t_run = time.time()
    service = get_sheets_service()
    get_or_create_tab(service, tab_name)
    ensure_header(service, tab_name)

    tasks = [(idx, url, strategy)
             for idx, (url, strategy) in enumerate((u, s) for u in urls for s in strategies)]
    results: dict[int, list] = {}
    done = ok_count = err_count = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check_one, url, strategy, timestamp, delay): (idx, url, strategy)
                   for idx, url, strategy in tasks}
        for fut in as_completed(futures):
            idx, url, strategy = futures[fut]
            r = fut.result()
            results[idx] = r["row"]
            done += 1
            if r["score"] is not None:
                ok_count += 1
                log.info(f"  → {url} [{strategy}]: {r['score']}/100 ({r['elapsed']}s)")
            else:
                err_count += 1
            yield {"event": "check", "i": done, "total": total, "url": url,
                   "strategy": strategy, "score": r["score"], "elapsed": r["elapsed"],
                   "error": r["error"], "attempts": r["attempts"]}

    rows = [results[idx] for idx in sorted(results)]  # giữ thứ tự URL trong Sheet

    if rows:
        append_rows(service, rows, tab_name)
        log.info(f"Đã ghi {len(rows)} dòng vào tab '{tab_name}'.")
        apply_conditional_formatting(service, tab_name)
    log.info("Hoàn thành.")
    yield {"event": "saved", "rows": len(rows), "tab": tab_name,
           "total": total, "ok": ok_count, "errors": err_count,
           "duration": round(time.time() - t_run, 1)}


def run_check():
    """Chạy trọn vẹn (scheduler / CLI --once dùng cái này)."""
    for _ in run_check_iter():
        pass


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _monthly_job():
    """Wrapper: chỉ thực sự chạy nếu hôm nay đúng ngày trong tháng đã cấu hình."""
    if datetime.now().day == SCHEDULE_DAY_OF_MONTH:
        run_check()


def start_scheduler():
    log.info(f"Scheduler khởi động — mode={SCHEDULE_MODE}, time={SCHEDULE_TIME}")

    if SCHEDULE_MODE == "daily":
        schedule.every().day.at(SCHEDULE_TIME).do(run_check)

    elif SCHEDULE_MODE == "weekly":
        getattr(schedule.every(), SCHEDULE_WEEKDAY).at(SCHEDULE_TIME).do(run_check)

    elif SCHEDULE_MODE == "monthly":
        # Kiểm tra mỗi ngày, chỉ chạy thật khi đúng ngày trong tháng
        log.info(f"  → Sẽ chạy vào ngày {SCHEDULE_DAY_OF_MONTH} mỗi tháng lúc {SCHEDULE_TIME}")
        schedule.every().day.at(SCHEDULE_TIME).do(_monthly_job)

    else:
        log.error(f"SCHEDULE_MODE không hợp lệ: '{SCHEDULE_MODE}'. Dùng daily/weekly/monthly.")
        return

    # Chạy ngay lần đầu khi khởi động
    run_check()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        # Chạy 1 lần rồi thoát: python psi_checker.py --once
        run_check()
    else:
        # Chạy scheduler liên tục
        start_scheduler()
