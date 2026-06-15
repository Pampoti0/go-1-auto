"""Microsoft Clarity insights cho DeCho — live UX behavior (read-only).

Credentials qua env (KHÔNG hardcode):
  CLARITY_PROJECT_ID   — id project Clarity (vd 't1skvuw82s')
  CLARITY_API_TOKEN    — Data Export token (Bearer)
  CLARITY_API_ENDPOINT — tùy chọn, mặc định project-live-insights
Mọi hàm *_safe nuốt lỗi: Clarity hỏng không được làm hỏng chat.
"""

import logging
import os
import time

log = logging.getLogger("clarity_agent")

CLARITY_PROJECT_ID = os.getenv("CLARITY_PROJECT_ID", "")
CLARITY_API_TOKEN = os.getenv("CLARITY_API_TOKEN", "")
CLARITY_API_ENDPOINT = os.getenv(
    "CLARITY_API_ENDPOINT",
    "https://www.clarity.ms/export-data/api/v1/project-live-insights",
)


def configured() -> bool:
    return bool(CLARITY_PROJECT_ID and CLARITY_API_TOKEN)


def heatmap_url() -> str:
    return f"https://clarity.microsoft.com/projects/view/{CLARITY_PROJECT_ID}/heatmaps" if CLARITY_PROJECT_ID else ""


def recordings_url() -> str:
    return f"https://clarity.microsoft.com/projects/view/{CLARITY_PROJECT_ID}/recordings" if CLARITY_PROJECT_ID else ""


def insights(num_days: int = 3) -> dict:
    """Live insights của project (session, engagement, scroll, rage/dead click, bot...).
    API live-insights chỉ hỗ trợ 1-3 ngày → kẹp lại cho an toàn."""
    import httpx

    if not configured():
        raise RuntimeError("Chưa cấu hình CLARITY_PROJECT_ID / CLARITY_API_TOKEN.")
    num_days = max(1, min(int(num_days or 3), 3))
    r = httpx.get(CLARITY_API_ENDPOINT,
                  params={"projectId": CLARITY_PROJECT_ID, "numDays": num_days},
                  headers={"Authorization": f"Bearer {CLARITY_API_TOKEN}"}, timeout=20)
    r.raise_for_status()
    return {"num_days": num_days, "data": r.json()}


# Clarity giới hạn ~10 lượt/ngày → cache 1 NGÀY + giữ bản tốt gần nhất + backoff khi lỗi.
_CACHE_TTL = float(os.getenv("CLARITY_CACHE_TTL", "86400"))   # 1 ngày
_FAIL_BACKOFF = 1800                                          # sau lỗi & chưa có data tốt: chờ 30' mới gọi lại
_cache = {"ts": 0.0, "data": None, "fail_ts": 0.0}


def insights_safe(num_days: int = 3) -> dict:
    """LUÔN lấy 3 ngày gần nhất (Clarity không hỗ trợ range tùy ý). Cache 1 ngày, giữ bản cũ khi lỗi.
    Trả kèm fetched_at (epoch giây) = thời điểm lấy data gần nhất."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return {**_cache["data"], "fetched_at": _cache["ts"]}   # còn hạn → dùng cache, KHÔNG gọi API
    if _cache["data"] is None and now - _cache["fail_ts"] < _FAIL_BACKOFF:
        return {"error": "Clarity tạm chưa gọi lại được (đang backoff để giữ quota). Thử lại sau ít phút."}
    try:
        out = insights(3)                                       # luôn 3 ngày
        _cache.update(ts=now, data=out)
        return {**out, "fetched_at": now}
    except Exception as e:  # noqa: BLE001
        _cache["fail_ts"] = now
        log.warning(f"Clarity insights lỗi: {type(e).__name__}: {e}")
        if _cache["data"] is not None:
            return {**_cache["data"], "stale": True, "fetched_at": _cache["ts"]}  # 429/lỗi → bản cũ
        if "429" in str(e) or "Too Many Requests" in str(e):
            return {"error": "Clarity giới hạn ~10 lượt/ngày (429). Đệ sẽ thử lại sau khi quota reset."}
        return {"error": f"{type(e).__name__}: {e}"}
