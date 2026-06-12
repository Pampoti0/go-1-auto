"""SEO Agent — module của DeCho Agent.

Kéo dữ liệu Google Search Console + GA4 theo tháng, so sánh với tháng trước,
ghi báo cáo (kèm tô màu % tăng/giảm) vào Google Sheet.

Config qua env (xem .env.example). Auth: OAuth user token —
SEO_TOKEN_JSON (nội dung JSON, dùng khi deploy) hoặc file token.json (local).
CLI: python seo_agent.py [--month YYYY-MM]
"""

import json
import logging
import os

log = logging.getLogger("seo_agent")

# ── Config (env-driven — KHÔNG hardcode ID vào code, repo là public) ──────────
SITE_URL = os.getenv("SEO_SITE_URL", "https://greennode.ai/")
GA4_PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "")
SEO_SHEET_ID = os.getenv("SEO_SHEET_ID", "")
TRACKED_URLS = [u.strip() for u in os.getenv("SEO_TRACKED_URLS", "").split(",") if u.strip()]
ROW_LIMIT = int(os.getenv("SEO_ROW_LIMIT", "5000"))
RUN_DAY_OF_MONTH = int(os.getenv("SEO_RUN_DAY_OF_MONTH", "8"))  # 1–28
RUN_TIME = os.getenv("SEO_RUN_TIME", "08:00")

TOKEN_FILE = os.getenv("SEO_TOKEN_FILE", "token.json")
SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_creds():
    """OAuth user credentials: env SEO_TOKEN_JSON (deploy) hoặc file token.json (local)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_json = os.getenv("SEO_TOKEN_JSON", "")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    else:
        raise RuntimeError(
            "Thiếu OAuth token: đặt env SEO_TOKEN_JSON (nội dung token.json) hoặc file token.json. "
            "Tạo token lần đầu bằng OAuth flow trên máy local (xem SETUP cũ của seo-agent).")
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            if not token_json:  # cập nhật lại file local
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
        else:
            raise RuntimeError("OAuth token hết hạn và không refresh được — tạo lại token.json.")
    return creds


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_url(url: str) -> str:
    url = url.split("?")[0].split("#")[0]
    return url.rstrip("/") + "/"


def runtime_val(key: str, fallback):
    """Đọc giá trị từ runtime_config (chỉnh được qua UI), fallback về env."""
    try:
        import runtime_config

        return runtime_config.current().get(key, fallback)
    except Exception:  # noqa: BLE001
        return fallback


def filter_urls(df):
    df = df.copy()
    df["url"] = df["url"].apply(clean_url)
    num_cols = [c for c in df.columns if c != "url"]
    df = df.groupby("url", as_index=False)[num_cols].sum()
    tracked = runtime_val("seo_tracked_urls", TRACKED_URLS)
    if not tracked:
        return df.reset_index(drop=True)
    base = SITE_URL.rstrip("/")
    targets = {clean_url(base + u) for u in tracked}
    return df[df["url"].isin(targets)].reset_index(drop=True)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_gsc(start: str, end: str):
    import pandas as pd
    from googleapiclient.discovery import build

    svc = build("searchconsole", "v1", credentials=get_creds(), cache_discovery=False)
    resp = svc.searchanalytics().query(
        siteUrl=SITE_URL,
        body={"startDate": start, "endDate": end, "dimensions": ["page"], "rowLimit": ROW_LIMIT},
    ).execute()
    rows = resp.get("rows", [])
    if not rows:
        return pd.DataFrame(columns=["url", "clicks", "impressions"])
    return pd.DataFrame([{
        "url": r["keys"][0],
        "clicks": int(r.get("clicks", 0)),
        "impressions": int(r.get("impressions", 0)),
    } for r in rows])


def fetch_ga4(start: str, end: str):
    import pandas as pd
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (DateRange, Dimension, Metric,
                                                    RunReportRequest)

    client = BetaAnalyticsDataClient(credentials=get_creds())
    resp = client.run_report(RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews"), Metric(name="totalUsers")],
        limit=ROW_LIMIT,
    ))
    if not resp.rows:
        return pd.DataFrame(columns=["url", "views", "users"])
    base = SITE_URL.rstrip("/")
    return pd.DataFrame([{
        "url": base + r.dimension_values[0].value,
        "views": int(r.metric_values[0].value),
        "users": int(r.metric_values[1].value),
    } for r in resp.rows])


# ── Build report ──────────────────────────────────────────────────────────────

def pct(cur, prev):
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 2)


def build_report(cur_gsc, cur_ga4, prev_gsc, prev_ga4):
    import pandas as pd

    cur = pd.merge(cur_gsc, cur_ga4, on="url", how="outer").fillna(0)
    prev = pd.merge(prev_gsc, prev_ga4, on="url", how="outer").fillna(0)
    df = pd.merge(cur, prev, on="url", how="left", suffixes=("", "_prev"))
    for col in ["views", "users", "clicks", "impressions"]:
        df[f"{col}_change_%"] = df.apply(lambda r: pct(r[col], r.get(f"{col}_prev", 0)), axis=1)
    cols = ["url", "views", "views_change_%", "users", "users_change_%",
            "clicks", "clicks_change_%", "impressions", "impressions_change_%"]
    return df[[c for c in cols if c in df.columns]]


# ── Save to Sheets ────────────────────────────────────────────────────────────

GREEN = {"red": 0.714, "green": 0.843, "blue": 0.659}
RED = {"red": 0.918, "green": 0.600, "blue": 0.600}
HEADER_BG = {"red": 0.263, "green": 0.263, "blue": 0.263}
HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}


def _color_requests(ws_id: int, df) -> list:
    import pandas as pd

    change_cols = [i for i, c in enumerate(df.columns) if c.endswith("_change_%")]
    requests = []
    for row_idx, (_, row) in enumerate(df.iterrows()):
        sheet_row = row_idx + 2
        for col_idx in change_cols:
            val = row.iloc[col_idx]
            if val == "N/A" or val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            color = GREEN if float(val) >= 0 else RED
            requests.append({"repeatCell": {
                "range": {"sheetId": ws_id, "startRowIndex": sheet_row - 1, "endRowIndex": sheet_row,
                          "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor"}})
    return requests


def _header_request(ws_id: int, num_cols: int) -> list:
    return [{"repeatCell": {
        "range": {"sheetId": ws_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": num_cols},
        "cell": {"userEnteredFormat": {"backgroundColor": HEADER_BG,
                                       "textFormat": {"foregroundColor": HEADER_FG, "bold": True}}},
        "fields": "userEnteredFormat(backgroundColor,textFormat)"}}]


def save_to_sheet(df, label: str):
    import gspread

    gc = gspread.authorize(get_creds())
    ss = gc.open_by_key(SEO_SHEET_ID)
    try:
        ws = ss.worksheet(label)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=label, rows=6000, cols=20)
    display = df.fillna("N/A")
    ws.update([display.columns.tolist()] + display.values.tolist())
    requests = _header_request(ws.id, len(df.columns)) + _color_requests(ws.id, display)
    if requests:
        ss.batch_update({"requests": requests})
    log.info(f"✓ Lưu {len(df)} URLs → sheet '{label}' (có tô màu)")


# ── Main job ──────────────────────────────────────────────────────────────────

def run_for_month(target_year: int, target_month: int):
    if not (SEO_SHEET_ID and GA4_PROPERTY_ID):
        raise RuntimeError("Thiếu SEO_SHEET_ID / GA4_PROPERTY_ID trong env — xem .env.example.")
    from datetime import date

    from dateutil.relativedelta import relativedelta

    first_cur = date(target_year, target_month, 1)
    last_cur = first_cur + relativedelta(months=1) - relativedelta(days=1)
    first_prev = first_cur - relativedelta(months=1)
    last_prev = first_cur - relativedelta(days=1)

    s1, e1 = first_cur.strftime("%Y-%m-%d"), last_cur.strftime("%Y-%m-%d")
    s2, e2 = first_prev.strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d")
    label = first_cur.strftime("%Y-%m")

    log.info(f"Tháng lấy data: {label} ({s1} → {e1}) — so với {s2[:7]}")
    cur_gsc = filter_urls(fetch_gsc(s1, e1))
    cur_ga4 = filter_urls(fetch_ga4(s1, e1))
    prev_gsc = filter_urls(fetch_gsc(s2, e2))
    prev_ga4 = filter_urls(fetch_ga4(s2, e2))
    log.info(f"GSC: {len(cur_gsc)} URL, GA4: {len(cur_ga4)} URL")

    report = build_report(cur_gsc, cur_ga4, prev_gsc, prev_ga4)
    report.insert(0, "month", label)
    save_to_sheet(report, label)
    return {"label": label, "rows": len(report)}


def run_for_range(start: str, end: str):
    """Báo cáo theo khoảng ngày bất kỳ (YYYY-MM-DD), tự so sánh với kỳ liền trước
    cùng số ngày. Sheet label = 'start__end'."""
    if not (SEO_SHEET_ID and GA4_PROPERTY_ID):
        raise RuntimeError("Thiếu SEO_SHEET_ID / GA4_PROPERTY_ID trong env — xem .env.example.")
    from datetime import datetime as _dt, timedelta

    d0 = _dt.strptime(start, "%Y-%m-%d").date()
    d1 = _dt.strptime(end, "%Y-%m-%d").date()
    if d0 > d1:
        d0, d1 = d1, d0
    days = (d1 - d0).days + 1
    if days > 366:
        raise RuntimeError(f"Khoảng quá dài ({days} ngày) — tối đa 366 ngày.")
    p1 = d0 - timedelta(days=1)
    p0 = p1 - timedelta(days=days - 1)
    s1, e1, s2, e2 = d0.isoformat(), d1.isoformat(), p0.isoformat(), p1.isoformat()
    label = f"{s1}__{e1}"

    log.info(f"Khoảng lấy data: {s1} → {e1} ({days} ngày) — so sánh {s2} → {e2}")
    cur_gsc = filter_urls(fetch_gsc(s1, e1))
    cur_ga4 = filter_urls(fetch_ga4(s1, e1))
    log.info(f"Kỳ chính: GSC {len(cur_gsc)} URL, GA4 {len(cur_ga4)} URL")
    prev_gsc = filter_urls(fetch_gsc(s2, e2))
    prev_ga4 = filter_urls(fetch_ga4(s2, e2))
    log.info(f"Kỳ so sánh ({s2} → {e2}): GSC {len(prev_gsc)} URL, GA4 {len(prev_ga4)} URL")

    report = build_report(cur_gsc, cur_ga4, prev_gsc, prev_ga4)
    report.insert(0, "range", label)
    save_to_sheet(report, label)

    # Tổng + % thay đổi so với kỳ trước (hiện trong kết quả chat)
    def _tot(df, col):
        return int(df[col].sum()) if col in df.columns else 0
    totals = {}
    for col, cdf, pdf in (("views", cur_ga4, prev_ga4), ("users", cur_ga4, prev_ga4),
                          ("clicks", cur_gsc, prev_gsc), ("impressions", cur_gsc, prev_gsc)):
        c, p = _tot(cdf, col), _tot(pdf, col)
        totals[col] = {"cur": c, "prev": p, "pct": round((c - p) / p * 100, 1) if p else None}
    log.info("So với kỳ trước: " + ", ".join(
        f"{k} {v['cur']:,} ({('+' if v['pct'] > 0 else '') + str(v['pct']) + '%' if v['pct'] is not None else 'N/A'})"
        for k, v in totals.items()))
    log.info("Hoàn thành!")
    return {"label": label, "rows": len(report), "days": days,
            "compare": f"{s2} → {e2}", "totals": totals}


def run():
    """Chạy cho tháng vừa kết thúc."""
    from datetime import date

    from dateutil.relativedelta import relativedelta

    log.info("SEO Agent bắt đầu chạy")
    target = date.today().replace(day=1) - relativedelta(months=1)
    result = run_for_month(target.year, target.month)
    log.info("Hoàn thành!")
    return result


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", help="Chạy cho tháng cụ thể (YYYY-MM)")
    args = parser.parse_args()
    if args.month:
        y, m = args.month.split("-")
        run_for_month(int(y), int(m))
    else:
        run()
