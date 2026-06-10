"""Persistence qua Google Sheet — sống qua container recreate trên AgentBase.

- Tab `_config`: lưu runtime config (JSON ở ô A1), load lại lúc khởi động.
- Tab `_logs`: mỗi lần chạy check ghi 1 dòng tóm tắt (lịch sử chạy dài hạn).
Mọi thao tác đều best-effort: Sheet lỗi thì agent vẫn chạy bình thường.
"""

import json
import logging
from datetime import datetime

import config
import psi_checker

log = logging.getLogger(__name__)

CONFIG_TAB = "_config"
LOGS_TAB = "_logs"
LOG_HEADERS = ["Timestamp", "Source", "Total checks", "OK", "Errors", "Duration (s)"]


def load_config() -> dict | None:
    """Đọc config đã lưu trên Sheet (None nếu chưa có / lỗi)."""
    try:
        svc = psi_checker.get_sheets_service()
        res = svc.spreadsheets().values().get(
            spreadsheetId=config.SHEET_ID, range=f"{CONFIG_TAB}!A1").execute()
        vals = res.get("values")
        if vals and vals[0]:
            return json.loads(vals[0][0])
    except Exception as e:  # noqa: BLE001
        log.info(f"Không load được config từ Sheet (dùng mặc định): {e}")
    return None


def save_config(cfg: dict) -> None:
    """Ghi config lên Sheet (gọi mỗi khi lưu cấu hình)."""
    try:
        svc = psi_checker.get_sheets_service()
        psi_checker.get_or_create_tab(svc, CONFIG_TAB)
        svc.spreadsheets().values().update(
            spreadsheetId=config.SHEET_ID, range=f"{CONFIG_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [[json.dumps(cfg, ensure_ascii=False)]]}).execute()
        log.info("Đã đồng bộ config lên Sheet tab _config.")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Không lưu được config lên Sheet: {e}")


def read_results(max_rows: int = 150) -> tuple[str | None, list, list]:
    """Đọc kết quả từ tab tháng mới nhất (YYYY-MM). Trả về (tab, headers, rows)."""
    import re

    try:
        svc = psi_checker.get_sheets_service()
        meta = svc.spreadsheets().get(spreadsheetId=config.SHEET_ID).execute()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        month_tabs = sorted(t for t in tabs if re.match(r"^\d{4}-\d{2}$", t))
        if not month_tabs:
            return None, [], []
        tab = month_tabs[-1]
        res = svc.spreadsheets().values().get(
            spreadsheetId=config.SHEET_ID, range=f"{tab}!A1:Q10000").execute()
        vals = res.get("values", [])
        if not vals:
            return tab, [], []
        return tab, vals[0], vals[1:][-max_rows:]
    except Exception as e:  # noqa: BLE001
        log.warning(f"Không đọc được kết quả từ Sheet: {e}")
        return None, [], []


def append_run_log(source: str, total: int, ok: int, errors: int, duration_s: float) -> None:
    """Ghi 1 dòng lịch sử chạy vào tab _logs."""
    try:
        svc = psi_checker.get_sheets_service()
        psi_checker.get_or_create_tab(svc, LOGS_TAB)
        res = svc.spreadsheets().values().get(
            spreadsheetId=config.SHEET_ID, range=f"{LOGS_TAB}!A1:F1").execute()
        if not res.get("values"):
            svc.spreadsheets().values().update(
                spreadsheetId=config.SHEET_ID, range=f"{LOGS_TAB}!A1",
                valueInputOption="RAW", body={"values": [LOG_HEADERS]}).execute()
        svc.spreadsheets().values().append(
            spreadsheetId=config.SHEET_ID, range=f"{LOGS_TAB}!A1",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": [[datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              source, total, ok, errors, duration_s]]}).execute()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Không ghi được run log lên Sheet: {e}")
