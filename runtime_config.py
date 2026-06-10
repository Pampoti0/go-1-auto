"""Config động lúc runtime — overlay lên config.py (env).

Lưu vào RUNTIME_CONFIG_FILE (mặc định runtime_config.json, gitignored).
Key nào chưa từng chỉnh thì dùng giá trị từ env/config.py.
"""

import json
import os
import re
import threading

import config as base

PATH = os.getenv("RUNTIME_CONFIG_FILE", "runtime_config.json")
_lock = threading.Lock()

# Callback đồng bộ ra ngoài (server gắn sheet_store.save_config vào đây)
on_change = None

VALID_STRATEGIES = {"mobile", "desktop"}
VALID_MODES = {"daily", "weekly", "monthly"}
VALID_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def _defaults() -> dict:
    return {
        "urls": list(base.URLS),
        "strategies": list(base.STRATEGIES),
        "schedule_mode": base.SCHEDULE_MODE,
        "schedule_time": base.SCHEDULE_TIME,
        "schedule_weekday": base.SCHEDULE_WEEKDAY,
        "schedule_day_of_month": base.SCHEDULE_DAY_OF_MONTH,
        "request_delay": base.REQUEST_DELAY,
    }


def current() -> dict:
    cfg = _defaults()
    try:
        with open(PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def validate(partial: dict) -> str | None:
    """Trả về thông báo lỗi, hoặc None nếu hợp lệ."""
    if "urls" in partial:
        if not isinstance(partial["urls"], list) or not partial["urls"]:
            return "urls phải là danh sách không rỗng."
        for u in partial["urls"]:
            if not isinstance(u, str) or not re.match(r"^https?://\S+$", u):
                return f"URL không hợp lệ: {u!r} (phải bắt đầu bằng http:// hoặc https://)"
    if "strategies" in partial:
        s = set(partial.get("strategies") or [])
        if not s or not s <= VALID_STRATEGIES:
            return "strategies chỉ nhận: mobile, desktop (ít nhất 1)."
    if "schedule_mode" in partial and partial["schedule_mode"] not in VALID_MODES:
        return "schedule_mode chỉ nhận: daily, weekly, monthly."
    if "schedule_time" in partial and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(partial["schedule_time"])):
        return "schedule_time phải dạng HH:MM (vd 08:00)."
    if "schedule_weekday" in partial and partial["schedule_weekday"] not in VALID_WEEKDAYS:
        return "schedule_weekday không hợp lệ (monday..sunday)."
    if "schedule_day_of_month" in partial:
        try:
            d = int(partial["schedule_day_of_month"])
        except (TypeError, ValueError):
            return "schedule_day_of_month phải là số 1–28."
        if not 1 <= d <= 28:
            return "schedule_day_of_month phải trong khoảng 1–28."
    if "request_delay" in partial:
        try:
            d = int(partial["request_delay"])
        except (TypeError, ValueError):
            return "request_delay phải là số giây >= 0."
        if d < 0:
            return "request_delay phải >= 0."
    return None


def update(partial: dict, notify: bool = True) -> dict:
    """Validate, merge và lưu. Trả về config mới. Raise ValueError nếu sai."""
    allowed = set(_defaults().keys())
    partial = {k: v for k, v in partial.items() if k in allowed}
    err = validate(partial)
    if err:
        raise ValueError(err)
    if "schedule_day_of_month" in partial:
        partial["schedule_day_of_month"] = int(partial["schedule_day_of_month"])
    if "request_delay" in partial:
        partial["request_delay"] = int(partial["request_delay"])
    if "urls" in partial:
        partial["urls"] = [u.strip() for u in partial["urls"]]
    with _lock:
        cfg = current()
        cfg.update(partial)
        stored = {k: v for k, v in cfg.items()}
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(stored, f, ensure_ascii=False, indent=2)
    if notify and on_change:
        try:
            on_change(cfg)
        except Exception:  # noqa: BLE001 — đồng bộ ngoài lỗi không được chặn việc lưu
            pass
    return cfg
