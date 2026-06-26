"""DeCho Agent — multi-agent web automation (Claw-a-thon 2026).

Module PageSpeed: check Core Web Vitals theo lịch/chat, ghi Google Sheet.
Module SEO: kéo GSC + GA4 theo tháng, so sánh tháng trước, ghi Google Sheet.

Endpoints chính:
  GET  /                 — web UI (chat + cấu hình + SEO)
  GET  /healthz          — health check (BTC dùng để chấm PASS)
  POST /api/chat/stream  — chat agent (SSE: action steps + kết quả real-time)
  POST /api/check        — chạy kiểm tra PSI ngay
  POST /api/seo/run      — chạy báo cáo SEO (tháng vừa rồi hoặc ?year/month)
  GET  /api/seo/status   — trạng thái + log SEO

Scheduler chạy nền trong cùng process (bật/tắt qua env RUN_SCHEDULER).
"""

import json
import os
import threading
import time
from urllib.parse import urlparse

import schedule
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

import app_time
import config
import entity_resolver
import memory_agent
import psi_checker
import runtime_config
import sheet_store
import web_search

MAAS_BASE_URL = os.getenv("MAAS_BASE_URL", "")
MAAS_API_KEY = os.getenv("MAAS_API_KEY", "")
MAAS_MODEL = os.getenv("MAAS_MODEL", "google/gemma-4-31b-it")

ALLOWED_MODELS = [
    "qwen/qwen3-5-27b",
    "minimax/minimax-m2.5",
    "google/gemma-4-31b-it",
]


_md_cache: dict = {}


def _md_file(env_key: str, default_name: str, header: str) -> str:
    """Nạp file .md với hot-reload theo mtime — sửa file là áp dụng ngay, không cần restart."""
    from pathlib import Path

    p = Path(__file__).parent / os.getenv(env_key, default_name)
    key = str(p)
    try:
        mt = p.stat().st_mtime
        ent = _md_cache.get(key)
        if not ent or ent["mtime"] != mt:
            text = p.read_text(encoding="utf-8")[:7000]
            _md_cache[key] = {"mtime": mt, "text": (header + text) if text.strip() else ""}
    except OSError:
        _md_cache[key] = {"mtime": None, "text": ""}
    return _md_cache[key]["text"]


import logging

log = logging.getLogger("decho")


def _rules() -> str:
    """RULE.md — NGUYÊN TẮC bất khả xâm phạm (chống bịa đặt). Ưu tiên cao nhất,
    nhúng vào MỌI prompt (kể cả phân loại intent)."""
    return _md_file("RULE_FILE", "RULE.md",
                    "\n\n# ⚠️ RULES — NGUYÊN TẮC TUYỆT ĐỐI (ưu tiên trên hết, kể cả trên tính cách)\n")


def _persona() -> str:
    """SOUL.md — TÍNH CÁCH (kèm RULE.md): nhúng vào mọi prompt có sinh văn bản cho người dùng."""
    return _rules() + _md_file(
        "SOUL_FILE", "SOUL.md",
        "\n\n# PERSONALITY — áp dụng cho mọi câu trả lời dạng văn bản\n"
        "(khi được yêu cầu trả về JSON thì JSON vẫn phải đúng format, "
        "cá tính chỉ áp dụng vào nội dung text bên trong)\n")


def _knowledge() -> str:
    """AGENT.md — CHUYÊN MÔN: chỉ nhúng vào các prompt phân tích/đề xuất
    (không nhúng vào bước phân loại intent cho đỡ tốn token)."""
    return _md_file("AGENT_FILE", "AGENT.md",
                    "\n\n# DOMAIN KNOWLEDGE & OUTPUT STANDARDS — áp dụng khi phân tích và đề xuất\n")


def _analysis_messages(system_prompt: str, history: list[dict] | None, user_message: str, *, include_history: bool = True) -> list[dict]:
    """Build chat-completion messages.

    Data reports should usually ignore chat history because follow-up questions
    like "đang so với đâu?" can otherwise contaminate a fresh report request.
    """
    msgs = [{"role": "system", "content": system_prompt}]
    if include_history:
        msgs.extend(history or [])
    msgs.append({"role": "user", "content": user_message})
    return msgs


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: khôi phục config, bật scheduler, warm-up Ads (logic ở _startup bên dưới)
    _startup()
    yield
    # (chỗ này để dọn dẹp khi shutdown nếu sau này cần)


app = FastAPI(title="DeCho Agent", lifespan=lifespan)

from pathlib import Path as _Path  # noqa: E402

from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/static", StaticFiles(directory=str(_Path(__file__).parent / "static")), name="static")

_state = {"running": False, "last_run": None, "last_result": None}
_lock = threading.Lock()

# ── SEO agent state ───────────────────────────────────────────────────────────
_seo_state = {"running": False, "last_run": None, "last_result": None, "log": []}
_seo_lock = threading.Lock()


class _SeoLogHandler(__import__("logging").Handler):
    def emit(self, record):
        _seo_state["log"].append(f"[{app_time.time_label()}] {record.getMessage()}")
        del _seo_state["log"][:-200]


__import__("logging").getLogger("seo_agent").addHandler(_SeoLogHandler())
__import__("logging").getLogger("seo_agent").setLevel(__import__("logging").INFO)


def _seo_exc_text(e: Exception) -> str:
    return str(e) if type(e).__name__ == "SeoAuthError" else f"{type(e).__name__}: {e}"


def _ads_exc_text(e: Exception) -> str:
    try:
        import ads_agent

        return ads_agent.ads_error_text(e)
    except Exception:  # noqa: BLE001
        return str(e) if type(e).__name__ == "AdsAuthError" else f"{type(e).__name__}: {e}"


def _seo_sheet_error(e: Exception) -> str:
    return f"❌ Không đọc được SEO Sheet: {_seo_exc_text(e)}"


def _run_seo_safe(year: int | None = None, month: int | None = None, url_contains: str | None = None, filter_label: str | None = None):
    with _seo_lock:
        if _seo_state["running"]:
            return
        _seo_state["running"] = True
    try:
        import seo_agent

        if year and month:
            result = seo_agent.run_for_month(year, month, url_contains or None)
        else:
            result = seo_agent.run()
        flt = f" · {filter_label or ('lọc URL chứa ' + repr(url_contains))}" if url_contains else ""
        _seo_state["last_result"] = f"success: {result['rows']} URL → tab {result['label']}{flt}"
    except Exception as e:  # noqa: BLE001
        msg = _seo_exc_text(e)
        _seo_state["last_result"] = f"error: {msg}"
        _seo_state["log"].append(f"[{app_time.time_label()}] ❌ {msg}")
    finally:
        _seo_state["running"] = False
        _seo_state["last_run"] = app_time.iso_now()
        _invalidate_cache()


# Đề xuất đang chờ xác nhận — tách theo user + session
_pending_by_session: dict = {}   # "user_id:session_id" -> {"range": {...}, "op": {...}}


def _pending_key(user_id: str | None, session_id: str | None) -> str:
    return f"{user_id or '_anon'}:{session_id or '_global'}"


def _pending_for(user_id: str | None, session_id: str | None) -> dict:
    return _pending_by_session.setdefault(_pending_key(user_id, session_id), {"range": {}, "op": {}})


def _run_seo_range_safe(start: str, end: str, url_contains: str | None = None, filter_label: str | None = None):
    with _seo_lock:
        if _seo_state["running"]:
            return
        _seo_state["running"] = True
    try:
        import seo_agent

        result = seo_agent.run_for_range(start, end, url_contains or None)
        t = result.get("totals", {})

        def _fmt(k):
            v = t.get(k)
            if not v:
                return f"{k}: —"
            pct = f" ({'+' if v['pct'] and v['pct'] > 0 else ''}{v['pct']}%)" if v["pct"] is not None else " (kỳ trước = 0)"
            return f"{k} **{v['cur']:,}**{pct}"
        summary = " · ".join(_fmt(k) for k in ("views", "users", "clicks", "impressions"))
        flt = f" · {filter_label or ('lọc URL chứa ' + repr(url_contains))}" if url_contains else ""
        top = result.get("top") or []
        top_txt = ""
        if top:
            top_txt = "\n🏆 Top URL theo views: " + "; ".join(
                f"{seo_agent.clean_url(r['url']).split('://')[-1]} ({r['views']:,} views)" for r in top[:5])
        _seo_state["last_result"] = (f"success: {result['rows']} URL → tab {result['label']} "
                                     f"({result['days']} ngày, so sánh {result['compare']}){flt}\n"
                                     f"📊 So với kỳ trước: {summary}{top_txt}\n"
                                     f"Chi tiết %_change từng URL nằm trong sheet (cột tô màu xanh/đỏ).")
    except Exception as e:  # noqa: BLE001
        msg = _seo_exc_text(e)
        _seo_state["last_result"] = f"error: {msg}"
        _seo_state["log"].append(f"[{app_time.time_label()}] ❌ {msg}")
    finally:
        _seo_state["running"] = False
        _seo_state["last_run"] = app_time.iso_now()
        _invalidate_cache()


def _run_check_safe(source: str = "schedule"):
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    try:
        saved = None
        for item in psi_checker.run_check_iter():
            if item["event"] == "saved":
                saved = item
        _state["last_result"] = "success"
        if saved:
            sheet_store.append_run_log(source, saved["total"], saved["ok"],
                                       saved["errors"], saved["duration"])
    except Exception as e:  # noqa: BLE001
        _state["last_result"] = f"error: {e}"
    finally:
        _state["running"] = False
        _state["last_run"] = app_time.iso_now()
        _invalidate_cache()


def _build_schedule():
    """Đăng ký job theo config động — gọi lại mỗi khi đổi lịch."""
    schedule.clear()
    cfg = runtime_config.current()
    mode, at = cfg["schedule_mode"], cfg["schedule_time"]
    if mode == "daily":
        schedule.every().day.at(at).do(_run_check_safe)
    elif mode == "weekly":
        getattr(schedule.every(), cfg["schedule_weekday"]).at(at).do(_run_check_safe)
    elif mode == "monthly":
        def monthly():
            if app_time.today().day == cfg["schedule_day_of_month"]:
                _run_check_safe()
        schedule.every().day.at(at).do(monthly)

    # SEO agent: chạy hàng tháng — ngày/giờ chỉnh được qua UI (runtime_config)
    def seo_monthly():
        if app_time.today().day == int(runtime_config.current().get("seo_run_day_of_month", 8)):
            _run_seo_safe()
    schedule.every().day.at(cfg.get("seo_run_time", "08:00")).do(seo_monthly)

def _scheduler_loop():
    _build_schedule()
    while True:
        schedule.run_pending()
        time.sleep(30)


def _startup():
    # 1) Khôi phục config đã lưu trên Sheet (sống qua container recreate)
    if config.SHEET_ID:
        saved = sheet_store.load_config()
        if saved:
            try:
                runtime_config.update(saved, notify=False)
            except ValueError:
                pass  # config trên Sheet hỏng thì dùng mặc định
    # 2) Từ giờ mỗi lần đổi config sẽ tự đồng bộ lên Sheet
    runtime_config.on_change = sheet_store.save_config
    # 3) Khởi động scheduler theo config (có thể vừa khôi phục)
    if os.getenv("RUN_SCHEDULER", "true").lower() == "true":
        threading.Thread(target=_scheduler_loop, daemon=True).start()

    # 4) Warm-up Google Ads client ở nền — request đầu tiên khỏi chờ init 2-3s
    def _ads_warmup():
        try:
            import ads_agent

            ads_agent.warmup()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_ads_warmup, daemon=True).start()


@app.get("/health")
@app.get("/healthz")
def healthz():
    cfg = runtime_config.current()
    return {
        "status": "ok",
        "configured": bool(config.PSI_API_KEY and config.SHEET_ID),
        "urls": len(cfg["urls"]),
        "schedule": f"{cfg['schedule_mode']} {cfg['schedule_time']}",
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{config.SHEET_ID}" if config.SHEET_ID else None,
    }


def _latest_psi_freshness() -> dict:
    if not (config.SHEET_ID and (config.SERVICE_ACCOUNT_FILE or config.SERVICE_ACCOUNT_JSON)):
        return {"configured": bool(config.SHEET_ID), "latest": "", "rows": 0, "error": ""}
    try:
        tab, _, rows = _cached("health:psi_latest", 120, lambda: sheet_store.read_results(1))
        return {"configured": True, "latest": tab or "", "rows": len(rows or []), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "latest": "", "rows": 0, "error": _friendly_error_text(e)}


def _latest_seo_freshness() -> dict:
    try:
        tabs = _cached("health:seo_tabs", 300, _seo_list_tabs)
        return {"configured": True, "latest": (tabs or [""])[-1], "tabs": len(tabs or []), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "latest": "", "tabs": 0, "error": _friendly_error_text(e)}


def _clarity_freshness() -> dict:
    from datetime import datetime

    import clarity_agent

    out = {"configured": clarity_agent.configured(), "cache": "", "latest": "", "error": ""}
    try:
        if clarity_agent._CACHE_FILE.exists():  # noqa: SLF001
            payload = json.loads(clarity_agent._CACHE_FILE.read_text(encoding="utf-8"))  # noqa: SLF001
            ts = float(payload.get("ts") or 0)
            out.update(cache="disk", latest=datetime.fromtimestamp(ts, app_time.APP_TZ).isoformat(timespec="seconds"))
    except Exception as e:  # noqa: BLE001
        out["error"] = _friendly_error_text(e)
    return out


def _service_status(name: str, ok: bool, *, configured: bool = True, message: str = "", action: str = "", freshness: dict | None = None) -> dict:
    if not configured:
        level = "missing"
    elif ok:
        level = "ok"
    else:
        level = "error"
    return {
        "name": name,
        "status": level,
        "configured": configured,
        "message": message,
        "action": action,
        "freshness": freshness or {},
    }


def _system_health_report() -> dict:
    import ads_agent
    import clarity_agent
    import seo_agent

    cfg = runtime_config.current()
    psi_fresh = _latest_psi_freshness()
    seo_auth = _seo_auth_health()
    seo_fresh = _latest_seo_freshness() if seo_auth.get("auth_configured") else {"configured": False, "latest": "", "tabs": 0}
    clarity_fresh = _clarity_freshness()
    services = [
        _service_status(
            "PageSpeed Insights",
            bool(config.PSI_API_KEY and config.SHEET_ID and not psi_fresh.get("error")),
            configured=bool(config.PSI_API_KEY and config.SHEET_ID),
            message=psi_fresh.get("error") or f"{len(cfg.get('urls') or [])} URL · {cfg.get('schedule_mode')} {cfg.get('schedule_time')}",
            action="Thêm PSI_API_KEY, SHEET_ID và service account nếu thiếu.",
            freshness=psi_fresh,
        ),
        _service_status(
            "Search Console / GA4 / SEO Sheet",
            bool(seo_auth.get("auth_usable") and not seo_fresh.get("error")),
            configured=bool(seo_auth.get("auth_configured")),
            message=seo_auth.get("auth_error") or seo_fresh.get("error") or f"Auth {seo_auth.get('auth_mode') or 'service account'} · latest {seo_fresh.get('latest') or '—'}",
            action="Thêm service account vào Search Console site và GA4 property nếu 403.",
            freshness={**seo_fresh, "auth_mode": seo_auth.get("auth_mode"), "auth_source": seo_auth.get("auth_source"), "site": seo_agent.SITE_URL, "ga4_property": seo_agent.GA4_PROPERTY_ID},
        ),
        _service_status(
            "Google Ads",
            ads_agent.configured(),
            configured=ads_agent.configured(),
            message="Configured" if ads_agent.configured() else "Thiếu GOOGLE_ADS_* env.",
            action="Auth lại Google Ads nếu invalid_grant; kiểm tra customer id/developer token nếu 403.",
            freshness={"cache": _cache_meta_for("ads:camps")},
        ),
        _service_status(
            "Microsoft Clarity",
            clarity_agent.configured() and not clarity_fresh.get("error"),
            configured=clarity_agent.configured(),
            message=clarity_fresh.get("error") or ("Configured" if clarity_agent.configured() else "Thiếu CLARITY_PROJECT_ID / CLARITY_API_TOKEN."),
            action="Dùng cache/backoff để tránh hết quota; set CLARITY_CACHE_FILE để sống qua restart.",
            freshness=clarity_fresh,
        ),
        _service_status(
            "AgentBase Memory",
            memory_agent.configured(),
            configured=memory_agent.configured(),
            message="Configured" if memory_agent.configured() else "Thiếu MEMORY_ID hoặc memory env.",
            action="Nếu không dùng memory dài hạn có thể bỏ qua.",
            freshness={},
        ),
        _service_status(
            "Report Cache",
            True,
            configured=True,
            message=f"Memory entries {len(_api_cache)} · disk cache {'on' if _API_DISK_CACHE else 'off'}",
            action="API_DISK_CACHE_DIR=.cache/api; xóa cache khi cần refresh toàn bộ.",
            freshness={"entries": len(_api_cache), "disk": str(_API_CACHE_DIR), "disk_enabled": _API_DISK_CACHE},
        ),
    ]
    blocking = [s for s in services if s["status"] == "error"]
    missing = [s for s in services if s["status"] == "missing" and s["name"] in {"PageSpeed Insights", "Search Console / GA4 / SEO Sheet"}]
    overall = "bad" if blocking else ("warn" if missing or any(s["status"] == "missing" for s in services) else "ok")
    return {
        "overall": overall,
        "generated_at": app_time.iso_now(),
        "timezone": app_time.APP_TZ_NAME,
        "services": services,
        "cache": {
            "memory": [_cache_meta_for(k) for k in sorted(_api_cache_meta)[:80]],
            "disk_enabled": _API_DISK_CACHE,
            "disk_dir": str(_API_CACHE_DIR),
        },
        "config": {
            "urls": len(cfg.get("urls") or []),
            "schedule": f"{cfg.get('schedule_mode')} {cfg.get('schedule_time')}",
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{config.SHEET_ID}" if config.SHEET_ID else "",
        },
    }


@app.get("/api/system-health")
def api_system_health():
    try:
        return _system_health_report()
    except Exception as e:  # noqa: BLE001
        return {"overall": "error", "error": _friendly_error_text(e), "services": []}


@app.get("/api/config")
def get_config():
    return runtime_config.current()


# ── Cache đọc Sheet (tránh 429: quota 60 reads/phút) ──────────────────────────
_api_cache: dict = {}
_api_cache_meta: dict = {}
_api_cache_refreshing: set[str] = set()
_api_cache_lock = threading.Lock()
_API_CACHE_DIR = _Path(os.getenv("API_DISK_CACHE_DIR", ".cache/api"))
_API_DISK_CACHE = os.getenv("API_DISK_CACHE", "true").lower() != "false"
_API_STALE_CACHE_TTL = int(os.getenv("API_STALE_CACHE_TTL", "21600"))
_INSIGHT_CACHE_VERSION = "v2"


def _insight_cache_key(key: str) -> str:
    return f"insight:{_INSIGHT_CACHE_VERSION}:{key}"


def _cache_disk_path(key: str):
    import re

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", key)[:160] or "cache"
    return _API_CACHE_DIR / f"{safe}.json"


def _cache_read_disk(key: str, ttl: int, now: float, *, allow_stale: bool = False, stale_ttl: int | None = None):
    if not _API_DISK_CACHE:
        return None
    try:
        p = _cache_disk_path(key)
        if not p.exists():
            return None
        payload = json.loads(p.read_text(encoding="utf-8"))
        ts = float(payload.get("ts") or 0)
        age = now - ts
        if age < ttl:
            return ts, payload.get("value"), "disk"
        if allow_stale and age < int(stale_ttl or _API_STALE_CACHE_TTL):
            return ts, payload.get("value"), "stale-disk"
        if now - ts >= ttl:
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("API disk cache đọc lỗi (bỏ qua): %s: %s", type(e).__name__, e)
        return None


def _cache_write_disk(key: str, val):
    if not _API_DISK_CACHE:
        return
    try:
        _API_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_disk_path(key)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps({"key": key, "ts": time.time(), "value": val}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        log.warning("API disk cache ghi lỗi (bỏ qua): %s: %s", type(e).__name__, e)


def _refresh_cache_async(key: str, ttl: int, fn):
    with _api_cache_lock:
        if key in _api_cache_refreshing:
            return
        _api_cache_refreshing.add(key)

    def run():
        try:
            val = fn()
            _cache_put(key, ttl, val)
        except Exception as e:  # noqa: BLE001
            log.warning("API stale cache refresh lỗi (bỏ qua): %s: %s", type(e).__name__, e)
        finally:
            with _api_cache_lock:
                _api_cache_refreshing.discard(key)

    threading.Thread(target=run, daemon=True).start()


def _cached(key: str, ttl: int, fn, *, allow_stale: bool = False, stale_ttl: int | None = None, refresh_stale: bool = True):
    now = time.time()
    ent = _api_cache.get(key)
    if ent and now - ent[0] < ttl:
        _api_cache_meta[key] = {"key": key, "ts": ent[0], "ttl": ttl, "source": "memory"}
        return ent[1]
    disk = _cache_read_disk(key, ttl, now, allow_stale=allow_stale, stale_ttl=stale_ttl)
    if disk is not None:
        ts, val, source = disk
        _api_cache[key] = (ts, val)
        _api_cache_meta[key] = {"key": key, "ts": ts, "ttl": ttl, "source": source}
        if source == "stale-disk" and refresh_stale:
            _refresh_cache_async(key, ttl, fn)
        return val
    val = fn()
    # không cache kết quả lỗi
    if not (isinstance(val, dict) and val.get("error")):
        _api_cache[key] = (now, val)
        _api_cache_meta[key] = {"key": key, "ts": now, "ttl": ttl, "source": "api"}
        _cache_write_disk(key, val)
    return val


def _cache_put(key: str, ttl: int, val):
    """Warm RAM/disk cache for derived reports built from the same source data."""
    if isinstance(val, dict) and val.get("error"):
        return val
    now = time.time()
    _api_cache[key] = (now, val)
    _api_cache_meta[key] = {"key": key, "ts": now, "ttl": ttl, "source": "api"}
    _cache_write_disk(key, val)
    return val


def _invalidate_cache():
    _api_cache.clear()
    _api_cache_meta.clear()


def _cache_meta_for(key: str) -> dict:
    from datetime import datetime

    meta = dict(_api_cache_meta.get(key) or {})
    if meta.get("ts"):
        meta["fetched_at"] = datetime.fromtimestamp(float(meta["ts"]), app_time.APP_TZ).isoformat(timespec="seconds")
        meta["age_seconds"] = max(0, int(time.time() - float(meta["ts"])))
        meta["fresh"] = meta["age_seconds"] < int(meta.get("ttl") or 0)
    return meta


def _report_meta(source: str, *, window: str | None = None, cache_key: str | None = None, note: str | None = None) -> dict:
    meta = {
        "source": source,
        "window": window or "",
        "generated_at": app_time.iso_now(),
        "timezone": app_time.APP_TZ_NAME,
    }
    if cache_key:
        meta["cache"] = _cache_meta_for(cache_key)
    if note:
        meta["note"] = note
    return meta


def _with_meta(data: dict, source: str, *, window: str | None = None, cache_key: str | None = None, note: str | None = None) -> dict:
    out = dict(data or {})
    out["_meta"] = _report_meta(source, window=window, cache_key=cache_key, note=note)
    return out


def _friendly_error_text(err) -> str:
    raw = str(err or "")
    low = raw.lower()
    if "invalid_grant" in low or "expired or revoked" in low:
        return "Token Google đã hết hạn/bị revoke. Nếu còn refresh token hợp lệ thì DeCho sẽ tự refresh; nếu Google revoke refresh token thì cần auth lại."
    if "403" in low or "sufficient permission" in low or "permission" in low:
        return "Thiếu quyền truy cập. Kiểm tra service account/user đã được thêm vào Search Console/GA4/Google Ads đúng property/customer chưa."
    if "429" in low or "quota" in low or "rate limit" in low:
        return "Đang bị quota/rate limit. DeCho sẽ ưu tiên cache/backoff để tránh đốt limit thêm."
    if "timeout" in low or "connecttimeout" in low or "timed out" in low:
        return "Kết nối timeout. Có thể mạng/API chậm; thử lại sau hoặc dùng dữ liệu cache nếu có."
    if "not configured" in low or "chưa cấu hình" in low:
        return "Thiếu cấu hình env hoặc credential cho nguồn dữ liệu này."
    return raw[:500]


@app.get("/api/results")
def api_results(month: str | None = None):
    """Dữ liệu PSI Sheet cho dashboard (cache 60s)."""
    try:
        return _cached(f"psi:{month}", 60, lambda: sheet_store.read_results_data(month))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "tabs": [], "tab": None, "headers": [], "rows": []}


@app.get("/api/seo/results")
def api_seo_results(month: str | None = None):
    """Dữ liệu SEO Sheet cho UI (cache 120s)."""
    def fetch():
        tab, headers, rows = _seo_read_results(month, 2000)
        return {"tab": tab, "tabs": _seo_list_tabs(), "headers": headers, "rows": rows}
    try:
        return _cached(f"seo:{month}", 120, fetch)
    except Exception as e:  # noqa: BLE001
        return {"error": _seo_exc_text(e), "tab": None, "tabs": [], "headers": [], "rows": []}


@app.get("/api/seo/summary")
def api_seo_summary(limit: int = 6):
    """Tổng views/users/clicks/impressions theo từng tháng (cache 10 phút)."""
    def fetch():
        return _seo_summary_fetch(limit)
    try:
        return _cached(f"seosum:{limit}", 600, fetch)
    except Exception as e:  # noqa: BLE001
        return {"error": _seo_exc_text(e), "months": []}


def _seo_summary_fetch(limit: int = 6):
    """Đọc N tháng bằng MỘT lệnh batchGet thay vì 2 lệnh/tháng — nhanh hơn ~5 lần."""
    try:
        import seo_agent
        from googleapiclient.discovery import build

        tabs = _seo_list_tabs()[-max(1, min(limit, 12)):]
        if not tabs:
            return {"months": []}
        svc = build("sheets", "v4", credentials=seo_agent.get_creds(), cache_discovery=False)
        res = svc.spreadsheets().values().batchGet(
            spreadsheetId=seo_agent.SEO_SHEET_ID,
            ranges=[f"{t}!A1:K2000" for t in tabs]).execute()
        out = []
        for t, vr in zip(tabs, res.get("valueRanges", [])):
            vals = vr.get("values", [])
            if len(vals) < 2:
                out.append({"month": t})
                continue
            h, rows = vals[0], vals[1:]
            idx = {k: i for i, k in enumerate(h)}

            def num(r, c):
                try:
                    return float(r[idx[c]])
                except (KeyError, IndexError, ValueError):
                    return 0.0
            out.append({"month": t, **{c: int(sum(num(r, c) for r in rows))
                                       for c in ("views", "users", "clicks", "impressions") if c in idx}})
        return {"months": out}
    except Exception as e:  # noqa: BLE001
        return {"error": _seo_exc_text(e), "months": []}


@app.get("/api/ads/campaigns")
def api_ads_campaigns():
    """Danh sách campaign Google Ads (cache 5 phút)."""
    import ads_agent

    if not ads_agent.configured():
        return {"error": "Chưa cấu hình Google Ads (GOOGLE_ADS_* trong env).", "campaigns": []}
    try:
        out = _cached("ads:camps", 300, lambda: {"campaigns": ads_agent.list_campaigns()})
        entity_resolver.register_campaigns(out.get("campaigns") or [])
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": _ads_exc_text(e), "campaigns": []}


@app.get("/api/ads/perf")
def api_ads_perf(days: int = 7, start: str | None = None, end: str | None = None):
    """Hiệu suất campaign: N ngày gần nhất, hoặc khoảng start/end YYYY-MM-DD (cache 5 phút)."""
    import re

    import ads_agent

    if not ads_agent.configured():
        return {"error": "Chưa cấu hình Google Ads (GOOGLE_ADS_* trong env).", "rows": []}
    if not (start and end and re.match(r"^\d{4}-\d{2}-\d{2}$", start) and re.match(r"^\d{4}-\d{2}-\d{2}$", end)):
        start = end = None
    try:
        key = f"ads:perf:{start}:{end}" if start else f"ads:perf:{days}"
        out = _cached(key, 300, lambda: ads_agent.campaign_perf(days, start, end))
        entity_resolver.register_campaigns(out.get("rows") or [])
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": _ads_exc_text(e), "rows": []}


@app.get("/api/ads/ldp")
def api_ads_ldp(days: int = 7, start: str | None = None, end: str | None = None):
    """Hiệu suất theo landing page (cache 5 phút)."""
    import re

    import ads_agent

    if not ads_agent.configured():
        return {"error": "Chưa cấu hình Google Ads (GOOGLE_ADS_* trong env).", "rows": []}
    if not (start and end and re.match(r"^\d{4}-\d{2}-\d{2}$", start) and re.match(r"^\d{4}-\d{2}-\d{2}$", end)):
        start = end = None
    try:
        key = f"ads:ldp:{start}:{end}" if start else f"ads:ldp:{days}"
        return _cached(key, 300, lambda: ads_agent.landing_page_perf(days, start, end))
    except Exception as e:  # noqa: BLE001
        return {"error": _ads_exc_text(e), "rows": []}


@app.get("/api/clarity")
def api_clarity():
    """Microsoft Clarity — LUÔN 3 ngày gần nhất; clarity_agent tự cache 1 ngày."""
    import clarity_agent

    if not clarity_agent.configured():
        return {"configured": False}
    out = clarity_agent.insights_safe()
    return {"configured": True, "heatmap": clarity_agent.heatmap_url(),
            "recordings": clarity_agent.recordings_url(), **out}


@app.get("/api/opportunities")
def api_opportunities(limit: int = 20):
    """Opportunity Score: PSI + SEO + Ads + Clarity, kèm evidence/confidence."""
    try:
        limit = max(1, min(int(limit or 20), 50))
        key = _insight_cache_key(f"opportunities:{limit}")
        out = _cached(key, 300, lambda: _opportunity_report(limit), allow_stale=True)
        return _with_meta(out, "PSI Sheet + SEO Sheet + Google Ads + Clarity", window=f"latest · limit {limit}", cache_key=key)
    except Exception as e:  # noqa: BLE001
        return {"error": _friendly_error_text(e), "opportunities": []}


@app.get("/api/alerts")
def api_alerts(limit: int = 50):
    """Alert monitor hợp nhất: PSI, SEO, Ads, Clarity."""
    try:
        limit = max(1, min(int(limit or 50), 100))
        key = _insight_cache_key(f"alerts:{limit}")
        out = _cached(key, 300, lambda: _alert_report(limit), allow_stale=True)
        return _with_meta(out, "PSI Sheet + SEO Sheet + Google Ads + Clarity", window=f"latest · limit {limit}", cache_key=key)
    except Exception as e:  # noqa: BLE001
        return {"error": _friendly_error_text(e), "alerts": []}


@app.get("/api/entities")
def api_entities():
    """Self-learning entity catalog used for URL/campaign filters."""
    return {"entities": entity_resolver.catalog(runtime_config.current().get("urls") or [])}


@app.get("/api/tracking-audit")
def api_tracking_audit(days: int = 30):
    """Conversion tracking audit from Ads campaign + landing page metrics."""
    try:
        days = max(1, min(int(days or 30), 90))
        key = _insight_cache_key(f"tracking_audit:{days}")
        out = _cached(key, 300, lambda: _conversion_tracking_report(days), allow_stale=True)
        return _with_meta(out, "Google Ads campaign + landing page metrics", window=f"{days} days", cache_key=key)
    except Exception as e:  # noqa: BLE001
        return {"error": _friendly_error_text(e), "issues": []}


@app.get("/api/root-cause")
def api_root_cause(limit: int = 12):
    """Root-cause signals across SEO, PSI, Ads, Clarity and tracking."""
    try:
        limit = max(1, min(int(limit or 12), 30))
        key = _insight_cache_key(f"root_cause:{limit}")
        out = _cached(key, 300, lambda: _root_cause_report(limit), allow_stale=True)
        return _with_meta(out, "Opportunity + Tracking Audit", window=f"latest · limit {limit}", cache_key=key)
    except Exception as e:  # noqa: BLE001
        return {"error": _friendly_error_text(e), "hypotheses": []}


@app.get("/api/experiments")
def api_experiments(limit: int = 8):
    """Suggested measurement plans for top opportunities."""
    try:
        limit = max(1, min(int(limit or 8), 20))
        key = _insight_cache_key(f"experiments:{limit}")
        out = _cached(key, 300, lambda: _experiment_report(limit), allow_stale=True)
        return _with_meta(out, "Opportunity + Tracking Audit", window=f"latest · limit {limit}", cache_key=key)
    except Exception as e:  # noqa: BLE001
        return {"error": _friendly_error_text(e), "experiments": []}


def _weekly_autopilot_report() -> dict:
    # Build the heavy cross-source snapshot once, then derive the other reports
    # from it. This keeps the first Insights load from repeating Ads/Sheet reads
    # for Alerts, Root Cause and Experiments.
    opportunity_base = _cached(_insight_cache_key("opportunities:50"), 300, lambda: _opportunity_report(50))
    opportunities = _cache_put(_insight_cache_key("opportunities:20"), 300, _slice_opportunity_report(opportunity_base, 20))
    _cache_put(_insight_cache_key("opportunities:30"), 300, _slice_opportunity_report(opportunity_base, 30))
    tracking = _cached(_insight_cache_key("tracking_audit:30"), 300, lambda: _conversion_tracking_report(30))
    alerts = _cache_put(_insight_cache_key("alerts:50"), 300, _alert_report_from_opportunity(opportunity_base, 50))
    root = _cache_put(_insight_cache_key("root_cause:12"), 300, _root_cause_report_from_inputs(opportunity_base, tracking, 12))
    _cache_put(_insight_cache_key("root_cause:20"), 300, _root_cause_report_from_inputs(opportunity_base, tracking, 20))
    experiments = _cache_put(_insight_cache_key("experiments:8"), 300, _experiment_report_from_inputs(opportunity_base, tracking, 8))
    _cache_put(_insight_cache_key("experiments:12"), 300, _experiment_report_from_inputs(opportunity_base, tracking, 12))

    actions: list[dict] = []
    for a in (alerts.get("alerts") or [])[:8]:
        actions.append({
            "priority": "P0" if a.get("lv") == "high" else "P1",
            "title": a.get("text") or "Alert cần xử lý",
            "why": "; ".join((a.get("evidence") or [])[:2]),
            "confidence": a.get("confidence") or "medium",
            "source": a.get("source") or "Alerts",
            "target": a.get("path") or a.get("name") or "",
        })
    for o in (opportunities.get("opportunities") or [])[:8]:
        actions.append({
            "priority": "P1" if _num_value(o.get("score")) >= 70 else "P2",
            "title": f"Tối ưu {o.get('path')} (score {o.get('score')})",
            "why": "; ".join((o.get("evidence") or [])[:3]),
            "confidence": o.get("confidence") or "medium",
            "source": ",".join(o.get("sources") or []),
            "target": o.get("path") or "",
        })
    for i in (tracking.get("issues") or [])[:5]:
        actions.append({
            "priority": "P0" if i.get("lv") == "high" else "P1",
            "title": i.get("text") or "Kiểm tra tracking",
            "why": "; ".join((i.get("evidence") or [])[:2]),
            "confidence": i.get("confidence") or "medium",
            "source": "Tracking",
            "target": i.get("path") or i.get("name") or i.get("scope") or "",
        })

    rank = {"P0": 0, "P1": 1, "P2": 2}
    deduped, seen = [], set()
    for item in sorted(actions, key=lambda x: (rank.get(x.get("priority"), 9), x.get("target") or "", x.get("title") or "")):
        key = (item.get("target"), item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return {
        "generated_at": app_time.iso_now(),
        "timezone": app_time.APP_TZ_NAME,
        "summary": {
            "alerts": len(alerts.get("alerts") or []),
            "high_alerts": len([a for a in (alerts.get("alerts") or []) if a.get("lv") == "high"]),
            "opportunities": len(opportunities.get("opportunities") or []),
            "tracking_health": tracking.get("health"),
            "root_hypotheses": len(root.get("hypotheses") or []),
            "experiments": len(experiments.get("experiments") or []),
        },
        "next_actions": deduped[:10],
        "top_opportunities": (opportunities.get("opportunities") or [])[:8],
        "top_alerts": (alerts.get("alerts") or [])[:8],
        "tracking_issues": (tracking.get("issues") or [])[:8],
        "root_causes": (root.get("hypotheses") or [])[:8],
        "experiments": (experiments.get("experiments") or [])[:8],
        "_meta": _report_meta(
            "Alerts + Opportunity + Tracking + Root Cause + Experiments",
            window="weekly planning snapshot",
            cache_key=_insight_cache_key("weekly_autopilot"),
            note="Read-only planning report; does not create tasks or change campaigns.",
        ),
    }


def _weekly_autopilot_text(report: dict, max_items: int = 8) -> str:
    if report.get("error"):
        return f"Không lập được Weekly Autopilot: {report['error']}"
    summary = report.get("summary") or {}
    lines = [
        "**Weekly Autopilot**",
        f"- Alerts: **{summary.get('alerts', 0)}** ({summary.get('high_alerts', 0)} high)",
        f"- Opportunities: **{summary.get('opportunities', 0)}** · Root hypotheses: **{summary.get('root_hypotheses', 0)}** · Tracking: **{summary.get('tracking_health') or '—'}**",
        "",
        "**Top việc tuần này:**",
    ]
    actions = report.get("next_actions") or []
    if not actions:
        lines.append("- Chưa có action đủ rõ. Nên refresh PSI/SEO/Ads hoặc kiểm tra cấu hình nguồn dữ liệu.")
        return "\n".join(lines)
    for idx, a in enumerate(actions[:max_items], 1):
        lines.append(
            f"{idx}. **{a.get('priority')} — {a.get('title')}**\n"
            f"   Evidence: {a.get('why') or '—'}\n"
            f"   Source: {a.get('source') or '—'} · Confidence: {a.get('confidence') or 'medium'}"
        )
    if len(actions) > max_items:
        lines.append(f"\nCòn {len(actions) - max_items} việc ít ưu tiên hơn trong tab Insights.")
    lines.append("\nGợi ý vận hành: xử lý P0 trước, sau đó chọn 1 experiment có success metric rõ để đo 7-14 ngày.")
    return "\n".join(lines)


@app.get("/api/weekly-autopilot")
def api_weekly_autopilot():
    try:
        key = _insight_cache_key("weekly_autopilot")
        out = _cached(key, 300, _weekly_autopilot_report, allow_stale=True)
        return _with_meta(out, "Alerts + Opportunity + Tracking + Root Cause + Experiments", window="weekly planning snapshot", cache_key=key)
    except Exception as e:  # noqa: BLE001
        return {"error": _friendly_error_text(e), "next_actions": []}


_jobs: dict = {}
_jobs_lock = threading.Lock()


class JobRequest(BaseModel):
    kind: str = "refresh_insights"
    params: dict | None = None


def _job_result_summary(result) -> dict:
    if not isinstance(result, dict):
        return {}
    counts = {}
    for key in ("next_actions", "opportunities", "alerts", "issues", "hypotheses", "experiments", "root_causes", "tracking_issues"):
        val = result.get(key)
        if isinstance(val, list):
            counts[key] = len(val)
    summary = {
        "overall": result.get("overall"),
        "health": result.get("health"),
        "summary": result.get("summary") if isinstance(result.get("summary"), dict) else None,
        "counts": counts,
        "error": result.get("error"),
    }
    return {k: v for k, v in summary.items() if v not in (None, {}, "")}


def _job_public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in {"result", "result_raw"}}


def _run_job(job_id: str, kind: str, params: dict):
    with _jobs_lock:
        _jobs[job_id].update(status="running", started_at=app_time.iso_now(), message="Đang chạy...")
    try:
        if kind == "weekly_autopilot":
            result = _weekly_autopilot_report()
        elif kind == "tracking_audit":
            result = _conversion_tracking_report(int((params or {}).get("days") or 30))
        elif kind == "root_cause":
            result = _root_cause_report(int((params or {}).get("limit") or 12))
        elif kind == "experiments":
            result = _experiment_report(int((params or {}).get("limit") or 8))
        elif kind == "opportunities":
            result = _opportunity_report(int((params or {}).get("limit") or 20))
        elif kind == "alerts":
            result = _alert_report(int((params or {}).get("limit") or 50))
        else:
            _invalidate_cache()
            result = {
                "system_health": _system_health_report(),
                "weekly": _weekly_autopilot_report(),
            }
        with _jobs_lock:
            _jobs[job_id].update(
                status="done",
                finished_at=app_time.iso_now(),
                message="Xong",
                result_summary=_job_result_summary(result),
                result_raw=result,
            )
    except Exception as e:  # noqa: BLE001
        with _jobs_lock:
            _jobs[job_id].update(status="error", finished_at=app_time.iso_now(), message=_friendly_error_text(e), error=_friendly_error_text(e))


@app.post("/api/jobs")
def api_start_job(req: JobRequest):
    import uuid

    kind = (req.kind or "refresh_insights").strip()
    params = req.params or {}
    allowed = {"refresh_insights", "weekly_autopilot", "tracking_audit", "root_cause", "experiments", "opportunities", "alerts"}
    if kind not in allowed:
        return {"error": f"Unsupported job kind: {kind}", "allowed": sorted(allowed)}
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "kind": kind, "params": params, "status": "queued", "created_at": app_time.iso_now(), "message": "Đang xếp hàng..."}
    with _jobs_lock:
        _jobs[job_id] = job
    threading.Thread(target=_run_job, args=(job_id, kind, params), daemon=True).start()
    return _job_public(job)


@app.get("/api/jobs")
def api_jobs():
    with _jobs_lock:
        vals = [_job_public(v) for v in _jobs.values()]
    vals.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"jobs": vals[:30]}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return {"error": "Job không tồn tại."}
    return _job_public(job)


class CreateCampaignRequest(BaseModel):
    password: str
    name: str | None = None
    budget: int | None = None
    final_url: str | None = None


def _campaign_budget_bounds() -> tuple[int, int]:
    def env_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    return env_int("CAMPAIGN_MIN_BUDGET_VND", 50_000), env_int("CAMPAIGN_MAX_BUDGET_VND", 500_000_000)


def _campaign_allowed_hosts() -> list[str]:
    raw = os.getenv("CAMPAIGN_ALLOWED_HOSTS", "greennode.ai,www.greennode.ai,register.vngcloud.vn")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _validate_campaign_request(req: CreateCampaignRequest) -> tuple[int, str | None, str | None]:
    try:
        budget = 100_000 if req.budget is None else int(req.budget)
    except (TypeError, ValueError):
        return 0, None, "Ngân sách phải là số nguyên VND."
    min_budget, max_budget = _campaign_budget_bounds()
    if not min_budget <= budget <= max_budget:
        return 0, None, f"Ngân sách phải trong khoảng {min_budget:,}–{max_budget:,} VND.".replace(",", ".")

    final_url = (req.final_url or "").strip() or None
    if final_url:
        parsed = urlparse(final_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return 0, None, "Final URL phải là URL http/https hợp lệ."
        host = (parsed.hostname or "").lower()
        allowed = _campaign_allowed_hosts()
        if allowed and not any(host == h or host.endswith("." + h) for h in allowed):
            return 0, None, "Final URL không thuộc domain được phép tạo campaign."
    return budget, final_url, None


@app.post("/api/ads/create-campaign")
def api_ads_create_campaign(req: CreateCampaignRequest):
    """Tạo campaign (PAUSED) — nhận mật khẩu qua FORM riêng (không qua chat), kiểm CAMPAIGN_CREATE_PASSWORD."""
    pw = os.getenv("CAMPAIGN_CREATE_PASSWORD", "")
    if not pw:
        return {"ok": False, "error": "Quyền tạo campaign chưa bật (thiếu CAMPAIGN_CREATE_PASSWORD trong .env)."}
    if (req.password or "") != pw:
        return {"ok": False, "error": "Sai mật khẩu."}
    import ads_agent

    if not ads_agent.configured():
        return {"ok": False, "error": "Chưa cấu hình Google Ads (GOOGLE_ADS_*)."}
    budget, final_url, validation_error = _validate_campaign_request(req)
    if validation_error:
        return {"ok": False, "error": validation_error}
    try:
        return ads_agent.create_campaign(req.name or None, budget, final_url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _ads_exc_text(e)}


def _ads_prompt(perf: dict) -> str:
    rows = perf.get("rows", [])
    lines = [f"{r['date']} | {r['name']} ({r['status']}) | impr={r['impressions']} | clicks={r['clicks']} | "
             f"CTR={r['ctr']}% | cost={r['cost']} | conv={r['conversions']} | CPA={r['cpa'] or 'N/A'}"
             for r in rows[-200:]]
    return (
        f"Bạn là DeCho — module Paid Campaigns (AI). Dữ liệu Google Ads từ {perf.get('start')} đến {perf.get('end')} "
        "(mỗi dòng = 1 campaign × 1 ngày; cost tính theo đơn vị tiền tài khoản):\n\n"
        + "\n".join(lines) +
        "\n\nPhân tích theo câu hỏi: tổng chi tiêu, CTR/CPA bất thường, campaign nào hiệu quả/kém, đề xuất. "
        "Mọi khuyến nghị phải kèm Evidence (số liệu/campaign/ngày) và Confidence (high/medium/low). "
        "TIẾNG VIỆT, ngắn gọn, số liệu cụ thể, **đậm** + gạch đầu dòng. KHÔNG dùng LaTeX. Trả lời trực tiếp. /no_think"
    ) + _knowledge() + _persona()


def _ldp_prompt(perf: dict) -> str:
    rows = perf.get("rows", [])
    lines = [f"{r['base_url']} | impr={r['impressions']} | clicks={r['clicks']} | CTR={r['ctr']}% | "
             f"CPC={r['avg_cpc']} | cost={r['cost']} | conv={r['conversions']} | "
             f"bounce={r['bounce_rate'] if r['bounce_rate'] is not None else 'N/A'}% | "
             f"speed={r['speed_score'] if r['speed_score'] is not None else 'N/A'}/100"
             for r in rows[:60]]
    cl = (f"\nLink Clarity để soi hành vi: heatmap {perf['clarity_heatmap']} · recordings {perf['clarity_recordings']}"
          if perf.get("clarity_heatmap") else "")
    return (
        f"Bạn là DeCho — module Paid Campaigns (AI). Hiệu suất theo LANDING PAGE từ {perf.get('start')} đến {perf.get('end')} "
        "(cost theo đơn vị tiền tài khoản; speed_score 0-100 càng cao càng nhanh):\n\n"
        + "\n".join(lines) + cl +
        "\n\nPhân tích: landing page nào hút click/chi phí nhưng convert kém hoặc bounce cao/speed thấp (rò rỉ ngân sách); "
        "trang nào nên tối ưu trước & cách làm (tốc độ, mobile, nội dung). Nếu có link Clarity thì nhắc Đại ca xem heatmap/recordings. "
        "Mọi khuyến nghị phải kèm Evidence (landing page + metric) và Confidence (high/medium/low). "
        "TIẾNG VIỆT, súc tích, **đậm** + gạch đầu dòng, KHÔNG LaTeX. /no_think"
    ) + _knowledge() + _persona()


def _clarity_prompt(ins: dict, filter_desc: str = "") -> str:
    import json as _json

    flt = (f"\nBỘ LỌC NGƯỜI DÙNG YÊU CẦU: {filter_desc}. "
           "Nếu dữ liệu Clarity có URL/page matching thì chỉ tập trung phần đó; nếu dữ liệu chỉ là project-level, nói rõ giới hạn này và phân tích tổng quan.\n"
           if filter_desc else "")
    return (
        "Bạn là DeCho — phân tích UX từ Microsoft Clarity (live insights):\n\n"
        + flt
        + _json.dumps(ins.get("data", ins), ensure_ascii=False)[:6000] +
        "\n\nDựa DUY NHẤT trên số liệu trên: nêu chỉ số đáng chú ý (sessions, engagement, scroll depth, rage click, "
        "dead click, quick back, bot). Chỉ dấu hiệu UX có vấn đề (rage/dead click cao = chỗ bấm hỏng; scroll thấp = nội dung "
        "không giữ chân) và gợi ý kiểm tra/cải thiện. Mọi khuyến nghị phải kèm Evidence và Confidence. "
        "TIẾNG VIỆT, súc tích, **đậm** + gạch đầu dòng, KHÔNG LaTeX, KHÔNG bịa số. /no_think"
    ) + _persona()


def _combined_prompt(ads: dict, ldp: dict, clarity: dict) -> str:
    import json as _json

    def _short(o):
        return _json.dumps(o, ensure_ascii=False)[:2500]

    return (
        "Bạn là DeCho — phân tích TỔNG HỢP user journey: Google Ads (chi tiêu/click) → Landing Page → hành vi Clarity.\n\n"
        f"ADS (campaign×ngày):\n{_short(ads.get('rows', ads))}\n\n"
        f"LANDING PAGE:\n{_short(ldp.get('rows', ldp))}\n\n"
        f"CLARITY (UX):\n{_short(clarity.get('data', clarity))}\n\n"
        "Nối các tầng: tiền đổ vào campaign nào → ra landing page nào → user hành xử ra sao (bounce, rage/dead click, scroll). "
        "Chẩn đoán vì sao traffic trả tiền nhưng KHÔNG convert + đề xuất 3-5 việc ưu tiên (bám số liệu thật, không bịa). "
        "Mỗi việc phải kèm Evidence (nguồn số liệu cụ thể) và Confidence (high/medium/low). "
        "TIẾNG VIỆT, súc tích, **đậm** + gạch đầu dòng, KHÔNG LaTeX. /no_think"
    ) + _knowledge() + _persona()


@app.get("/api/llm-test")
def llm_test():
    """Chẩn đoán kết nối MaaS: trả về lỗi thật thay vì fallback âm thầm."""
    if not MAAS_API_KEY:
        return {"ok": False, "step": "env", "error": "MAAS_API_KEY chưa được set (kiểm tra .env + restart server)."}
    if not MAAS_BASE_URL:
        return {"ok": False, "step": "env", "error": "MAAS_BASE_URL chưa được set."}
    import httpx

    result = {"base_url": MAAS_BASE_URL, "model": MAAS_MODEL}
    try:  # 1) thử list models
        r = httpx.get(f"{MAAS_BASE_URL}/models",
                      headers={"Authorization": f"Bearer {MAAS_API_KEY}"}, timeout=20)
        result["models_status"] = r.status_code
        if r.status_code == 200:
            try:
                result["available_models"] = [m.get("id") for m in r.json().get("data", [])][:20]
            except Exception:  # noqa: BLE001
                result["models_raw"] = r.text[:300]
        else:
            result["models_error"] = r.text[:300]
    except Exception as e:  # noqa: BLE001
        result["models_error"] = f"{type(e).__name__}: {e}"
    try:  # 2) thử chat completion
        r = httpx.post(f"{MAAS_BASE_URL}/chat/completions",
                       json={"model": MAAS_MODEL, "max_tokens": 10,
                             "messages": [{"role": "user", "content": "ping"}]},
                       headers={"Authorization": f"Bearer {MAAS_API_KEY}"}, timeout=30)
        result["chat_status"] = r.status_code
        if r.status_code == 200:
            result["ok"] = True
            result["reply"] = r.json()["choices"][0]["message"]["content"]
        else:
            result["ok"] = False
            result["chat_error"] = r.text[:300]
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["chat_error"] = f"{type(e).__name__}: {e}"
    return result


class ConfigUpdate(BaseModel):
    urls: list[str] | None = None
    strategies: list[str] | None = None
    schedule_mode: str | None = None
    schedule_time: str | None = None
    schedule_weekday: str | None = None
    schedule_day_of_month: int | None = None
    request_delay: int | None = None
    seo_run_day_of_month: int | None = None
    seo_run_time: str | None = None
    seo_tracked_urls: list[str] | None = None
    password: str | None = None


_CONFIG_MUTATION_ACTIONS = {"add_url", "remove_url", "set_schedule"}


def _config_password_error(password: str | None) -> str | None:
    pw_env = os.getenv("CONFIG_EDIT_PASSWORD", "")
    if not pw_env:
        return "Quyền đổi cấu hình chưa bật (thiếu CONFIG_EDIT_PASSWORD trong .env)."
    if (password or "") != pw_env:
        return "Sai mật khẩu — không đổi được cấu hình."
    return None


def _config_locked_text() -> str:
    return "🔒 Đổi cấu hình cần mật khẩu. Đại ca dùng popup/form cấu hình để xác nhận, Đệ không đổi trực tiếp qua endpoint chat cũ."


@app.put("/api/config")
def put_config(body: ConfigUpdate):
    err = _config_password_error(body.password)
    if err:
        return {"ok": False, "error": err}
    partial = {k: v for k, v in body.model_dump().items() if v is not None and k != "password"}
    if not partial:
        return {"ok": False, "error": "Không có trường nào để cập nhật."}
    try:
        cfg = runtime_config.update(partial)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if any(k.startswith("schedule") or k.startswith("seo_run") for k in partial):
        _build_schedule()  # áp lịch mới ngay
    return {"ok": True, "config": cfg}


class ConfigApplyRequest(BaseModel):
    password: str
    action: str
    url: str | None = None
    schedule_mode: str | None = None
    schedule_time: str | None = None
    schedule_day_of_month: int | None = None
    schedule_weekday: str | None = None


@app.post("/api/config/apply")
def api_config_apply(req: ConfigApplyRequest):
    """Áp 1 thay đổi config (add/remove URL, đổi lịch) — yêu cầu CONFIG_EDIT_PASSWORD (nhập qua popup, không qua chat)."""
    err = _config_password_error(req.password)
    if err:
        return {"ok": False, "error": err}
    if req.action not in ("add_url", "remove_url", "set_schedule"):
        return {"ok": False, "error": "Thao tác không hợp lệ."}
    data = {k: v for k, v in req.model_dump().items() if v is not None and k != "password"}
    try:
        return {"ok": True, "text": _execute_action(data, allow_config_mutation=True)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/status")
def status():
    return _state


@app.get("/api/logs", response_class=HTMLResponse)
def logs(lines: int = 200):
    """Xem nhanh log của agent (kể cả các lần scheduler tự chạy) ngay trên browser."""
    from pathlib import Path

    p = Path(__file__).parent / "psi_checker.log"
    if not p.exists():
        content = "(chưa có log — agent chưa chạy lần nào)"
    else:
        content = "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-min(lines, 2000):])
    return HTMLResponse(
        f"<pre style='font:12px/1.6 monospace;padding:16px;white-space:pre-wrap'>{content}</pre>"
    )


@app.post("/api/check")
def trigger_check():
    if not (config.PSI_API_KEY and config.SHEET_ID):
        return {"started": False, "reason": "Thiếu PSI_API_KEY hoặc SHEET_ID trong env."}
    if _state["running"]:
        return {"started": False, "reason": "Đang có lần chạy khác."}
    threading.Thread(target=_run_check_safe, kwargs={"source": "api"}, daemon=True).start()
    return {"started": True, "urls": len(config.URLS), "strategies": config.STRATEGIES,
            "note": "Chạy nền vài phút. Xem tiến độ tại /api/status, kết quả trong Google Sheet."}


class ChatRequest(BaseModel):
    message: str


def _do_trigger() -> str:
    if not (config.PSI_API_KEY and config.SHEET_ID):
        return "Chưa cấu hình PSI_API_KEY / SHEET_ID nên mình chưa chạy được."
    if _state["running"]:
        return "Đang có một lần kiểm tra chạy rồi — chờ xong đã nhé. Xem tiến độ ở /api/status."
    cfg = runtime_config.current()
    threading.Thread(target=_run_check_safe, kwargs={"source": "chat"}, daemon=True).start()
    return (f"✅ Đã bắt đầu kiểm tra {len(cfg['urls'])} URL × {len(cfg['strategies'])} strategy. "
            f"Chạy nền vài phút, kết quả ghi vào Google Sheet (tab {app_time.now().strftime('%Y-%m')}).")


def _list_urls_text() -> str:
    urls = runtime_config.current()["urls"]
    listing = "\n".join(f"{i+1}. {u}" for i, u in enumerate(urls))
    return f"Đang theo dõi {len(urls)} URL:\n{listing}"


def _add_url(url: str) -> str:
    cfg = runtime_config.current()
    if url in cfg["urls"]:
        return f"URL đã có trong danh sách rồi: {url}"
    try:
        runtime_config.update({"urls": cfg["urls"] + [url]})
    except ValueError as e:
        return f"❌ {e}"
    return f"✅ Đã thêm {url} — danh sách hiện có {len(cfg['urls']) + 1} URL."


def _remove_url(url: str) -> str:
    cfg = runtime_config.current()
    matches = [u for u in cfg["urls"] if u == url or url in u]
    if not matches:
        return f"Không tìm thấy URL khớp với '{url}' trong danh sách."
    if len(matches) > 1:
        return "Khớp nhiều URL, nói cụ thể hơn nhé:\n" + "\n".join(f"- {u}" for u in matches)
    remaining = [u for u in cfg["urls"] if u != matches[0]]
    if not remaining:
        return "Không xóa được — danh sách phải còn ít nhất 1 URL."
    runtime_config.update({"urls": remaining})
    return f"✅ Đã xóa {matches[0]} — còn {len(remaining)} URL."


def _status_text() -> str:
    if _state["running"]:
        return "🔄 Đang chạy kiểm tra... Kết quả sẽ ghi vào Google Sheet khi xong."
    if _state["last_run"]:
        return f"Lần chạy gần nhất (phiên này): {_state['last_run']} — {_state['last_result']}."
    # Chưa chạy trong phiên này → soi Sheet xem đã có data lịch sử chưa (sống sót qua restart)
    try:
        tab, _, rows = sheet_store.read_results(1)
        if tab and rows:
            return (f"Phiên server này chưa tự chạy, nhưng Sheet đã có dữ liệu (tab {tab}). "
                    "Hỏi 'phân tích PageSpeed' để xem điểm, hoặc 'chạy kiểm tra' để đo mới.")
    except Exception:  # noqa: BLE001
        pass
    return "Chưa có lần chạy nào. Nói 'chạy kiểm tra' để bắt đầu."


def _system_prompt() -> str:
    cfg = runtime_config.current()
    return (
        "Bạn là PageSpeed Checker Agent (luôn khai báo là AI). Bạn quản lý việc kiểm tra "
        f"Core Web Vitals cho {len(cfg['urls'])} URL, lịch {cfg['schedule_mode']} lúc {cfg['schedule_time']}, "
        f"kết quả ghi vào Google Sheet. Trạng thái hiện tại: {_status_text()}\n"
        "Trả về DUY NHẤT một JSON theo intent của người dùng:\n"
        '- Chạy kiểm tra ngay: {"action":"run_check"}\n'
        '- Hỏi trạng thái: {"action":"status"}\n'
        '- Xem danh sách URL: {"action":"list_urls"}\n'
        '- Hỏi về KẾT QUẢ đã đo (điểm số, trang nhanh/chậm, phân tích, so sánh): {"action":"query_results"}\n'
        '- Thêm URL: {"action":"add_url","url":"<url>"}\n'
        '- Xóa URL: {"action":"remove_url","url":"<url hoặc từ khóa>"}\n'
        '- Đổi lịch: {"action":"set_schedule","schedule_mode":"daily|weekly|monthly","schedule_time":"HH:MM","schedule_day_of_month":<1-28 nếu monthly>,"schedule_weekday":"<thứ nếu weekly>"} (chỉ kèm field người dùng nêu)\n'
        '- Còn lại: {"action":"reply","text":"<trả lời ngắn gọn>"}'
        + _persona()
    )


_KNOWN_ACTIONS = {
    "run_check", "query_results", "list_urls", "add_url", "remove_url", "set_schedule",
    "run_report", "seo_range", "confirm", "seo_query", "list_months", "ads_list", "ads_perf",
    "ldp_perf", "clarity", "combined", "create_campaign",
    "status", "help", "web_search", "web_fetch", "priority_fix", "action_plan",
    "diagnose_drop", "fix_suggest", "tracking_audit", "experiment_plan",
    "weekly_autopilot", "alerts", "remember", "reply",
}
_TOOL_ALIAS = {"search": "web_search", "websearch": "web_search", "fetch": "web_fetch",
               "read_url": "web_fetch", "readurl": "web_fetch", "open_url": "web_fetch", "browse": "web_fetch"}


def _extract_tool_action(text: str) -> dict | None:
    """Một số model (Gemma/minimax) trả khối <tool_code> hoặc tool => 'x' thay vì JSON thuần.
    Bóc tên tool + query/url ra thành action dict để Đệ vẫn thực thi đúng (thay vì in raw ra màn hình)."""
    import re
    if not text:
        return None
    m = re.search(r"""(?:tool|action|name)["']?\s*(?:=>|:|=)\s*["']?([a-zA-Z_]+)["']?""", text)
    if not m:
        # Không có key tool/action nhưng có JSON trần {"url":...}/{"query":...} (model giả tool-call) → route thật
        mj = re.search(r'\{\s*"url"\s*:\s*"(https?://[^"]+)"', text)
        if mj:
            return {"action": "web_fetch", "url": mj.group(1)}
        mj = re.search(r'\{\s*"query"\s*:\s*"([^"]+)"', text)
        if mj:
            return {"action": "web_search", "query": mj.group(1)}
        return None
    tool = _TOOL_ALIAS.get(m.group(1).strip().lower(), m.group(1).strip().lower())
    if tool not in _KNOWN_ACTIONS:
        return None
    out = {"action": tool}

    def _grab(*names):
        tags = "|".join(names)
        mt = re.search(rf"<(?:{tags})>(.*?)</(?:{tags})>", text, re.S | re.I)
        if mt:
            return mt.group(1).strip()
        mk = re.search(rf"""(?:{tags})["']?\s*(?:=>|:|=)\s*["']([^"']+)["']""", text, re.I)
        return mk.group(1).strip() if mk else ""

    if tool == "web_search":
        out["query"] = _grab("query", "q")
    elif tool == "remember":
        out["fact"] = _grab("fact", "query", "text")
    elif tool in ("web_fetch", "add_url", "remove_url"):
        mu = re.search(r"https?://[^\s'\"<>)]+", text)
        if mu:
            out["url"] = mu.group(0)
    return out


def _validate_action(data: dict) -> dict:
    """Chặn action thiếu field bắt buộc → đổi sang hỏi lại, tránh thực thi sai (Đệ hết 'hứa suông')."""
    a = data.get("action")
    if a == "add_url" and not str(data.get("url") or "").strip():
        return {"action": "reply", "text": "Đại ca cho Đệ URL cụ thể để thêm nha (vd: https://...)."}
    if a == "remove_url" and not str(data.get("url") or "").strip():
        return {"action": "reply", "text": "Đại ca muốn xóa URL nào? Cho Đệ URL hoặc từ khóa của nó nha."}
    if a == "set_schedule" and not any(str(data.get(k) or "").strip()
                                       for k in ("schedule_mode", "schedule_time", "schedule_day_of_month", "schedule_weekday")):
        return {"action": "reply", "text": "Đổi lịch thế nào hả Đại ca? Nói rõ giúp Đệ: daily/weekly/monthly + giờ (vd 'daily 8h sáng')."}
    if a == "remember" and not str(data.get("fact") or "").strip():
        return {"action": "remember", "fact": ""}  # để handler tự lấy nguyên câu của user
    return data


def _describe_op(data: dict) -> str:
    """Mô tả thao tác config để hỏi xác nhận."""
    a = data.get("action")
    if a == "remove_url":
        return f"**xóa URL** khớp '{data.get('url')}' khỏi danh sách theo dõi?"
    if a == "set_schedule":
        bits = [f"{k.replace('schedule_', '')}={data[k]}" for k in
                ("schedule_mode", "schedule_time", "schedule_day_of_month", "schedule_weekday") if data.get(k)]
        return "**đổi lịch PageSpeed** (" + ", ".join(bits) + ")?"
    return "thực hiện thao tác này?"


_EXTERNAL_CUES = ("mới nhất", "tin tức", "đối thủ", "competitor", "xu hướng thị trường",
                  "benchmark", "bảng giá", "ra mắt", "vừa ra", "cập nhật mới", "thị trường hiện",
                  "trên thị trường", "tình hình ngành", "best practice mới")
_INTERNAL_CUES = ("pagespeed", "lcp", "cls", "score", "điểm", "seo", "traffic", "clicks",
                  "impression", "campaign", "ads", "quảng cáo", "url", "lịch", "sheet",
                  "báo cáo", "theo dõi", "decho", "đệ ", "năng lực", "làm được gì")


def _looks_external(msg: str) -> bool:
    """Câu hỏi RÕ RÀNG cần thông tin ngoài hệ thống/cập nhật → nên web_search (lưới an toàn khi model bỏ sót)."""
    m = (msg or "").lower()
    if any(k in m for k in _INTERNAL_CUES):
        return False
    return any(k in m for k in _EXTERNAL_CUES)


_HELP_CUES = ("làm được gì", "làm được những gì", "hướng dẫn cách dùng", "hướng dẫn sử dụng",
              "hướng dẫn dùng", "hướng dẫn nhanh", "cách dùng", "dùng thế nào", "dùng sao",
              "dùng như thế nào", "có tính năng", "tính năng gì", "có những gì", "decho làm")


def _looks_help(msg: str) -> bool:
    """Hỏi về năng lực / cách dùng → luôn ra help (model hay deflect 'đã trả lời rồi')."""
    m = (msg or "").lower()
    return any(k in m for k in _HELP_CUES)


_CONFIRM_CUES = ("ok", "oke", "okê", "okay", "okie", "k", "đồng ý", "dong y", "chạy đi", "chay di",
                 "chạy luôn", "chạy", "chay", "chốt", "chot", "duyệt", "duyet", "làm đi", "lam di",
                 "ừ", "uh", "ừm", "yes", "yep", "có", "co", "đúng", "dung", "đúng rồi", "xác nhận", "xac nhan", "go")


def _looks_confirm(msg: str) -> bool:
    """Câu đồng ý NGẮN (ok/oke/chạy đi/đồng ý...) — chỉ coi là xác nhận khi đang có đề xuất chờ."""
    m = (msg or "").strip().lower().rstrip("!.~, ")
    if not m or len(m) > 16:
        return False
    return m in _CONFIRM_CUES or any(m == c or m.startswith(c + " ") for c in _CONFIRM_CUES)


def _looks_comparison_period_question(msg: str) -> bool:
    """Follow-up hỏi mốc/baseline của số so sánh, không phải yêu cầu phân tích mới."""
    text = _filter_norm(msg)
    text = (
        text.replace("so zoi", "so voi")
        .replace("so vs", "so voi")
        .replace("chaneg", "change")
    )
    words = text.split()
    business_not_followup = (
        "baseline experiment",
        "baseline success metric",
        "change request",
        "change log",
        "changelog",
        "period pricing",
        "moc launch",
        "kpi muc tieu",
        "doi thu",
        "product va tutorial",
        "product voi tutorial",
        "psi voi seo",
        "pagespeed voi seo",
        "gia thi truong",
    )
    if any(p in text for p in business_not_followup):
        return False
    has_period_word = any(p in text for p in ("thang", "ky", "period", "khoang", "moc"))
    short_period_only = (
        len(words) <= 4
        and any(p in text for p in ("ky nao", "ki nao", "moc nao", "thang nao", "thang may", "period nao"))
    )
    if short_period_only:
        return True
    compare = (
        any(p in text for p in ("so sanh", "compare", "doi chieu", "so voi", "baseline", "change", "tang giam"))
        or "truoc" in text
    )
    period = (
        any(p in text for p in ("thang may", "thang nao", "ky nao", "period nao", "khoang nao", "moc nao"))
        or ("so voi" in text and has_period_word)
        or ("baseline" in text and any(p in text for p in ("month", "thang", "ky", "period", "moc")))
        or ("change" in text and has_period_word)
        or ("change" in text and any(p in text for p in ("la sao", "sao a", "sao vay", "tinh tu dau", "so voi gi", "so voi dau", "moc nao", "ky nao")))
        or ("tang giam" in text and has_period_word)
        or any(p in text for p in (
            "so voi dau", "so voi cai gi", "so voi cai nao",
            "so voi gi",
            "doi chieu voi dau", "tinh tu dau", "lay moc",
            "moc so sanh", "moc doi chieu", "ky truoc cu the",
            "ky truoc la khi nao", "thang truoc la khi nao",
            "dot truoc cu the",
        ))
    )
    return compare and period


def _prev_month_label(month: str) -> str | None:
    import re
    from datetime import date

    from dateutil.relativedelta import relativedelta

    mt = re.match(r"^(20\d{2})-(\d{2})$", str(month or "").strip())
    if not mt:
        return None
    cur = date(int(mt.group(1)), int(mt.group(2)), 1)
    prev = cur - relativedelta(months=1)
    return f"{prev.year}-{prev.month:02d}"


def _latest_month_from_history(history: list[dict] | None) -> str | None:
    import re

    items = list(history or [])
    preferred_patterns = (
        r"(?i)report\s*(?:[—\-:]\s*)?(?:tháng|thang)?\s*(20\d{2}-\d{2})",
        r"(?i)seo\s+(?:tháng|thang)\s*(20\d{2}-\d{2})",
        r"(?i)seo\s+sheet\s+tab\s*(20\d{2}-\d{2})",
        r"(?i)(?:đọc|doc)\s+\d+\s+(?:dòng|dong)\s+(?:từ|tu)\s+seo\s+sheet\s+tab\s*(20\d{2}-\d{2})",
    )
    for item in reversed(items):
        content = str(item.get("content") or "")
        if _looks_comparison_period_answer(content):
            continue
        preferred = [m for pat in preferred_patterns for m in re.findall(pat, content)]
        if preferred:
            return max(preferred)
    all_months = []
    for item in items:
        content = str(item.get("content") or "")
        if _looks_comparison_period_answer(content):
            continue
        norm = _filter_norm(content)
        if any(k in norm for k in ("seo", "gsc", "ga4", "traffic", "clicks", "impressions", "views", "users")):
            all_months.extend(re.findall(r"\b20\d{2}-\d{2}\b", content))
    if all_months:
        return max(all_months)
    return None


def _looks_comparison_period_answer(text: str) -> bool:
    norm = _filter_norm(text)
    return (
        ("thang lien truoc" in norm and ("so voi" in norm or "dang so voi" in norm or "so sanh" in norm))
        or "khong co baseline co dinh" in norm
        or "khong dung mot moc so sanh co dinh" in norm
        or ("cac lan do psi" in norm and "moc so sanh co dinh" in norm)
    )


def _looks_metric_relationship_answer(text: str) -> bool:
    norm = _filter_norm(text)
    return (
        "khong phai cung mot loai so lieu" in norm
        or "cach decho noi hai phan nay" in norm
        or "vua cham vua co traffic" in norm
    )


def _history_looks_psi_only(history: list[dict] | None) -> bool:
    text = _filter_norm("\n".join(str(m.get("content") or "") for m in (history or [])[-6:]))
    has_psi = any(k in text for k in ("pagespeed", "page speed", "psi", "core web", "web vitals", "lcp", "cls", "tbt", "fcp", "inp", "performance score"))
    has_seo = any(k in text for k in ("seo", "gsc", "ga4", "traffic", "clicks", "impressions", "views", "users"))
    return has_psi and not has_seo


def _latest_data_context(history: list[dict] | None) -> str | None:
    """Classify the latest analytics answer as psi/seo.

    PageSpeed reports can mention "SEO blog" or a tab month, so the newest
    context type has to win before we infer month-over-month SEO.
    """
    for item in reversed(list(history or [])):
        content = str(item.get("content") or "")
        if not content or _looks_comparison_period_answer(content) or _looks_metric_relationship_answer(content):
            continue
        norm = _filter_norm(content)
        psi = any(k in norm for k in (
            "pagespeed", "page speed", "psi sheet", "core web", "web vitals",
            "performance score", "fcp", "lcp", "cls", "tbt", "inp", "ttfb",
            "mobile score", "desktop score",
        ))
        seo = any(k in norm for k in (
            "seo sheet", "gsc", "ga4", "search console", "organic", "traffic",
            "clicks", "impressions", "views", "users", "ctr", "position",
            "clicks_change", "views_change", "change_",
        ))
        if psi and (not seo or any(k in norm for k in ("pagespeed report", "bao cao pagespeed", "psi sheet", "phan tich ket qua pagespeed", "core web", "lcp", "cls", "tbt"))):
            return "psi"
        if seo:
            return "seo"
    return None


def _psi_comparison_reply() -> str:
    return (
        "Với báo cáo PageSpeed vừa rồi, DeCho **không dùng một mốc so sánh cố định**. "
        "Nó đang đọc các lần đo PSI có trong tab hiện tại rồi nhận xét theo chuỗi đo: "
        "điểm mới nhất, điểm thấp/cao, xu hướng giữa các lần đo, và các URL đang kém.\n\n"
        "Nói gọn: nếu thấy câu 'giảm/tăng/phục hồi' trong PageSpeed, đó là so giữa các lần đo PSI đang có."
    )


_SESSION_DATA_CONTEXT: dict[str, dict] = {}


def _data_context_key(user_id: str | None, session_id: str | None) -> str | None:
    if not session_id:
        return None
    return f"{user_id or '_anon'}:{session_id}"


def _remember_data_context(user_id: str | None, session_id: str | None, kind: str, month: str | None = None):
    key = _data_context_key(user_id, session_id)
    if not key or kind not in {"seo", "psi"}:
        return
    _SESSION_DATA_CONTEXT[key] = {"kind": kind, "month": month or ""}


def _session_data_context(user_id: str | None, session_id: str | None) -> dict | None:
    key = _data_context_key(user_id, session_id)
    if not key:
        return None
    ctx = _SESSION_DATA_CONTEXT.get(key)
    return dict(ctx) if ctx else None


def _seo_comparison_reply(month: str | None, source: str) -> str:
    prev = _prev_month_label(month or "")
    if not (month and prev):
        return (
            "Với báo cáo SEO, mặc định DeCho so sánh **tháng đang phân tích** với "
            "**tháng liền trước**. Câu vừa rồi chưa có đủ context tháng cụ thể nên Đệ không đoán bừa."
        )
    return (
        f"Với báo cáo SEO trong context hiện tại: **SEO tháng {month}** đang so với **{prev}** "
        "(tháng liền trước). "
        f"Đệ xác định từ {source}.\n\n"
        "Lưu ý: PageSpeed chỉ là các lần đo trong tab/tháng đang xem; phần so sánh tăng/giảm theo tháng là dữ liệu SEO "
        "(các cột *_change_% trong SEO Sheet)."
    )


def _comparison_period_reply(history: list[dict] | None = None, user_id: str | None = None, session_id: str | None = None) -> str:
    ctx = _session_data_context(user_id, session_id)
    if ctx and ctx.get("kind") == "psi":
        return _psi_comparison_reply()
    if ctx and ctx.get("kind") == "seo":
        return _seo_comparison_reply(ctx.get("month"), "phiên chat hiện tại")

    if _latest_data_context(history) == "psi":
        return _psi_comparison_reply()
    month = _latest_month_from_history(history)
    source = "context chat"
    if not month:
        if _history_looks_psi_only(history):
            return _psi_comparison_reply()
        source = "SEO Sheet"
        try:
            tabs = _seo_list_tabs()
            month = tabs[-1] if tabs else None
        except Exception as e:  # noqa: BLE001
            return (
                "Với báo cáo SEO, mặc định DeCho so sánh **tháng đang phân tích** với "
                "**tháng liền trước**.\n\n"
                f"Hiện Đệ chưa đọc được danh sách tab SEO để nói chính xác tháng nào: {_friendly_error_text(e)}"
            )
    return _seo_comparison_reply(month, source)


_INTENT_OVERRIDE_ACTIONS = {
    "ads_list", "ads_perf",
    "action_plan", "priority_fix", "diagnose_drop", "fix_suggest", "tracking_audit", "experiment_plan", "weekly_autopilot", "alerts",
}


def _hard_intent_override(msg: str, current_action: str) -> dict | None:
    """High-confidence keyword intent that should beat occasional LLM misroutes.

    Keep this small: these phrases are operationally distinct, and routing them
    to SEO report/query can trigger expensive or blocked work.
    """
    if current_action == "confirm":
        return None
    if _looks_comparison_period_question(msg):
        return None
    kw = _all_keyword_intent(msg)
    if kw and kw.get("action") == "query_results" and current_action in ("run_check", "run_report", "seo_query", "seo_range"):
        return kw
    if kw and kw.get("action") in ("seo_query", "query_results") and current_action == "reply":
        return kw
    if kw and kw.get("action") in _INTENT_OVERRIDE_ACTIONS and kw.get("action") != current_action:
        return kw
    return None


def _keyword_param_patch(msg: str, data: dict, current_action: str) -> tuple[dict, bool]:
    """Let deterministic keyword parsing fix LLM's default time window for the same action."""
    kw = _all_keyword_intent(msg)
    if not kw or kw.get("action") != current_action:
        return data, False
    out = dict(data)
    changed = False
    for key in ("start", "end", "days", "month", "months", "year"):
        val = kw.get(key)
        if val in (None, ""):
            continue
        if out.get(key) != val:
            out[key] = val
            changed = True
    if kw.get("start") and kw.get("end") and "days" in out:
        out.pop("days", None)
        changed = True
    return out, changed


def _proactive_suffix() -> str:
    """Bắt mọi phân tích kết thúc bằng cảnh báo + việc nên làm tiếp — BÁM SỐ LIỆU THẬT, không bịa."""
    return ("\n\nQUAN TRỌNG — sau phần phân tích, KẾT THÚC bằng (chỉ dựa trên số liệu thật ở trên, tuyệt đối không bịa):\n"
            "⚠️ **Cảnh báo**: 1-2 bất thường đáng lưu ý (sụt mạnh, điểm thấp, chi phí tăng…) — KHÔNG có thì bỏ hẳn phần này.\n"
            "👉 **Nên làm tiếp**: 1-3 việc ưu tiên cao, cụ thể & actionable, bám đúng dữ liệu vừa phân tích. "
            "Mỗi việc nên có Evidence: <số liệu/URL/campaign> và Confidence: high/medium/low.")


def _execute_action(data: dict, *, allow_config_mutation: bool = False) -> str:
    action = data.get("action")
    if action == "run_check":
        return _do_trigger()
    if action == "status":
        return _status_text()
    if action == "list_urls":
        return _list_urls_text()
    if action in _CONFIG_MUTATION_ACTIONS and not allow_config_mutation:
        return _config_locked_text()
    if action == "add_url" and data.get("url"):
        return _add_url(data["url"].strip())
    if action == "remove_url" and data.get("url"):
        return _remove_url(data["url"].strip())
    if action == "set_schedule":
        partial = {k: v for k, v in data.items()
                   if k in ("schedule_mode", "schedule_time", "schedule_day_of_month", "schedule_weekday") and v}
        try:
            cfg = runtime_config.update(partial)
        except ValueError as e:
            return f"❌ {e}"
        _build_schedule()
        return (f"✅ Đã đổi lịch: {cfg['schedule_mode']} lúc {cfg['schedule_time']}"
                + (f", ngày {cfg['schedule_day_of_month']} hàng tháng" if cfg["schedule_mode"] == "monthly" else "")
                + (f", {cfg['schedule_weekday']}" if cfg["schedule_mode"] == "weekly" else ""))
    return data.get("text") or _status_text()


def _strip_think(text: str) -> str:
    import re

    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _capability_gap_reply(message: str) -> str | None:
    """Deterministic guard for unsupported delivery/notification features.

    The LLM tends to be overly helpful here ("send me a webhook and I'll set it
    up"). In this codebase there is no Slack/Discord/webhook sender yet, so keep
    the answer grounded and explicit.
    """
    import unicodedata

    raw = (message or "").lower()
    norm = unicodedata.normalize("NFD", raw)
    norm = "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")
    destinations = ("slack", "discord", "webhook", "teams", "telegram", "zalo", "email", "mail")
    send_cues = ("gui", "send", "notify", "notification", "thong bao", "tu dong gui", "auto gui", "day vao", "push")
    report_cues = ("bao cao", "report", "alert", "canh bao", "weekly", "hang tuan", "hang ngay")
    if not any(d in norm for d in destinations):
        return None
    if not (any(c in norm for c in send_cues) or any(c in norm for c in report_cues)):
        return None
    return (
        "Chưa có tính năng tự gửi báo cáo/alert ra Slack, Discord, email hay webhook trong bản hiện tại.\n\n"
        "Hiện DeCho đọc dữ liệu, tổng hợp báo cáo và hiển thị trong app. Các báo cáo có thể ghi vào Sheet/cache, "
        "nhưng chưa có bước tự đẩy thông báo ra kênh bên ngoài.\n\n"
        "Muốn thêm phần này thì cần bổ sung nơi nhận webhook, mẫu nội dung gửi, cơ chế gửi lại khi lỗi "
        "và cách bảo vệ webhook URL trong giao diện/log. Chỉ đưa webhook URL thôi thì DeCho chưa tự gửi được."
    )


def _metric_relationship_reply(message: str) -> str | None:
    """Grounded answer for natural questions about how data sources relate."""
    norm = _filter_norm(message)
    has_pagespeed = any(k in norm for k in ("pagespeed", "page speed", "psi", "core web", "web vitals", "toc do", "speed"))
    has_seo = any(k in norm for k in ("seo", "gsc", "search console", "organic", "traffic", "clicks", "impressions"))
    asks_relation = any(k in norm for k in ("lien quan", "so voi", "khac nhau", "anh huong", "co lien he", "relation", "compare"))
    if not (has_pagespeed and has_seo and asks_relation):
        return None
    return (
        "Có liên quan, nhưng không phải cùng một loại số liệu nha Đại ca.\n\n"
        "- **PageSpeed/PSI** cho biết trang nhanh hay chậm: Performance Score, LCP, CLS, TBT, INP, TTFB.\n"
        "- **SEO/GSC/GA4** cho biết trang có kéo được traffic không: clicks, impressions, views, users, CTR.\n\n"
        "Cách DeCho nối hai phần này: trang nào **vừa chậm vừa có traffic/clicks/impressions cao** thì ưu tiên tối ưu trước. "
        "PageSpeed thấp không tự động nghĩa là SEO tụt, nhưng nó có thể làm UX, conversion và khả năng giữ traffic kém hơn."
    )


def _insights_grounded_reply(message: str) -> str | None:
    """Grounded answer for Insights data/cache/quota questions."""
    norm = _filter_norm(message)
    mentions_insights = any(k in norm for k in (
        "insights", "opportunity", "root cause", "experiments", "experiment", "tracking audit", "weekly autopilot",
    ))
    asks_about_data_or_cache = any(k in norm for k in (
        "data", "du lieu", "lay tu dau", "nguon", "source", "cache", "api", "goi moi", "goi lai",
        "lien tuc", "limit", "quota", "luu", "luu o dau", "fresh", "tai lai",
    ))
    asks_about_purpose = any(k in norm for k in ("dung de lam gi", "lam gi", "de lam gi", "la gi"))
    if not mentions_insights or not (asks_about_data_or_cache or asks_about_purpose):
        return None
    return (
        "Có cache nha Đại ca, không phải cứ mở Insights hay bấm **Tải lại** là DeCho gọi lại toàn bộ nguồn dữ liệu.\n\n"
        "Luồng hiện tại là:\n"
        "- Nếu dữ liệu vừa được lấy gần đây, DeCho dùng lại bản cache để tránh chậm và tránh đụng quota.\n"
        "- Nếu cache đã cũ, DeCho mới đọc lại các nguồn thật: PageSpeed Sheet, SEO Sheet, Google Ads và Clarity nếu có cấu hình.\n"
        "- Riêng Clarity được giữ cache lâu hơn vì nguồn này dễ hết limit hơn.\n\n"
        "Nói đơn giản: **Tải lại** nghĩa là “lấy bản tốt nhất hiện có”; còn cache mới thì trả nhanh, cache hết hạn thì mới refresh dữ liệu thật.\n\n"
        "Cache chỉ là bản lưu tạm cho app đang chạy, không phải nơi lưu báo cáo lâu dài. Muốn ép đọc mới hoàn toàn thì xóa cache hoặc đợi cache hết hạn."
    )


def _repair_response_prefix(text: str) -> str:
    """Fix occasional clipped first characters before sending final text."""
    base_fixes = (
        ("ưa có", "Chưa có"),
        ("ổng quan", "Tổng quan"),
        ("ưới đây", "Dưới đây"),
        ("áo cáo", "Báo cáo"),
        ("óm tắt", "Tóm tắt"),
        ("huyến nghị", "Khuyến nghị"),
        ("ên làm tiếp", "Nên làm tiếp"),
        ("hận xét", "Nhận xét"),
    )
    leading = text[:len(text) - len(text.lstrip())]
    body = text.lstrip()
    for prefix in ("", "# ", "## ", "### "):
        for bad, good in base_fixes:
            candidate = prefix + bad
            if body.startswith(candidate):
                return leading + prefix + good + body[len(candidate):]
    return text


def _repair_pagespeed_report_prefix(text: str) -> str:
    """Fix occasional clipped first characters in PageSpeed report titles."""
    text = _repair_response_prefix(text)
    fixes = (
        ("# Speed Report", "# PageSpeed Report"),
        ("Speed Report", "PageSpeed Report"),
        ("# áo cáo PageSpeed", "# Báo cáo PageSpeed"),
        ("áo cáo PageSpeed", "Báo cáo PageSpeed"),
        ("# 📊 áo cáo PageSpeed", "# 📊 Báo cáo PageSpeed"),
        ("📊 áo cáo PageSpeed", "📊 Báo cáo PageSpeed"),
    )
    leading = text[:len(text) - len(text.lstrip())]
    body = text.lstrip()
    for bad, good in fixes:
        if body.startswith(bad):
            return leading + good + body[len(bad):]
    return text


def _results_prompt(tab: str, headers: list, rows: list) -> str:
    keep = list(range(min(11, len(headers))))  # bỏ các cột label, giữ metric chính
    lines = [" | ".join(str(headers[i]) for i in keep)]
    for r in rows:
        lines.append(" | ".join(str(r[i]) if i < len(r) else "" for i in keep))
    return (
        "Bạn là PageSpeed Checker Agent (AI). Dưới đây là dữ liệu Core Web Vitals đã đo, "
        f"từ Google Sheet tab {tab} ({len(rows)} dòng gần nhất, mỗi dòng 1 lượt đo). "
        "Performance Score 0–100 càng cao càng tốt; FCP/LCP/TBT/INP/TTFB (ms) và CLS càng thấp càng tốt.\n\n"
        + "\n".join(lines) +
        "\n\nTrả lời câu hỏi dựa trên dữ liệu trên. Yêu cầu: TIẾNG VIỆT, ngắn gọn (tối đa ~15 dòng), "
        "nêu số liệu cụ thể, có thể dùng **đậm** và gạch đầu dòng. Với mọi khuyến nghị, kèm "
        "Evidence (URL + metric) và Confidence (high/medium/low). Không tự so với benchmark/industry nếu dữ liệu không có; "
        "không nói Google penalty, chỉ nói ảnh hưởng UX/conversion/Core Web Vitals nếu phù hợp. KHÔNG dùng ký hiệu LaTeX "
        "(viết mũi tên là →, không viết $\\rightarrow$). Trả lời trực tiếp, không suy luận dài. /no_think"
        + _knowledge() + _persona()
    )


def _analyze_results(question: str, model: str, tab: str, headers: list, rows: list) -> str:
    """Bản non-stream (dùng cho /api/chat cũ)."""
    import httpx

    r = httpx.post(
        f"{MAAS_BASE_URL}/chat/completions",
        json={"model": model, "temperature": 0.2, "max_tokens": 4096,
              "messages": [{"role": "system", "content": _results_prompt(tab, headers, rows)},
                           {"role": "user", "content": question}]},
        headers={"Authorization": f"Bearer {MAAS_API_KEY}"},
        timeout=180,
    )
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    # Qwen reasoning có thể trả content=None và dồn hết vào reasoning_content
    content = _strip_think(msg.get("content") or "")
    if not content:
        reasoning = _strip_think(msg.get("reasoning_content") or msg.get("reasoning") or "")
        content = reasoning or "❌ Model không trả về nội dung phân tích."
    return content


def _keyword_intent(message: str) -> dict | None:
    """Phân loại intent bằng keyword — fallback khi model không trả JSON."""
    import re

    m = message.lower()
    url_match = re.search(r"https?://\S+", message)
    if any(k in m for k in ("thêm", "add")) and url_match:
        return {"action": "add_url", "url": url_match.group(0).rstrip(".,;")}
    if any(k in m for k in ("xóa", "xoá", "remove", "delete", "bỏ")) and any(k in m for k in ("url", "http", "trang", "link")):
        return {"action": "remove_url",
                "url": url_match.group(0).rstrip(".,;") if url_match else message.split()[-1]}
    psi_ctx = any(k in m for k in ("pagespeed", "page speed", "lcp", "cls", "fcp", "tbt", "inp", "ttfb",
                                   "web vitals", "score", "điểm", "chậm", "nhanh"))
    report_ctx = _has_norm_phrase(message, ("báo cáo", "bao cao", "report", "tổng hợp", "tong hop", "review"))
    analysis_ctx = any(k in m for k in ("phân tích", "phan tich", "kết quả", "ket qua", "điểm",
                                        "chậm nhất", "nhanh nhất", "so sánh", "analyze",
                                        "score", "trung bình", "trung binh", "bao nhiêu", "bao nhieu",
                                        "average", "avg"))
    if analysis_ctx or (report_ctx and psi_ctx):
        terms = _parse_psi_url_terms(message)
        return {"action": "query_results", **({"url_terms": terms} if terms else {})}
    if any(k in m for k in ("danh sách", "list", "url nào", "những url")):
        return {"action": "list_urls"}
    if _has_norm_phrase(message, ("chạy", "check", "kiểm tra", "run", "trigger", "start")):
        return {"action": "run_check"}
    if any(k in m for k in ("trạng thái", "status", "xong chưa", "sao rồi")):
        return {"action": "status"}
    return None


def _ask_llm(message: str) -> str | None:
    """Phân loại intent qua MaaS LLM (non-stream). Trả về None nếu LLM không khả dụng."""
    if not (MAAS_API_KEY and MAAS_BASE_URL):
        return None
    import httpx

    try:
        r = httpx.post(
            f"{MAAS_BASE_URL}/chat/completions",
            json={"model": MAAS_MODEL, "temperature": 0,
                  "messages": [{"role": "system", "content": _system_prompt()},
                               {"role": "user", "content": message}]},
            headers={"Authorization": f"Bearer {MAAS_API_KEY}"},
            timeout=60,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        data = json.loads(content[content.index("{"):content.rindex("}") + 1])
        if data.get("action") == "query_results":
            tab, headers, rows = sheet_store.read_results()
            if not rows:
                return "Chưa có dữ liệu kết quả nào trong Sheet — chạy kiểm tra trước nhé."
            return _analyze_results(message, MAAS_MODEL, tab, headers, rows)
        return _execute_action(data)
    except Exception:  # noqa: BLE001 — LLM lỗi thì fallback keyword
        return None


class ChatStreamRequest(BaseModel):
    message: str
    model: str | None = None
    history: list[dict] | None = None  # [{"role":"user"|"assistant","content":"..."}]
    user_id: str | None = None     # AgentBase Memory: actorId (UUID phía client)
    session_id: str | None = None  # AgentBase Memory: sessionId (đổi khi xóa lịch sử)


def _sanitize_history(history: list[dict] | None, limit: int = 24) -> list[dict]:
    out = []
    for m in (history or [])[-limit:]:
        role, content = m.get("role"), str(m.get("content") or "")[:2000]
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


@app.get("/api/models")
def models():
    return {"models": ALLOWED_MODELS, "default": MAAS_MODEL}


def _unified_prompt() -> str:
    cfg = runtime_config.current()
    import seo_agent

    now = app_time.now()
    return (
        f"Hôm nay là {now.strftime('%Y-%m-%d')} (thứ {now.isoweekday()+1 if now.isoweekday()<7 else 'CN'}).\n"
        "Bạn là DeCho — AI agent all-in-one (app DeCho Agent, luôn khai báo là AI), quản lý 2 mảng:\n"
        f"A. PAGESPEED: kiểm tra Core Web Vitals cho {len(cfg['urls'])} URL, lịch {cfg['schedule_mode']} "
        f"lúc {cfg['schedule_time']}, ghi Google Sheet. Trạng thái: {_status_text()}\n"
        f"B. SEO: báo cáo GSC + GA4 hàng tháng cho {seo_agent.SITE_URL}, so sánh tháng trước, ghi Sheet riêng. "
        f"Trạng thái: {_seo_status_text()}\n"
        "C. PAID CAMPAIGNS: theo dõi Google Ads (read-only).\n"
        "Trả về DUY NHẤT một JSON theo intent:\n"
        '- Chạy/kiểm tra/đo/scan PageSpeed NGAY (real-time): {"action":"run_check"}\n'
        "QUY TẮC THỜI GIAN PAGESPEED — PageSpeed là phép đo REAL-TIME tại thời điểm chạy, KHÔNG thể chạy cho quá khứ hay tương lai:\n"
        '  + "chạy pagespeed tháng <quá khứ>" → hiểu là muốn XEM data đã đo tháng đó: {"action":"query_results","month":"YYYY-MM"}\n'
        '  + tháng TƯƠNG LAI → {"action":"reply","text":"<từ chối dí dỏm đúng tính cách: tháng đó chưa tới, Đệ chưa biết du hành thời gian; mời chạy ngay hoặc xem data tháng đã có>"}\n'
        '  + tháng hiện tại hoặc không nêu tháng → {"action":"run_check"}\n'
        '  + KHÔNG RÕ tháng nào/năm nào (vd nói "tháng 12" khi chưa rõ năm) → {"action":"reply","text":"<hỏi lại cho rõ tháng/năm>"} — đừng đoán.\n'
        '- Báo cáo/phân tích/xem/tổng hợp KẾT QUẢ PageSpeed đã đo (điểm, score, LCP/CLS, trang nhanh/chậm, "pagespeed/web có ỔN/TỐT không", "tình hình/sức khoẻ web thế nào", "báo cáo PageSpeed url tutorial và product"): {"action":"query_results"} — thêm "month":"YYYY-MM" nếu người dùng chỉ định tháng. Nếu người dùng nêu nhóm URL cho PageSpeed (vd "url tutorial và product", "trang /product"), thêm "url_terms":["tutorial","product"] hoặc ["product"]. "báo cáo PageSpeed" KHÔNG được chạy run_check; chỉ run_check khi có ý đo/chạy/check real-time rõ ràng.\n'
        '- Danh sách URL theo dõi: {"action":"list_urls"}\n'
        '- Thêm URL: {"action":"add_url","url":"<url>"}\n'
        '- Xóa URL: {"action":"remove_url","url":"<url hoặc từ khóa>"}\n'
        '- Đổi lịch PageSpeed: {"action":"set_schedule","schedule_mode":"daily|weekly|monthly","schedule_time":"HH:MM","schedule_day_of_month":<1-28>,"schedule_weekday":"<thứ>"} (chỉ kèm field người dùng nêu)\n'
        '- Chạy báo cáo SEO 1 tháng: {"action":"run_report","year":<năm>,"month":<1-12>} (bỏ year/month → tháng vừa rồi)\n'
        '- Chạy báo cáo SEO NHIỀU tháng TÁCH RIÊNG TỪNG THÁNG, mỗi tháng tự so với THÁNG LIỀN TRƯỚC (dùng khi user nói "so sánh từng tháng"/"tháng sau so tháng trước"/"backfill theo tháng"): {"action":"run_report","months":[{"year":2026,"month":3},{"year":2026,"month":4},...]}. KHÁC với seo_range (chỉ 1 kỳ + so với kỳ liền trước).\n'
        '- Phân tích số liệu SEO (traffic, views, users, clicks, impressions, "tình hình/sức khoẻ SEO", "SEO/traffic có ỔN/TỐT không", "SEO thế nào/ra sao"): {"action":"seo_query","month":"YYYY-MM hoặc bỏ"} '
        'hoặc nhiều tháng/xu hướng: {"action":"seo_query","months":["2026-01",...]} (tất cả: "months":"all"). '
        '"tình hình/ổn không/tốt không" về SEO = phân tích số liệu đã có → seo_query, KHÔNG phải status. Bỏ "month" thì Đệ lấy tháng mới nhất đã có.\n'
        '- Các tháng có báo cáo SEO: {"action":"list_months"}\n'
        '- Báo cáo SEO theo KHOẢNG THỜI GIAN tự nhiên ("3 tháng gần nhất", "tuần trước", "quý 1 2026", "từ 01/05 đến 12/06", "từ đầu năm đến nay", "cả năm 2025", "năm ngoái", "6 tháng đầu năm"...): '
        '{"action":"seo_range","start":"YYYY-MM-DD","end":"YYYY-MM-DD"} — TỰ TÍNH ngày từ hôm nay. '
        'Quy ước: "N tháng gần nhất" = N tháng TRỌN VẸN trước tháng hiện tại (vd hôm nay 2026-06-12 thì "3 tháng gần nhất" = 2026-03-01 → 2026-05-31); '
        '"N ngày gần nhất" = N ngày kết thúc hôm qua; "tuần trước" = thứ 2 → CN tuần trước. '
        'Nếu input mơ hồ không tính được ngày → đừng đoán, dùng {"action":"reply"} hỏi lại.\n'
        'LỌC CHUNG: nếu user muốn chỉ một nhóm URL/campaign (vd "url tutorial và product", "trang /product, /tutorial", "campaign brand trừ competitor"), THÊM filter_spec với include/exclude nhiều keyword. Bỏ filter nếu xét toàn bộ.\n'
        '- Người dùng XÁC NHẬN đề xuất ngay trước đó ("ok", "đồng ý", "chạy đi"): {"action":"confirm"}\n'
        '- Danh sách campaign Google Ads (CHỈ khi hỏi "liệt kê/có những campaign nào"): {"action":"ads_list"}\n'
        '- Hiệu suất/chi tiêu/CPA/CTR Google Ads, HOẶC "tình hình/hiệu quả campaigns", "ads ổn không", "quảng cáo thế nào": {"action":"ads_perf","days":<số ngày, mặc định 7>} '
        'hoặc khoảng thời gian tự nhiên ("ads tháng 5", "chi tiêu từ 01/05 đến 31/05", "quảng cáo quý 1"): '
        '{"action":"ads_perf","start":"YYYY-MM-DD","end":"YYYY-MM-DD"} — tự tính ngày như seo_range\n'
        '- Hiệu suất theo LANDING PAGE / trang đích ("landing page nào hiệu quả", "trang đích tốn tiền mà không convert", "phân tích LDP", "trang nào bounce cao/tải chậm"): {"action":"ldp_perf","days":<mặc định 7>} (hoặc start/end)\n'
        '- Microsoft CLARITY / hành vi người dùng trên web ("clarity", "user hành xử sao", "rage click", "dead click", "scroll depth", "UX trang web"): {"action":"clarity"}\n'
        '- PHÂN TÍCH TỔNG HỢP Ads + landing page + Clarity (user journey, "vì sao traffic trả tiền mà không convert", "phân tích tổng hợp ads", "từ click tới hành vi"): {"action":"combined","days":<mặc định 7>}\n'
        '- TẠO campaign Google Ads ("tạo campaign", "tạo chiến dịch mới"): {"action":"create_campaign"} — Đệ chỉ hướng dẫn mở form bảo mật ở tab Paid Campaigns (KHÔNG nhận mật khẩu qua chat, đừng hỏi/đọc mật khẩu trong chat).\n'
        '- ALERT MONITOR / cảnh báo bất thường ("có alert gì", "monitor hôm nay", "cảnh báo nào cần xử lý"): {"action":"alerts"}\n'
        '- CONVERSION TRACKING AUDITOR ("tracking conversion ổn không", "audit tracking", "thiếu conversion tracking", "GTM/GA4 event/conversion action có lỗi không"): {"action":"tracking_audit","days":<mặc định 30>}\n'
        '- Trạng thái hệ thống: {"action":"status"}\n'
        '- HƯỚNG DẪN / HỎI VỀ NĂNG LỰC ("DeCho làm được gì", "có tính năng nào", "làm sao thêm URL", "đổi lịch ở đâu", "LCP là gì", "score bao nhiêu là tốt", "lọc URL được không"): {"action":"help"}\n'
        '- GHI NHỚ theo yêu cầu ("nhớ giúp...", "ghi nhớ...", "DeCho nhớ là...", "lưu lại: ...", "từ giờ gọi tôi là...", "thông tin về anh/cửa hàng/brand là..."): {"action":"remember","fact":"<dữ kiện cô đọng, ngôi thứ 3 về Đại ca/doanh nghiệp>"}\n'
        '- TÌM TRÊN WEB (thông tin NGOÀI dữ liệu nội bộ: tin tức/cập nhật mới, đối thủ, xu hướng thị trường, "best practice mới nhất", giá dịch vụ bên ngoài, sự kiện sau tháng 5/2025): {"action":"web_search","query":"<từ khoá tìm kiếm súc tích>"}. CHỦ ĐỘNG chọn web_search khi câu cần thông tin CẬP NHẬT/ngoài hệ thống — đừng trả lời chung chung từ trí nhớ rồi thôi.\n'
        '- ĐỌC 1 URL cụ thể (người dùng dán link hoặc nói "đọc/tóm tắt trang này"): {"action":"web_fetch","url":"<url>"}\n'
        '- ƯU TIÊN TỐI ƯU / Opportunity Score ("nên tối ưu trang nào", "trang nào vừa chậm vừa nhiều traffic", "ưu tiên fix"): {"action":"priority_fix"}\n'
        '- WEEKLY AUTOPILOT / báo cáo vận hành tuần đầy đủ ("weekly autopilot", "báo cáo tuần", "top việc tuần này", "weekly report"): {"action":"weekly_autopilot"}\n'
        '- KẾ HOẠCH HÀNH ĐỘNG từ Opportunity Score + alerts ("tuần này nên làm gì", "cần làm gì", "lên kế hoạch", "to-do", "ưu tiên công việc"): {"action":"action_plan"}\n'
        '- CHẨN ĐOÁN SỤT GIẢM ("tại sao clicks/traffic/views giảm", "vì sao tụt", "trang nào kéo xuống", "phân tích nguyên nhân giảm"): {"action":"diagnose_drop"}\n'
        '- GỢI Ý CÁCH SỬA trang chậm ("làm sao tăng tốc", "cách tối ưu trang X", "fix LCP/CLS thế nào", "trang chậm sửa sao"): {"action":"fix_suggest","url":"<path nếu có>"}\n'
        '- EXPERIMENT PLANNER / kế hoạch đo sau khi làm ("làm experiment", "baseline/success metric/rollback", "đo sau khi sửa"): {"action":"experiment_plan"}\n'
        'FILTER CHUNG: nếu user nêu nhiều keyword/path/campaign như "url tutorial và product", "trang /product, /tutorial", "chỉ product trừ blog", hoặc "campaign brand và competitor", thêm '
        '{"filter_spec":{"scope":"url|campaign","include":["tutorial","product"],"exclude":["blog"],"match_mode":"any|all"}}. Mặc định "và/hoặc/dấu phẩy" = any; chỉ dùng all khi user nói rõ "phải chứa cả". Áp filter cho PageSpeed, SEO, Ads, Clarity, Opportunity, Alerts, kế hoạch.\n'
        '- Còn lại: {"action":"reply","text":"<trả lời ngắn>"}\n'
        "Phân biệt: kiểm tra/điểm/score/LCP/CLS/pagespeed → PageSpeed; báo cáo/traffic/clicks/GSC/GA4/SEO → SEO."
    ) + _persona()


def _capabilities() -> str:
    """Danh sách năng lực THẬT của DeCho — nguồn grounded cho action help (không để model bịa)."""
    return (
        "NĂNG LỰC THẬT CỦA DECHO (chỉ trả lời dựa trên đây, không bịa thêm tính năng không có):\n"
        "## PageSpeed (Core Web Vitals)\n"
        "- Chạy kiểm tra real-time mọi URL đang theo dõi: nói 'chạy kiểm tra ngay'. PageSpeed CHỈ đo hiện tại, không đo lại quá khứ/tương lai.\n"
        "- Xem & phân tích kết quả đã đo (điểm, LCP/CLS/FCP/TBT, trang nhanh/chậm): 'phân tích PageSpeed', 'trang nào chậm nhất', 'xem điểm tháng 5'.\n"
        "- Dashboard (menu PageSpeed): điểm TB mobile/desktop, xu hướng theo lần chạy, bảng chi tiết sort được theo từng cột.\n"
        "## SEO (Google Search Console + GA4)\n"
        "- Chạy báo cáo 1 tháng / nhiều tháng (mỗi tháng tự so tháng liền trước): 'báo cáo SEO tháng 5', 'so sánh từng tháng từ tháng 3 đến tháng 5'.\n"
        "- Báo cáo theo khoảng thời gian tự nhiên, tự so kỳ liền trước: '3 tháng gần nhất', 'quý 1 2026', 'từ 01/05 đến 12/06', 'cả năm 2025'. Có bước xác nhận trước khi chạy.\n"
        "- Lọc theo nhóm URL: thêm 'các url chứa /tutorial' / 'trang /product' vào yêu cầu.\n"
        "- Phân tích số liệu traffic/clicks/views/users/impressions: 'traffic tháng này sao', 'xu hướng 6 tháng'.\n"
        "## URL Intelligence (menu URL Intelligence)\n"
        "- Bảng gộp traffic (GSC/GA4) + PageSpeed theo từng URL, click 1 dòng xem chi tiết + lịch sử điểm; sort theo mọi cột; ô tìm URL.\n"
        "## Paid Campaigns (Google Ads — chỉ ĐỌC, không tạo/sửa campaign, không tiêu tiền)\n"
        "- Danh sách campaign, hiệu suất/chi tiêu/CTR/CPA theo N ngày hoặc khoảng ngày tự nhiên: 'chi tiêu ads tháng 5', 'CPA 30 ngày'. Lọc ngày bằng lời hoặc bằng date picker.\n"
        "- Hiệu suất theo từng LANDING PAGE (impr/clicks/CTR/CPC/cost/conv + bounce + speed score), kèm link Microsoft Clarity heatmap/recordings: 'phân tích landing page', 'trang đích nào tốn tiền mà không convert'.\n"
        "- Microsoft Clarity (hành vi UX): sessions, engagement, scroll depth, rage/dead click, bot — 'clarity', 'user hành xử sao trên web'. Cần cấu hình CLARITY_PROJECT_ID/CLARITY_API_TOKEN.\n"
        "- Phân tích TỔNG HỢP user journey: gộp Ads + landing page + Clarity để chẩn đoán vì sao traffic trả tiền nhưng không convert — 'phân tích tổng hợp ads', 'từ click tới hành vi'.\n"
        "- TẠO campaign Search mới (GHI): chỉ tạo được khi nhập ĐÚNG MẬT KHẨU; campaign luôn ở trạng thái PAUSED (chưa tiêu tiền) để Đại ca review rồi tự bật. Mặc định budget 100k VND/ngày, target Việt Nam, có sẵn headline/mô tả/sitelink.\n"
        "## Cấu hình (menu Cấu hình)\n"
        "- Thêm/xóa URL theo dõi, đổi lịch PageSpeed (daily/weekly/monthly + giờ), đổi lịch & URL theo dõi SEO — chỉnh bằng lời ('thêm https://...', 'đổi lịch sang daily 8h') hoặc trong trang Cấu hình. Lưu là áp dụng ngay + đồng bộ Google Sheet.\n"
        "## Insight & hành động (gộp PSI + SEO + Ads + Clarity)\n"
        "- Ưu tiên tối ưu: 'nên tối ưu trang nào trước' → Opportunity Score gộp PSI + SEO + Ads + Clarity, kèm Evidence/Confidence.\n"
        "- Kế hoạch tuần: 'tuần này nên làm gì' → to-do xếp ưu tiên từ Opportunity Score + alerts + xu hướng.\n"
        "- Alert monitor: 'có alert gì không' → cảnh báo PSI/SEO/Ads/Clarity kèm Evidence/Confidence.\n"
        "- Conversion Tracking Auditor: 'tracking conversion ổn không' → soi campaign/landing page có cost/click nhưng 0 conversion, cảnh báo signal conversion không đáng tin.\n"
        "- Chẩn đoán sụt giảm: 'tại sao clicks/traffic giảm' → trang nào kéo xuống + nguyên nhân khả dĩ.\n"
        "- Gợi ý cách sửa trang chậm: 'cách tăng tốc trang X' → hành động cụ thể theo LCP/CLS/TBT.\n"
        "- Experiment Planner: 'làm experiment/đo sau khi sửa' → baseline, success metric, target 7/14 ngày và rollback rule cho top opportunity.\n"
        "## Bộ lọc chung\n"
        "- Hầu hết phân tích hỗ trợ nhiều keyword/entity: 'url tutorial và product', 'trang /product, /tutorial', 'chỉ product trừ blog', 'campaign brand và competitor'. Mặc định OR; muốn AND thì nói 'phải chứa cả'.\n"
        "- Entity catalog tự học từ URL đang theo dõi và campaign/landing data đã đọc: hiểu 'product', 'tutorial', 'vdb', 'vdb mysql', 'brand campaign', 'cloud server' là nhóm thật thay vì bắt mọi từ trong câu làm keyword.\n"
        "## Tìm & đọc web\n"
        "- Tìm thông tin NGOÀI dữ liệu nội bộ (tin mới, đối thủ, xu hướng, best practice): 'tìm trên mạng ...', 'tin mới nhất về ...'. Trả lời kèm trích nguồn.\n"
        "- Đọc/tóm tắt một URL cụ thể: dán link và nói 'đọc/tóm tắt trang này'.\n"
        "## Khác\n"
        "- Trí nhớ dài hạn (nhớ sở thích/URL Đại ca quan tâm qua các phiên), trạng thái hệ thống, đổi model AI ở header.\n"
        "GIẢI THÍCH CHỈ SỐ (nếu được hỏi): LCP (Largest Contentful Paint) tốt <2.5s; CLS (độ giật layout) tốt <0.1; FCP <1.8s; TBT <200ms; điểm PageSpeed ≥90 xanh/tốt, 50-89 cần cải thiện, <50 kém. Traffic: clicks (GSC) = lượt bấm từ tìm kiếm, impressions = lượt hiển thị, views/users (GA4).\n"
    )


def _help_prompt() -> str:
    return (
        "Người dùng đang hỏi về NĂNG LỰC hoặc CÁCH DÙNG của bạn (DeCho — agent marketing all-in-one). "
        "Trả lời ĐÚNG TRỌNG TÂM câu hỏi, ngắn gọn, dựa DUY NHẤT trên danh sách dưới đây. "
        "Nếu hỏi cách làm, chỉ rõ câu lệnh mẫu để gõ hoặc menu cần vào. "
        "Nếu hỏi tính năng không có trong danh sách, nói thẳng là chưa hỗ trợ — KHÔNG bịa. "
        "Gợi ý 2-3 việc liên quan người dùng có thể làm tiếp. KHÔNG dùng LaTeX. /no_think\n\n"
        + _capabilities()
    ) + _persona()


def _parse_range_vi(m: str) -> dict | None:
    """Fallback parse khoảng thời gian tiếng Việt phổ biến (không cần LLM)."""
    import re
    from datetime import date, timedelta

    from dateutil.relativedelta import relativedelta

    today = app_time.today()
    mm = re.search(r"(\d+)\s*tháng gần nhất", m)
    if mm:  # N tháng TRỌN VẸN trước tháng hiện tại
        n = int(mm.group(1))
        end = today.replace(day=1) - timedelta(days=1)
        start = (end.replace(day=1) - relativedelta(months=n - 1))
        return {"action": "seo_range", "start": start.isoformat(), "end": end.isoformat()}
    mm = re.search(r"(\d+)\s*ngày gần nhất", m)
    if mm:
        end = today - timedelta(days=1)
        start = end - timedelta(days=int(mm.group(1)) - 1)
        return {"action": "seo_range", "start": start.isoformat(), "end": end.isoformat()}
    if "tuần trước" in m:
        end = today - timedelta(days=today.isoweekday())  # CN tuần trước
        start = end - timedelta(days=6)
        return {"action": "seo_range", "start": start.isoformat(), "end": end.isoformat()}
    mm = re.search(r"(?:cả\s*)?năm\s*(20\d{2})", m)
    if mm and "đầu năm" not in m:
        y = int(mm.group(1))
        end = date(y, 12, 31)
        if end >= today:
            end = today - timedelta(days=1)
        return {"action": "seo_range", "start": date(y, 1, 1).isoformat(), "end": end.isoformat()}
    if "năm ngoái" in m or "năm trước" in m:
        y = today.year - 1
        return {"action": "seo_range", "start": date(y, 1, 1).isoformat(), "end": date(y, 12, 31).isoformat()}
    mm = re.search(r"(\d+)\s*tháng đầu năm(?:\s*(20\d{2}))?", m)
    if mm:
        n = int(mm.group(1)); y = int(mm.group(2) or today.year)
        end = (date(y, n, 1) + relativedelta(months=1)) - timedelta(days=1)
        if end >= today:
            end = today - timedelta(days=1)
        return {"action": "seo_range", "start": date(y, 1, 1).isoformat(), "end": end.isoformat()}
    if "từ đầu năm" in m or "đầu năm đến nay" in m or "năm nay" in m:
        return {"action": "seo_range", "start": today.replace(month=1, day=1).isoformat(),
                "end": (today - timedelta(days=1)).isoformat()}
    mm = re.search(r"quý\s*([1-4])(?:\s*năm)?\s*(20\d{2})?", m)
    if mm:
        q = int(mm.group(1)); y = int(mm.group(2) or today.year)
        start = date(y, 3 * q - 2, 1)
        end = (start + relativedelta(months=3)) - timedelta(days=1)
        return {"action": "seo_range", "start": start.isoformat(), "end": end.isoformat()}
    return None


def _parse_url_contains(message: str) -> str | None:
    """Bắt từ khóa lọc URL: 'url chứa /tutorial', 'các trang /product', 'bài /blog'."""
    spec = entity_resolver.resolve(message, "url")
    if not _filter_active(spec):
        spec = _parse_filter_spec(message, "url")
    return (spec.get("include") or [None])[0]


_PSI_RUN_CUES = ("chạy", "chay", "kiểm tra", "kiem tra", "check", "run", "trigger", "start",
                 "đo", "do ", "scan", "quét", "quet", "crawl", "cào")
_PSI_REPORT_CUES = ("báo cáo", "bao cao", "phân tích", "phan tich", "tổng hợp", "tong hop",
                    "xem", "kết quả", "ket qua", "report", "review", "đánh giá", "danh gia")


def _filter_norm(s: str) -> str:
    import re
    import unicodedata

    raw = unicodedata.normalize("NFD", s or "")
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    raw = raw.replace("đ", "d").replace("Đ", "D")

    def split_token(match):
        token = match.group(0)
        if re.search(r"[a-z][A-Z]|[0-9][A-Z]", token):
            token = re.sub(r"([0-9])([A-Z][a-z])", r"\1-\2", token)
            token = re.sub(r"([a-z])([A-Z])", r"\1-\2", token)
            if re.search(r"[a-z]AI$", match.group(0)):
                token = re.sub(r"-AI$", "AI", token)
        return token

    raw = re.sub(r"[A-Za-z0-9]+", split_token, raw)
    return raw.lower()


def _has_norm_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    import re

    hay = _filter_norm(text)
    for phrase in phrases:
        needle = _filter_norm(str(phrase or "")).strip()
        if not needle:
            continue
        if re.search(rf"(?<![a-z0-9_\-/]){re.escape(needle)}(?![a-z0-9_\-/])", hay):
            return True
    return False


def _has_raw_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    import re

    hay = (text or "").lower()
    for phrase in phrases:
        needle = str(phrase or "").lower().strip()
        if not needle:
            continue
        if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay):
            return True
    return False


_FILTER_STOP = {
    "a", "an", "and", "or", "the", "va", "voi", "hoac", "hoặc", "hay", "cac", "các", "nhung", "những",
    "nao", "nào", "co", "có", "chua", "chứa", "gom", "gồm", "chi", "chỉ", "loc", "lọc",
    "cho", "phai", "phải", "ca", "cả", "trong", "cau", "câu", "filter", "filters", "keyword", "keywords",
    "dung", "đừng", "generic", "match", "all",
    "thay", "doi", "đổi", "truc", "trúc", "truu", "tuong", "tượng", "structure", "site", "sanh", "sánh",
    "url", "urls", "trang", "page", "pages", "path", "duong", "dan", "đường", "dẫn",
    "landing", "ldp", "campaign", "campaigns", "chien", "dich", "chiến", "dịch",
    "pagespeed", "page-speed", "speed", "psi", "seo", "gsc", "ga4", "ads", "clarity",
    "bao", "cao", "báo", "cáo", "phan", "tich", "phân", "tích", "tong", "hop", "tổng", "hợp",
    "xem", "ket", "qua", "kết", "quả", "review", "report", "diem", "điểm", "score",
    "danh", "sach", "danh-sach", "trung", "binh", "trung-binh", "bao-nhieu", "nhieu",
    "so", "số", "lieu", "liệu", "so-lieu", "số-liệu", "du", "dữ", "du-lieu", "dữ-liệu",
    "data", "metric", "metrics", "kpi", "kpis", "reporting", "performance",
    "chay", "chạy", "kiem", "tra", "kiểm", "đo", "do", "scan", "quet", "quét",
    "thang", "tháng", "nam", "năm", "ngay", "ngày", "tuan", "tuần", "quy", "quý",
    "gan", "nhat", "gần", "nhất", "vua", "roi", "vừa", "rồi", "nay", "nay", "dang", "đang",
    "on", "ổn", "khong", "không", "ko", "hong", "hông", "tot", "tốt", "xau", "xấu",
    "khoe", "khoẻ", "the", "thế", "ra", "sao", "cham", "chậm", "nhanh", "slow", "fast",
    "active", "running", "status", "run", "rate",
    "tu", "từ", "den", "đến", "toi", "tới", "hien", "tai", "hiện", "tại",
    "nen", "lam", "gi", "nên", "làm", "gì", "uu", "tien", "ưu", "tiên",
    "can", "cần", "viec", "việc", "cong", "công",
    "alert", "alerts", "canh", "bao", "cảnh", "báo", "fix", "sua", "sửa",
    "toi", "uu", "tối", "ưu", "traffic", "clicks", "views", "users", "impressions",
    "google", "paid", "performance", "hieu", "hiệu", "suat", "suất", "hieu-qua", "hiệu-quả",
    "quang", "quảng", "ad", "advertising",
    "chi", "tieu", "tiêu", "cpa", "ctr", "ngan", "sach", "ngân", "sách",
    "conversion", "conversions", "convert", "tracking", "audit", "event", "events",
    "spend", "reach", "frequency", "value", "lead", "leads", "roas",
    "organic", "scope", "ranking", "rank", "position", "lcp", "cls", "tbt", "inp",
    "table", "drop", "script", "private", "shadow", "passwd", "secret", "etc", "tmp",
    "dau", "cuoi", "đầu", "cuối", "truoc", "sau", "trước", "sau",
}


def _filter_context_strength(message: str) -> str:
    """none | weak | explicit.

    weak: natural group mention like "trang product" — only known entities.
    explicit: URL/path/campaign/filter cues — allow new free-text terms too.
    """
    import re

    raw = message or ""
    raw_l = raw.lower()
    norm = _filter_norm(raw)
    if re.search(r"https?://|(?<![a-z0-9])\/[\w\-/]+", raw, re.I):
        return "explicit"
    explicit_cues = (
        "url", "urls", "path", "landing", "ldp", "duong dan", "đường dẫn",
        "campaign", "campaigns", "chien dich", "chiến dịch",
        "gom", "loc", "tru",
        "ngoai tru", "ngoại trừ", "exclude", "except",
    )
    accented_cues = ("chứa", "gồm", "lọc", "trừ", "ngoại trừ")
    if _has_norm_phrase(raw, explicit_cues) or _has_raw_phrase(raw_l, accented_cues):
        return "explicit"
    weak_cues = ("trang", "page", "pages")
    if _has_norm_phrase(raw, weak_cues):
        return "weak"
    return "none"


def _empty_filter_spec(scope: str, match_mode: str = "any") -> dict:
    return {
        "scope": scope or "url",
        "include": [],
        "exclude": [],
        "match_mode": match_mode or "any",
        "entities": [],
        "entity_labels": [],
        "exclude_entities": [],
    }


def _filter_match_mode(message: str) -> str:
    return "all" if (
        _has_norm_phrase(message, ("phai chua ca", "chua ca", "match all", "tat ca keyword"))
        or _has_raw_phrase(message, ("phải chứa cả", "chứa cả", "tất cả keyword"))
    ) else "any"


def _filter_request_suppressed(message: str) -> bool:
    import re

    sep = _filter_sep_norm(message)
    if re.match(r"^\s*(?:khong phai|not)\b", sep):
        return True
    return (
        _has_norm_phrase(message, ("dung loc", "dung filter", "khong can loc", "khong can filter", "do not filter", "dont filter"))
        or _has_raw_phrase(message, ("đừng lọc", "đừng filter", "không cần lọc", "không cần filter"))
    )


def _message_has_campaign_identifier(message: str) -> bool:
    import re

    return bool(
        re.search(r"\b(?:GG|PMAX|Competitor|Remarketing|seoLeads)[-_]?[A-Za-z0-9_-]*[A-Z_][A-Za-z0-9_-]*\b", message or "")
        or re.search(r"\bBrand[_-][A-Za-z0-9_-]+\b", message or "")
        or re.search(r"\bBrandAwareness[A-Za-z0-9]*\b", message or "")
    )


def _term_looks_campaign_identifier(term: str) -> bool:
    import re

    clean = str(term or "").strip().lower()
    return bool(
        re.search(r"^(?:gg|pmax|brand|competitor|remarketing|seoleads|seo-leads)[_-][a-z0-9_-]+$", clean)
        or re.search(r"^(?:mlops|abm)[_-][a-z0-9_-]+$", clean)
    )


def _message_has_url_scope_hint(message: str) -> bool:
    import re

    raw = message or ""
    safe_raw = re.sub(r"(?:\.\.?/)+[\w\-/]+|<[^>]+>", " ", raw)
    return (
        bool(re.search(r"https?://|(?<![a-z0-9])\/[\w\-/]+|(?<![a-z0-9])[\w-]+/[\w\-/]+", safe_raw, re.I))
        or _has_norm_phrase(raw, (
            "blog", "docs", "api gpu", "case study", "gpu benchmark",
            "h200 vs h100", "resources", "solutions",
        ))
    )


def _message_has_filter_analysis_cue(message: str, scope: str) -> bool:
    common = (
        "traffic", "click", "clicks", "view", "views", "user", "users", "impression", "impressions",
        "conversion", "conversions", "convert", "ctr", "cpa", "score", "performance", "hieu suat",
        "hieu qua", "organic", "tracking", "ranking", "rank", "position", "keyword",
        "lcp", "cls", "tbt", "inp", "pagespeed", "page speed", "cham", "chậm", "slow",
        "loi", "lỗi", "giam", "giảm", "tang", "tăng", "tut", "tụt",
        "ra sao", "on", "ổn", "cao", "thap", "thấp", "assist",
    )
    campaign = (
        "cost", "spend", "chi tieu", "chi tiêu", "budget", "ngan sach", "ngân sách",
        "reach", "frequency", "lead", "leads", "value", "roas", "cpc", "cpm",
    )
    return _has_norm_phrase(message, common + (campaign if scope == "campaign" else ()))


def _message_has_knowledge_cue(message: str) -> bool:
    return (
        _has_norm_phrase(message, (
            "la gi", "nghia la gi", "bao nhieu tien", "gia", "market", "ngoai thi truong",
            "mua", "thue", "nen chon", "can tai lieu", "ben ngoai", "cau hinh gi",
            "doi tac", "doi thu", "tong quan", "dung de lam gi", "can doc", "de hieu",
            "khac gi", "khac", "so voi", "co nghia", "giai thich", "co phai",
            "dang tin", "la loai", "loai campaign", "search hay display", "audience gi",
            "muc tieu", "khi nao", "naming convention", "cau truc", "la ai",
            "la san pham", "san pham gi", "dinh gia", "kien thuc", "tai lieu ky thuat",
        ))
        or _has_raw_phrase(message, (
            "là gì", "nghĩa là gì", "bao nhiêu tiền", "giá", "ngoài thị trường",
            "mua", "thuê", "nên chọn", "cần tài liệu", "bên ngoài", "cấu hình gì",
            "đối tác", "đối thủ", "tổng quan", "dùng để làm gì", "cần đọc", "để hiểu",
            "khác gì", "khác", "so với", "có nghĩa", "giải thích", "có phải",
            "đáng tin", "là loại", "loại campaign", "search hay display", "audience gì",
            "mục tiêu", "khi nào", "naming convention", "cấu trúc", "là ai",
            "là sản phẩm", "sản phẩm gì", "định giá", "kiến thức", "tài liệu kỹ thuật",
        ))
    )


def _message_has_noise_trap(message: str) -> bool:
    return _has_norm_phrase(message, (
        "urlencoded", "preurl", "urlify", "urlness", "nonurl",
        "pathological", "pathfinder", "pathway", "apath",
        "campaigner", "campaigning", "campaignish", "campaignless",
        "landingness", "landingish", "filtering", "filtered", "filterable",
        "narrative", "copy", "workload",
    ))


def _campaign_free_terms_allowed(message: str) -> bool:
    import re

    raw = message or ""
    if (
        _has_norm_phrase(raw, ("chua", "loc", "phai chua ca", "match all"))
        or _has_norm_phrase(raw, ("tat ca keyword",))
        or _has_raw_phrase(raw, ("chứa", "lọc", "phải chứa cả", "tất cả keyword"))
    ):
        return True
    return bool(re.search(r"\b[A-Za-z0-9]+[-_][A-Za-z0-9_-]+\b|[A-Z]{2,}[A-Za-z]+|[A-Z][a-z]+[A-Z][A-Za-z]*|,", raw))


def _filter_natural_terms_allowed(message: str, scope: str) -> bool:
    strength = _filter_context_strength(message)
    if _filter_request_suppressed(message):
        return False
    if scope == "campaign":
        if _has_norm_phrase(message, ("not campaign", "khong phai campaign", "khong phai chien dich")) or _has_raw_phrase(message, ("không phải campaign", "không phải chiến dịch")):
            return False
        if _message_has_url_scope_hint(message):
            return False
        if _message_has_noise_trap(message) and not _message_has_filter_analysis_cue(message, scope):
            return False
        if _message_has_knowledge_cue(message) and not _message_has_filter_analysis_cue(message, scope):
            return False
        if strength == "explicit":
            return True
        return _message_has_campaign_identifier(message) or _message_has_filter_analysis_cue(message, scope)
    if _message_has_campaign_identifier(message):
        return False
    if (
        _has_norm_phrase(message, ("giai thich", "co phai", "la san pham", "san pham gi", "dang tin"))
        or _has_raw_phrase(message, ("giải thích", "có phải", "là sản phẩm", "sản phẩm gì", "đáng tin"))
    ):
        return False
    if (
        _has_norm_phrase(message, ("dinh gia", "tai lieu ky thuat"))
        or _has_raw_phrase(message, ("định giá", "tài liệu kỹ thuật"))
    ):
        return False
    if _message_has_knowledge_cue(message) and not _message_has_filter_analysis_cue(message, scope):
        return False
    if _message_has_noise_trap(message) and not _message_has_filter_analysis_cue(message, scope):
        return False
    return True


def _filter_entity_spec_safe(spec: dict, message: str, default_scope: str) -> bool:
    if default_scope == "url" and (spec or {}).get("include") == ["ai"]:
        if (
            _has_norm_phrase(message, ("viettel ai", "fpt ai", "cmc ai", "aws ai", "azure ai", "gcp ai"))
            and not _message_has_filter_analysis_cue(message, "url")
        ):
            return False
    return True


def _has_filter_context(message: str) -> bool:
    return _filter_context_strength(message) != "none"


def _known_filter_terms(scope: str) -> set[str]:
    vals: set[str] = set()
    try:
        ents = entity_resolver.catalog(runtime_config.current().get("urls") or [])
    except Exception:  # noqa: BLE001
        ents = []
    for ent in ents:
        if ent.get("scope") != scope:
            continue
        for raw in [ent.get("label"), *(ent.get("aliases") or []), *(ent.get("patterns") or [])]:
            clean = _filter_clean_term(str(raw or ""))
            if not clean:
                continue
            vals.add(clean)
            if "/" in clean:
                vals.add(clean.rsplit("/", 1)[-1])
    return vals


def _filter_term_allowed(term: str, scope: str, strength: str, message: str = "") -> bool:
    clean = _filter_clean_term(term)
    clean_l = clean.lower()
    if not clean or _filter_is_stop_term(clean) or clean_l.isdigit() or len(clean) < 2:
        return False
    if scope == "campaign" and "/" in clean_l:
        return False
    if scope == "url" and _term_looks_campaign_identifier(clean):
        return False
    known = _known_filter_terms(scope)
    known_l = {k.lower() for k in known}
    if clean_l in known_l:
        return True
    if any(k.lower().endswith("/" + clean_l) for k in known):
        return True
    if scope == "campaign" and not _campaign_free_terms_allowed(message):
        return False
    return strength == "explicit"


def _filter_is_stop_term(term: str) -> bool:
    import re

    clean = _filter_clean_term(term)
    clean_l = clean.lower()
    if not clean:
        return True
    if clean_l in {"drop-table"}:
        return False
    if clean_l in _FILTER_STOP:
        return True
    parts = [p for p in re.split(r"[-\s]+", clean_l) if p]
    return bool(parts) and all(p in _FILTER_STOP or p.isdigit() for p in parts)


def _filter_sep_norm(text: str) -> str:
    import re
    import unicodedata

    raw = unicodedata.normalize("NFD", text or "")
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    raw = raw.replace("đ", "d").replace("Đ", "D")
    raw = raw.lower()
    raw = re.sub(r"[^a-z0-9_\-/\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _filter_term_exact_in_text(text: str, term: str) -> bool:
    import re

    hay = _filter_sep_norm(text)
    clean = _filter_clean_term(term).lower()
    if not clean:
        return False
    parts = [re.escape(p) for p in re.split(r"[-_\s]+", clean) if p]
    if not parts:
        return False
    if "/" in clean:
        needle = re.escape(clean)
    else:
        needle = r"[-_\s]+".join(parts)
    return bool(re.search(rf"(?<![a-z0-9_\-/]){needle}(?![a-z0-9_\-/])", hay))


def _filter_negated_text(message: str) -> str:
    import re

    parts = re.split(r"\b(?:khong phai|không phải|not)\b", _filter_sep_norm(message), maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _sanitize_filter_spec(
    spec: dict,
    message: str,
    default_scope: str = "url",
    *,
    require_exact_terms: bool = False,
) -> dict:
    scope = spec.get("scope") or default_scope
    strength = _filter_context_strength(message)
    negated_text = _filter_negated_text(message)
    out = {
        "scope": scope,
        "include": [],
        "exclude": [],
        "match_mode": spec.get("match_mode") or "any",
        "entities": list(spec.get("entities") or []),
        "entity_labels": list(spec.get("entity_labels") or []),
        "exclude_entities": list(spec.get("exclude_entities") or []),
    }
    seen_i, seen_e = set(), set()
    for term in spec.get("include") or []:
        if require_exact_terms and not _filter_term_exact_in_text(message, str(term)):
            continue
        if negated_text and _filter_term_exact_in_text(negated_text, str(term)):
            continue
        if _filter_term_allowed(str(term), scope, strength, message):
            _filter_add_term(out["include"], seen_i, str(term))
    for term in spec.get("exclude") or []:
        if require_exact_terms and not _filter_term_exact_in_text(message, str(term)):
            continue
        if negated_text and _filter_term_exact_in_text(negated_text, str(term)):
            continue
        if _filter_term_allowed(str(term), scope, strength, message):
            _filter_add_term(out["exclude"], seen_e, str(term))
    if not out["include"]:
        out["entities"] = []
        out["entity_labels"] = []
    if not out["exclude"]:
        out["exclude_entities"] = []
    return out


def _filter_clean_term(term: str) -> str:
    import re

    raw = str(term or "").strip().strip(".,:;\"'()[]{}")
    raw = re.sub(r"^https?://[^/]+/?", "", raw, flags=re.I).strip("/")
    if "/" in raw:
        return raw.strip("/")
    clean = _filter_norm(raw).strip().strip("/.,:;\"'()[]{}")
    clean = re.sub(r"\s+", "-", clean)
    clean = clean.replace("gpuaa-s", "gpuaas")
    clean = clean.replace("mlops-kit", "mlopskit")
    clean = clean.replace("h200-quickstart", "h200quickstart")
    return clean


def _filter_add_term(out: list[str], seen: set[str], term: str):
    term = _filter_clean_term(term)
    key = term.lower()
    if not term or key in seen:
        return
    if _filter_is_stop_term(term) or term.isdigit():
        return
    if len(term) < 2:
        return
    seen.add(key)
    out.append(term)


def _extract_filter_terms(phrase: str) -> list[str]:
    import re

    out: list[str] = []
    seen: set[str] = set()
    scrubbed = phrase or ""

    for url in re.findall(r"https?://[^\s'\"<>)]+", phrase or "", re.I):
        _filter_add_term(out, seen, url)
        scrubbed = scrubbed.replace(url, " ")
    for path in re.findall(r"(?<![a-z0-9_\-/.])[\w-]+/[\w\-/]+", scrubbed, re.I):
        _filter_add_term(out, seen, path)
        scrubbed = scrubbed.replace(path, " ")
    for path in re.findall(r"/[\w\-/]+", scrubbed):
        _filter_add_term(out, seen, path)
        scrubbed = scrubbed.replace(path, " ")

    norm = _filter_norm(scrubbed)
    norm = re.sub(r"\bcase\s+study\b", " ", norm)
    norm = re.sub(r"https?://\S+", " ", norm)
    norm = re.sub(r"(?<![a-z0-9_\-/])[\w-]+/[\w\-/]+", " ", norm)
    norm = re.sub(r"/[\w\-/]+", " ", norm)
    norm = re.sub(r"\b(?:phai chua ca|chua ca|match all|tat ca keyword)\b", " ", norm)
    norm = re.sub(r"20\d{2}-\d{1,2}(?:-\d{1,2})?", " ", norm)
    norm = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", norm)
    for word in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", norm):
        _filter_add_term(out, seen, word)
    return out[:10]


def _parse_filter_spec(message: str, default_scope: str = "url") -> dict:
    """Parse filter chung: include/exclude nhiều keyword, OR mặc định, ALL khi user nói rõ."""
    import re

    raw = message or ""
    norm = _filter_norm(raw)
    url_cue = (
        _has_norm_phrase(raw, ("url", "trang", "page", "path", "landing", "ldp", "duong dan", "đường dẫn", "seo"))
        or bool(re.search(r"https?://|(?<![a-z0-9])\/[\w\-/]+", raw, re.I))
    )
    campaign_cue = _has_norm_phrase(raw, ("campaign", "chien dich", "chiến dịch"))
    scope = "campaign" if default_scope == "campaign" and campaign_cue and not url_cue else default_scope
    match_mode = _filter_match_mode(raw)

    # Nếu không có dấu hiệu filter rõ ràng, chỉ lấy term khi message có path/url cụ thể.
    has_filter_context = _has_filter_context(raw)
    if not has_filter_context:
        return {"scope": scope, "include": [], "exclude": [], "match_mode": match_mode}

    exc_re = r"(?i)\b(?:tru|trừ|ngoai\s+tru|ngoại\s+trừ|loai\s+tru|loại\s+trừ|khong\s+gom|không\s+gồm|exclude|except)\b"
    exc_split = re.split(exc_re, raw, maxsplit=1)
    include_phrase = exc_split[0]
    exclude_phrase = exc_split[1] if len(exc_split) > 1 else ""

    # Ưu tiên phần sau cue filter để tránh nhặt nhầm động từ ở đầu câu.
    cue_re = r"(?i)\b(?:url|urls|trang|page|pages|path|landing\s+page|ldp|duong\s+dan|đường\s+dẫn|campaign|campaigns|chien\s+dich|chiến\s+dịch)\b"
    parts = re.split(cue_re, include_phrase, maxsplit=1)
    if len(parts) > 1:
        include_phrase = parts[1]
    include = _extract_filter_terms(include_phrase)
    exclude = _extract_filter_terms(exclude_phrase)
    if exclude_phrase and re.search(r"(?i)\bdrop\s+table\b", exclude_phrase):
        exclude.append("drop-table")

    return _sanitize_filter_spec(
        {"scope": scope, "include": include, "exclude": exclude, "match_mode": match_mode},
        message,
        default_scope,
    )


def _merge_filter_terms(spec: dict, *, include=None, exclude=None, scope: str | None = None) -> dict:
    out = {
        "scope": scope or spec.get("scope") or "url",
        "include": list(spec.get("include") or []),
        "exclude": list(spec.get("exclude") or []),
        "match_mode": spec.get("match_mode") or "any",
        "entities": list(spec.get("entities") or []),
        "entity_labels": list(spec.get("entity_labels") or []),
        "exclude_entities": list(spec.get("exclude_entities") or []),
    }
    seen_i = set(out["include"])
    seen_e = set(out["exclude"])
    for term in include or []:
        _filter_add_term(out["include"], seen_i, str(term))
    for term in exclude or []:
        _filter_add_term(out["exclude"], seen_e, str(term))
    return out


def _merge_filter_specs(base: dict, extra: dict) -> dict:
    base = {
        "scope": (base or {}).get("scope") or (extra or {}).get("scope") or "url",
        "include": list((base or {}).get("include") or []),
        "exclude": list((base or {}).get("exclude") or []),
        "match_mode": (base or {}).get("match_mode") or "any",
        "entities": list((base or {}).get("entities") or []),
        "entity_labels": list((base or {}).get("entity_labels") or []),
        "exclude_entities": list((base or {}).get("exclude_entities") or []),
    }
    forced_include: list[str] = []
    known_l = {k.lower() for k in _known_filter_terms((base or {}).get("scope") or (extra or {}).get("scope") or "url")}
    for term in (extra or {}).get("include") or []:
        clean = _filter_clean_term(str(term))
        if clean and "/" not in clean and "-" in clean and clean.lower() not in known_l:
            before = len(base["include"])
            base["include"] = [
                existing for existing in base["include"]
                if not str(existing).lower().endswith("/" + clean.lower())
            ]
            if len(base["include"]) != before:
                forced_include.append(str(term))
    include = [
        t for t in ((extra or {}).get("include") or [])
        if str(t) in forced_include
        or not _filter_term_fragment_of_spec(
            str(t),
            base,
            "include",
            use_labels=True,
        )
    ]
    exclude = [
        t for t in ((extra or {}).get("exclude") or [])
        if not _filter_term_fragment_of_spec(str(t), base, "exclude")
    ]
    out = _merge_filter_terms(
        base,
        include=include,
        exclude=exclude,
        scope=(base or {}).get("scope") or (extra or {}).get("scope"),
    )
    if (extra or {}).get("match_mode") == "all":
        out["match_mode"] = "all"
    return out


def _filter_term_position(message: str, term: str) -> int:
    import re

    hay_norm = _filter_norm(message)
    hay_sep = _filter_sep_norm(message)
    clean = _filter_clean_term(term)
    if "/" in clean:
        full_parts = [re.escape(p) for p in re.split(r"[-_\s/]+", clean.strip("/").lower()) if p]
        if full_parts:
            full_needle = r"[-_\s/]+".join(full_parts)
            for hay in (hay_norm, hay_sep):
                m = re.search(rf"(?<![a-z0-9_\-/]){full_needle}(?![a-z0-9_\-/])", hay.lower())
                if m:
                    return m.start()
    candidates = [clean, clean.lower()]
    if "/" in clean:
        candidates.append(clean.rsplit("/", 1)[-1])
    candidates.extend([
        clean.replace("-", " "),
        clean.replace("_", " "),
        clean.replace("/", " "),
        re.sub(r"[^a-z0-9]+", "", clean.lower()),
    ])
    best = 10**9
    for cand in candidates:
        cand = str(cand or "").strip()
        if not cand:
            continue
        parts = [re.escape(p) for p in re.split(r"[-_\s/]+", cand.lower()) if p]
        if parts:
            needle = r"[-_\s/]+".join(parts)
            for hay in (hay_norm, hay_sep):
                m = re.search(rf"(?<![a-z0-9_\-/]){needle}(?![a-z0-9_\-/])", hay.lower())
                if m:
                    best = min(best, m.start())
        compact_cand = re.sub(r"[^a-z0-9]+", "", cand.lower())
        if len(compact_cand) >= 6:
            compact_hay = re.sub(r"[^a-z0-9]+", "", hay_sep.lower())
            idx = compact_hay.find(compact_cand)
            if idx >= 0:
                best = min(best, idx)
    if best == 10**9:
        try:
            ents = entity_resolver.catalog(runtime_config.current().get("urls") or [])
        except Exception:  # noqa: BLE001
            ents = []
        clean_l = clean.lower()
        for ent in ents:
            pattern_hits = {
                _filter_clean_term(str(raw or "")).lower()
                for raw in ent.get("patterns") or []
                if str(raw or "").strip()
            }
            if clean_l not in pattern_hits:
                continue
            for raw in [ent.get("label"), *(ent.get("aliases") or []), *(ent.get("patterns") or [])]:
                alias = _filter_clean_term(str(raw or ""))
                if not alias or alias.lower() == clean_l:
                    continue
                parts = [re.escape(p) for p in re.split(r"[-_\s/]+", alias.lower()) if p]
                if parts:
                    needle = r"[-_\s/]+".join(parts)
                    for hay in (hay_norm, hay_sep):
                        m = re.search(rf"(?<![a-z0-9_\-/]){needle}(?![a-z0-9_\-/])", hay.lower())
                        if m:
                            best = min(best, m.start())
                compact_alias = re.sub(r"[^a-z0-9]+", "", alias.lower())
                if len(compact_alias) >= 6:
                    compact_hay = re.sub(r"[^a-z0-9]+", "", hay_sep.lower())
                    idx = compact_hay.find(compact_alias)
                    if idx >= 0:
                        best = min(best, idx)
    return best


def _filter_order_spec_terms(spec: dict, message: str) -> dict:
    out = dict(spec or {})
    should_order = bool(
        out.get("match_mode") == "all"
        or re_search_camel_or_path(message)
    )
    if not should_order:
        return out
    for key in ("include", "exclude"):
        vals = list(out.get(key) or [])
        out[key] = sorted(enumerate(vals), key=lambda item: (_filter_term_position(message, item[1]), item[0]))
        out[key] = [v for _, v in out[key]]
    return out


def _filter_focus_message(message: str, default_scope: str) -> str:
    """Prefer the metric/action clause when a user mixes context and analysis.

    This keeps "GPUaaS là gì ... nhưng xem traffic CloudGPUX" focused on
    CloudGPUX, while leaving normal multi-term/exclude requests untouched.
    """
    import re

    raw = message or ""
    if not raw.strip():
        return raw
    if _filter_request_suppressed(raw):
        return raw
    if not (_message_has_knowledge_cue(raw) and _message_has_filter_analysis_cue(raw, default_scope)):
        return raw

    clauses = [
        c.strip(" \t\r\n,;:.")
        for c in re.split(
            r"(?i)(?:[.;\n]|,\s*|\b(?:nhung|nhưng|sau\s+do|sau\s+đó|con|còn|nua\s+sau|nửa\s+sau|phan\s+cuoi|phần\s+cuối|doan\s+sau|đoạn\s+sau|roi|rồi)\b)",
            raw,
        )
        if c and c.strip(" \t\r\n,;:.")
    ]
    if len(clauses) <= 1:
        return raw

    def score_clause(clause: str, idx: int) -> tuple[int, int, int]:
        score = 0
        has_analysis = _message_has_filter_analysis_cue(clause, default_scope)
        has_knowledge = _message_has_knowledge_cue(clause)
        if has_analysis:
            score += 5
        if _filter_context_strength(clause) == "explicit":
            score += 2
        try:
            ent = entity_resolver.resolve(clause, default_scope)
            if ent.get("scope") == default_scope and _filter_active(ent):
                score += 4
        except Exception:  # noqa: BLE001
            pass
        if _message_has_noise_trap(clause) and not has_analysis:
            score -= 3
        if has_knowledge and not has_analysis:
            score -= 5
        elif has_knowledge:
            score -= 1
        return score, len(clause), -idx

    best_idx, best_clause = max(enumerate(clauses), key=lambda item: score_clause(item[1], item[0]))
    if score_clause(best_clause, best_idx)[0] >= 4:
        return best_clause
    return raw


def re_search_camel_or_path(message: str) -> bool:
    import re

    raw = message or ""
    return bool(
        re.search(r"https?://|(?<![a-z0-9])/?[\w-]+/[\w\-/]+", raw, re.I)
        or re.search(r"[a-z][A-Z]|[0-9][A-Z]", raw)
        or re.search(r"[A-Z]{2,}[a-z]|[A-Za-z]+[0-9]", raw)
        or re.search(r"[,;]", raw)
    )


def _filter_term_fragment_of_spec(term: str, spec: dict, side: str, *, use_labels: bool = True) -> bool:
    import re

    clean = _filter_clean_term(term)
    if not clean:
        return True
    scope = spec.get("scope") or "url"
    known = _known_filter_terms(scope)
    known_l = {k.lower() for k in known}
    clean_l = clean.lower()
    if use_labels and (clean_l in known_l or any(k.lower().endswith("/" + clean_l) for k in known)):
        return True
    surfaces = list(spec.get(side) or [])
    if side == "include" and use_labels:
        surfaces.extend(spec.get("entity_labels") or [])
    else:
        surfaces.extend(spec.get("exclude_entities") or [])
    for surface in surfaces:
        candidate = _filter_clean_term(str(surface or ""))
        if not candidate:
            continue
        clean_sig = re.sub(r"[-_\s]+", "-", clean)
        candidate_sig = re.sub(r"[-_\s]+", "-", candidate)
        if candidate == clean:
            continue
        clean_compact = re.sub(r"[^a-z0-9]+", "", clean.lower())
        candidate_last_compact = re.sub(r"[^a-z0-9]+", "", candidate.rsplit("/", 1)[-1].lower())
        if clean_compact and clean_compact == candidate_last_compact:
            return True
        if clean_sig == candidate_sig:
            return True
        if re.search(r"\d", clean) and re.search(rf"(?<![a-z0-9]){re.escape(clean)}(?![a-z0-9])", candidate):
            return True
        if len(clean) < len(candidate) and re.search(rf"(?<![a-z0-9]){re.escape(clean)}(?![a-z0-9])", candidate):
            if clean == "case" and candidate.startswith("case-study"):
                continue
            return True
        if len(candidate) < len(clean) and re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", clean):
            return True
    return False


def _filter_spec_from_action(data: dict, message: str, default_scope: str = "url") -> dict:
    import re

    match_mode = _filter_match_mode(message)
    focus_message = _filter_focus_message(message, default_scope)
    if _filter_request_suppressed(message) or not _filter_natural_terms_allowed(focus_message, default_scope):
        return _empty_filter_spec(default_scope, match_mode)

    entity_spec = entity_resolver.resolve(focus_message, default_scope)
    if entity_spec.get("scope") != default_scope:
        entity_spec = _empty_filter_spec(default_scope, match_mode)
    if not _filter_entity_spec_safe(entity_spec, message, default_scope):
        entity_spec = _empty_filter_spec(default_scope, match_mode)
    parsed_spec = _parse_filter_spec(focus_message, default_scope)
    if parsed_spec.get("scope") != default_scope:
        parsed_spec = _empty_filter_spec(default_scope, match_mode)
    raw = data.get("filter_spec") if isinstance(data, dict) else None
    raw_spec = {"scope": default_scope, "include": [], "exclude": [], "match_mode": "any"}
    if isinstance(raw, dict):
        raw_spec = {
            "scope": raw.get("scope") or default_scope,
            "include": [],
            "exclude": [],
            "match_mode": match_mode,
        }
        raw_spec = _merge_filter_terms(
            raw_spec,
            include=raw.get("include") or raw.get("url_terms") or raw.get("terms") or [],
            exclude=raw.get("exclude") or raw.get("exclude_url_terms") or [],
        )
        raw_spec = _sanitize_filter_spec(raw_spec, focus_message, default_scope, require_exact_terms=True)
        if raw_spec.get("scope") != default_scope:
            raw_spec = _empty_filter_spec(default_scope, match_mode)

    prefer_keyword_terms = match_mode == "all" and _filter_active(parsed_spec) and (
        _has_norm_phrase(message, ("tat ca keyword",))
        or _has_raw_phrase(message, ("tất cả keyword",))
        or (_has_norm_phrase(message, ("match all",)) and not re.search(r"[A-Z]", message or ""))
    )

    if prefer_keyword_terms:
        spec = parsed_spec
    elif _filter_active(entity_spec):
        spec = entity_spec
        if _filter_context_strength(message) == "explicit" and _filter_active(parsed_spec):
            spec = _merge_filter_specs(spec, parsed_spec)
        if _filter_context_strength(message) == "explicit" and _filter_active(raw_spec):
            spec = _merge_filter_specs(spec, raw_spec)
    elif _filter_active(raw_spec):
        spec = raw_spec
    else:
        spec = parsed_spec

    if isinstance(data, dict):
        extra = {"scope": spec.get("scope") or default_scope, "include": [], "exclude": [], "match_mode": spec.get("match_mode") or "any"}
        if data.get("url_contains"):
            extra["include"].append(data.get("url_contains"))
        extra["include"].extend(data.get("url_terms") or [])
        if data.get("campaign_terms"):
            extra["scope"] = "campaign"
            extra["include"].extend(data.get("campaign_terms") or [])
        extra = _sanitize_filter_spec(extra, focus_message, default_scope, require_exact_terms=True)
        if _filter_active(extra):
            spec = _merge_filter_specs(spec, extra)
    if match_mode == "all" and _filter_active(parsed_spec):
        lowered_message = message or ""
        for term in parsed_spec.get("include") or []:
            clean = _filter_clean_term(str(term))
            if "/" in clean or "-" not in clean:
                continue
            if not re.search(rf"(?<![A-Za-z0-9_\-/]){re.escape(clean)}(?![A-Za-z0-9_\-/])", lowered_message):
                continue
            spec["include"] = [
                clean if str(existing).lower().endswith("/" + clean.lower()) else existing
                for existing in (spec.get("include") or [])
            ]
    spec["match_mode"] = match_mode if match_mode == "all" else "any"
    return _filter_order_spec_terms(spec, focus_message)


def _filter_active(spec: dict) -> bool:
    return bool((spec or {}).get("include") or (spec or {}).get("exclude"))


def _filter_text_matches(text: str, spec: dict) -> bool:
    hay = _filter_norm(text)
    inc = [_filter_clean_term(str(t)).lower() for t in (spec.get("include") or []) if t]
    exc = [_filter_clean_term(str(t)).lower() for t in (spec.get("exclude") or []) if t]
    if exc and any(t in hay for t in exc):
        return False
    if not inc:
        return True
    if spec.get("match_mode") == "all":
        return all(t in hay for t in inc)
    return any(t in hay for t in inc)


def _filter_desc(spec: dict, label: str | None = None) -> str:
    if not _filter_active(spec):
        return ""
    label = label or ("campaign" if spec.get("scope") == "campaign" else "URL")
    joiner = " và " if spec.get("match_mode") == "all" else " hoặc "
    bits = []
    if spec.get("include"):
        names = spec.get("entity_labels") or spec["include"]
        bits.append(f"{label} chứa " + joiner.join(names))
    if spec.get("exclude"):
        bits.append("trừ " + " hoặc ".join(spec["exclude"]))
    return "; ".join(bits)


def _filter_rows_by_text(rows: list, text_fn, spec: dict) -> list:
    if not _filter_active(spec):
        return rows
    return [r for r in rows if _filter_text_matches(text_fn(r), spec)]


def _filter_table_rows(headers: list, rows: list, spec: dict, *, fallback_idx: int = 1) -> list:
    if not _filter_active(spec):
        return rows
    if spec.get("scope") == "campaign":
        names = ("campaign", "name")
    else:
        names = ("url", "path", "page", "landing")
    idx = next((i for i, h in enumerate(headers) if any(n in str(h).lower() for n in names)),
               fallback_idx if len(headers) > fallback_idx else 0)
    return _filter_rows_by_text(rows, lambda r: str(r[idx] if idx < len(r) else ""), spec)


def _filter_dict_data(data: dict, fields: tuple[str, ...], spec: dict) -> dict:
    if not _filter_active(spec):
        return data
    out = dict(data)
    rows = data.get("rows") or []
    out["rows"] = _filter_rows_by_text(rows, lambda r: " ".join(str(r.get(f, "")) for f in fields), spec)
    return out


def _filter_regex_for_seo(spec: dict) -> str:
    import re

    if not _filter_active(spec) or spec.get("scope") != "url":
        return ""
    inc = [re.escape(t) for t in spec.get("include") or []]
    exc = [re.escape(t) for t in spec.get("exclude") or []]
    parts = ["^"]
    if exc:
        parts.append(f"(?!.*(?:{'|'.join(exc)}))")
    if inc:
        if spec.get("match_mode") == "all":
            parts.extend(f"(?=.*{t})" for t in inc)
        else:
            parts.append(f"(?=.*(?:{'|'.join(inc)}))")
    parts.append(".*")
    return "".join(parts)


def _parse_psi_url_terms(message: str) -> list[str]:
    """Backward-compatible wrapper cho PageSpeed URL terms."""
    return _parse_filter_spec(message, "url").get("include") or []


def _filter_psi_rows(headers: list, rows: list, terms: list[str] | dict) -> tuple[list, list[str]]:
    if isinstance(terms, dict):
        spec = terms
    else:
        spec = {"scope": "url", "include": [t.strip().strip("/").lower() for t in terms if str(t).strip()],
                "exclude": [], "match_mode": "any"}
    if not _filter_active(spec):
        return rows, []
    filtered = _filter_table_rows(headers, rows, spec, fallback_idx=1)
    used = list(spec.get("include") or [])
    return filtered, used


def _all_keyword_intent(message: str) -> dict | None:
    import re as _re2

    m = message.lower()
    if m.strip() in ("ok", "oke", "okay", "đồng ý", "dong y", "chạy đi", "chay di", "xác nhận", "xac nhan", "confirm", "yes", "lgtm"):
        return {"action": "confirm"}
    if _looks_comparison_period_question(message):
        return None
    _health = any(k in m for k in ("ổn không", "on khong", "tốt không", "tot khong", "khoẻ không", "khoe khong",
                                   "tình hình", "tinh hinh", "thế nào", "the nao", "ra sao", "ổn chứ", "hiệu quả"))
    # Insight xuyên mảng
    if any(k in m for k in ("conversion tracking", "tracking conversion", "audit tracking", "tracking audit",
                            "đo conversion", "do conversion", "thiếu tracking", "thieu tracking",
                            "tracking lỗi", "tracking loi", "gtm", "conversion action", "event ga4", "ga4 event")):
        dm = _re2.search(r"(\d{1,2})\s*ngày", m)
        return {"action": "tracking_audit", "days": int(dm.group(1)) if dm else 30}
    if any(k in m for k in ("experiment", "thử nghiệm", "thu nghiem", "a/b", "ab test",
                            "baseline", "success metric", "rollback", "đo sau khi", "do sau khi",
                            "kế hoạch đo", "ke hoach do")):
        return {"action": "experiment_plan"}
    if any(k in m for k in ("alert", "alerts", "cảnh báo", "canh bao", "bất thường", "bat thuong", "monitor")):
        return {"action": "alerts"}
    if any(k in m for k in ("weekly autopilot", "báo cáo tuần", "bao cao tuan", "top việc tuần", "top viec tuan",
                            "weekly report")):
        return {"action": "weekly_autopilot"}
    if any(k in m for k in ("ưu tiên", "uu tien", "nên tối ưu", "nen toi uu", "fix trước", "fix truoc", "trang nào nên")):
        return {"action": "priority_fix"}
    if any(k in m for k in ("nên làm gì", "nen lam gi", "kế hoạch", "ke hoach", "to-do", "todo", "việc tuần", "cần làm gì", "can lam gi", "ưu tiên công việc")):
        return {"action": "action_plan"}
    if (any(k in m for k in ("tại sao", "tai sao", "vì sao", "vi sao", "nguyên nhân", "nguyen nhan", "kéo xuống", "root cause"))
            and any(k in m for k in ("giảm", "giam", "tụt", "tut", "drop", "rớt", "rot", "không convert", "khong convert", "conversion kém", "conversion kem", "khong co conversion", "không có conversion"))):
        return {"action": "diagnose_drop"}
    if (any(k in m for k in ("tăng tốc", "tang toc", "speed up"))
            or (any(k in m for k in ("làm sao", "lam sao", "cách", "cach", "fix", "sửa", "sua", "tối ưu", "toi uu"))
                and any(k in m for k in ("chậm", "cham", "lcp", "cls", "tbt", "pagespeed", "tốc độ", "toc do", "tốc", "toc")))):
        uc = _parse_url_contains(message)
        return {"action": "fix_suggest", **({"url": uc} if uc else {})}
    # Hỏi về năng lực / hướng dẫn → help (grounded)
    if (any(k in m for k in ("làm được gì", "lam duoc gi", "làm được những gì", "giúp được gì", "giup duoc gi",
                             "tính năng", "tinh nang", "chức năng", "chuc nang", "hướng dẫn", "huong dan",
                             "dùng như nào", "dùng sao", "dung sao", "sử dụng", "su dung", "có thể làm",
                             "bạn là ai", "ban la ai", "decho là gì", "decho la gi", "menu nào", "ở đâu"))
            or any(p in m for p in ("làm sao", "lam sao", "làm thế nào", "lam the nao", "cách "))
            or ((any(p in m for p in ("là gì", "la gi", "bao nhiêu là", "ngưỡng", "thế nào là tốt", "nghĩa là gì"))
                 and any(k in m for k in ("lcp", "cls", "fcp", "tbt", "inp", "score", "điểm", "impression",
                 "clicks", "views", "users", "ctr", "cpa", "core web", "web vitals"))))):
        return {"action": "help"}
    # Đọc URL cụ thể khi user dán link + có ý "đọc/tóm tắt/xem"
    import re as _ref2
    mu = _ref2.search(r"https?://\S+", message)
    if mu and any(k in m for k in ("đọc", "doc", "tóm tắt", "tom tat", "xem", "phân tích trang", "nội dung", "noi dung", "bài này", "trang này", "link này")):
        return {"action": "web_fetch", "url": mu.group(0)}
    # Tìm web — chỉ khi có ý định tìm kiếm rõ ràng
    if any(k in m for k in ("tìm trên mạng", "tìm trên web", "search web", "google giúp",
                            "tra google", "tin mới nhất về", "cập nhật mới nhất về", "tra cứu trên mạng",
                            "lên mạng tìm", "tìm giúp trên internet")):
        import re as _re4
        q = _re4.sub(r"(?i)(tìm trên mạng|tìm trên web|search web|google giúp|tra google|tra cứu trên mạng|lên mạng tìm|tìm giúp trên internet|giúp tôi|giúp đệ|cho tôi)", "", message).strip(" :,")
        return {"action": "web_search", "query": q or message}
    rng = _parse_range_vi(m)
    rng_psi_context = any(k in m for k in ("pagespeed", "lcp", "cls", "fcp", "tbt", "inp", "web vitals", "score", "điểm"))
    if rng and any(k in m for k in ("seo", "traffic", "báo cáo", "bao cao", "clicks", "gsc", "ga4")) and not rng_psi_context:
        uc = _parse_url_contains(message)
        return {**rng, **({"url_contains": uc} if uc else {})}
    if any(k in m for k in ("landing page", "landing", "trang đích", "trang dich", "ldp")):
        if rng:
            return {"action": "ldp_perf", "start": rng["start"], "end": rng["end"]}
        dm = _re2.search(r"(\d{1,2})\s*ngày", m)
        return {"action": "ldp_perf", "days": int(dm.group(1)) if dm else 7}
    if any(k in m for k in ("ads", "campaign", "quảng cáo", "cpa", "chi tiêu", "spend", "ngân sách")):
        if ((any(k in m for k in ("danh sách", "list", "những campaign", "campaign nào đang", "liệt kê"))
                or _has_norm_phrase(message, ("campaign đang active", "campaign active", "active campaign")))
                and not _health):
            return {"action": "ads_list"}
        if rng:  # khoảng thời gian tự nhiên ("tháng 5", "quý 1", "từ đầu năm"...) → lọc ads theo range
            return {"action": "ads_perf", "start": rng["start"], "end": rng["end"]}
        tm = _re2.search(r"tháng\s*(\d{1,2})(?:\s*[/\-]?\s*(?:năm\s*)?(20\d{2}))?", m)
        if tm:
            from datetime import date, timedelta

            from dateutil.relativedelta import relativedelta
            mo, yr = int(tm.group(1)), int(tm.group(2) or app_time.now().year)
            if 1 <= mo <= 12:
                s = date(yr, mo, 1)
                return {"action": "ads_perf", "start": s.isoformat(),
                        "end": (s + relativedelta(months=1) - timedelta(days=1)).isoformat()}
        dm = _re2.search(r"(\d{1,2})\s*ngày", m)
        return {"action": "ads_perf", "days": int(dm.group(1)) if dm else 7}
    psiish = any(k in m for k in ("lcp", "cls", "fcp", "tbt", "inp", "ttfb", "score", "điểm",
                                  "pagespeed", "kiểm tra", "web vitals", "chậm", "nhanh"))
    psi_run = any(k in m for k in _PSI_RUN_CUES)
    psi_report = any(k in m for k in _PSI_REPORT_CUES)
    if psiish and psi_report and not psi_run:
        data = {"action": "query_results"}
        terms = _parse_psi_url_terms(message)
        if terms:
            data["url_terms"] = terms
        return data
    # "SEO/traffic ổn không, tình hình SEO thế nào" → phân tích số liệu SEO
    if _health and any(k in m for k in ("seo", "traffic", "clicks", "gsc", "ga4", "impression", "views")):
        return {"action": "seo_query"}
    # "pagespeed/web ổn không, tốt không, tình hình thế nào" → phân tích điểm thực tế
    if _health and any(k in m for k in ("pagespeed", "web", "trang", "tốc độ")):
        return {"action": "query_results"}
    seoish = any(k in m for k in ("seo", "traffic", "clicks", "gsc", "ga4", "impression",
                                  "views", "users")) or (any(k in m for k in ("báo cáo", "bao cao")) and not psiish)
    kw_seo = _seo_keyword_intent(message)
    if kw_seo and kw_seo.get("action") == "query_data":
        kw_seo = {**kw_seo, "action": "seo_query"}
    kw_psi = _keyword_intent(message)
    if psiish and not seoish:
        # "pagespeed tháng N (năm Y)" → phân nhánh quá khứ / tương lai / mơ hồ
        tm = _re2.search(r"tháng\s*(\d{1,2})(?:\s*[/\-]?\s*(?:năm\s*)?(20\d{2}))?", m)
        if tm:
            from datetime import date
            mo, yr = int(tm.group(1)), tm.group(2) and int(tm.group(2))
            today = app_time.today()
            if 1 <= mo <= 12:
                if yr is None and mo > today.month:  # không rõ năm mà tháng chưa tới → hỏi lại
                    return {"action": "reply",
                            "text": f"Tháng {mo} mà chưa rõ năm nào đó Đại ca — ý Đại ca là {mo:02d}/{today.year - 1} (đã qua) hay {mo:02d}/{today.year} (chưa tới)? Nói rõ giúp Đệ nha."}
                yr = yr or today.year
                if (yr, mo) > (today.year, today.month):
                    return {"action": "reply",
                            "text": f"Tháng {mo:02d}/{yr} còn chưa tới mà Đại ca 😅 Đệ đo PageSpeed real-time chứ chưa biết du hành thời gian. Muốn thì Đệ chạy kiểm tra NGAY bây giờ, hoặc xem lại data các tháng đã đo nha."}
                if (yr, mo) < (today.year, today.month):
                    return {"action": "query_results", "month": f"{yr}-{mo:02d}"}
                return {"action": "run_check"}  # đúng tháng hiện tại
        # câu hỏi metric PSI nhưng _keyword_intent không bắt được → ép query_results
        if kw_psi and kw_psi.get("action") != "run_check":
            return kw_psi
        if psi_run:
            return {"action": "run_check"}
        terms = _parse_psi_url_terms(message)
        return {"action": "query_results", **({"url_terms": terms} if terms else {})}
    if seoish:
        return kw_seo or kw_psi
    return kw_psi or kw_seo


def _norm_identity_text(text: str) -> str:
    import re
    import unicodedata

    raw = unicodedata.normalize("NFD", text or "")
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", raw.lower())).strip()


def _identity_query_name(message: str) -> str | None:
    """Bắt câu hỏi high-risk kiểu 'X là ai' để không để model tự suy diễn danh tính."""
    import re

    raw = (message or "").strip()
    if not raw:
        return None
    patterns = (
        r"^\s*(?:anh|chị|chi|ông|ong|bà|ba|cô|co|chú|chu|mr\.?|ms\.?)?\s*([^?!.\n]{2,80}?)\s+(?:là|la)\s+ai\s*[?!.]*\s*$",
        r"^\s*(?:có\s+biết|co\s+biet|biết|biet|nhớ|nho)\s+(?:anh|chị|chi|ông|ong|bà|ba|cô|co|chú|chu)?\s*([^?!.\n]{2,80}?)\s+(?:không|khong|ko|hông|hong|chưa|chua)\s*[?!.]*\s*$",
    )
    for pat in patterns:
        m = re.search(pat, raw, re.I)
        if not m:
            continue
        name = re.sub(r"^(anh|chị|chi|ông|ong|bà|ba|cô|co|chú|chu|mr\.?|ms\.?)\s+", "", m.group(1).strip(), flags=re.I)
        norm = _norm_identity_text(name)
        blocked = {
            "ban", "ban la", "de", "decho", "dai ca", "toi", "minh", "agent", "app",
            "seo", "psi", "pagespeed", "lcp", "cls", "fcp", "tbt", "ctr", "cpa", "ads", "ga4", "gsc",
        }
        if norm and norm not in blocked and len(norm) >= 2:
            return name
    return None


def _identity_fact_matches(facts: list[str], name: str, *, trusted_only: bool = True) -> list[str]:
    target = _norm_identity_text(name)
    out, seen = [], set()
    for fact in facts or []:
        text = str(fact or "").strip()
        norm = _norm_identity_text(text)
        if not text or target not in norm:
            continue
        if trusted_only and not any(tag in norm for tag in ("ghi nho", "fact nguoi dung xac nhan", "user cung cap", "nguoi dung xac nhan")):
            continue
        key = norm[:220]
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out[:3]


def _identity_memory_answer(name: str, matches: list[str]) -> str:
    if matches:
        ev = "\n".join(f"- {m}" for m in matches)
        return (f"Theo fact **đã được Đại ca ghi nhớ rõ ràng** trong memory, Đệ có dữ kiện về **{name}**:\n"
                f"{ev}\n\nNếu fact này sai, Đại ca vào **Cấu hình → Đệ nhớ gì về Đại ca → Reset trí nhớ** rồi ghi nhớ lại fact đúng.")
    return (f"Đệ chưa có fact đáng tin về **{name}** trong memory, nên Đệ không dám gán bừa là founder/đối tác/người quen của Đại ca.\n\n"
            f"Nếu muốn lưu đúng: gõ **ghi nhớ: {name} là ...**. Nếu muốn tra ngoài memory, bảo Đệ **tìm trên web {name}**.")


@app.post("/api/agent/chat/stream")
async def agent_chat_stream(req: ChatStreamRequest, request: Request = None):
    """DeCho all-in-one: một chat xử lý mọi action của cả PageSpeed lẫn SEO."""
    import asyncio
    import re as _re

    import httpx

    model = req.model if req.model in ALLOWED_MODELS else MAAS_MODEL

    final_parts: list[str] = []  # gom câu trả lời để ghi AgentBase Memory
    _pend = _pending_for(req.user_id, req.session_id)
    pend_range, pend_op = _pend["range"], _pend["op"]  # đề xuất chờ xác nhận — riêng theo phiên

    async def gen():
        def ev(obj):
            if obj.get("type") == "final" and obj.get("text"):
                obj = {**obj, "text": _repair_response_prefix(str(obj["text"]))}
                final_parts.append(str(obj["text"]))
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        if not (MAAS_API_KEY and MAAS_BASE_URL):
            yield ev({"type": "error", "text": "❌ Chưa cấu hình MAAS_API_KEY / MAAS_BASE_URL."})
            yield ev({"type": "done"})
            return

        history = _sanitize_history(req.history)
        # ── AgentBase Memory: recall fact dài hạn về người dùng ──
        mem_block = ""
        if memory_agent.configured() and req.user_id:
            mem_block = await asyncio.to_thread(memory_agent.memory_block, req.user_id, req.message)
            if mem_block:
                facts = memory_agent.recall_safe(req.user_id, req.message)  # cache hit từ memory_block ở trên
                preview = "; ".join(f[:60] for f in facts[:3])
                yield ev({"type": "step", "text": "🧠 Đệ nhớ: " + (preview or "vài điều về Đại ca")})
        identity_name = _identity_query_name(req.message)
        if identity_name:
            yield ev({"type": "step", "text": f"🧠 Kiểm tra memory theo đúng tên: {identity_name}"})
            facts = []
            if memory_agent.configured() and req.user_id:
                try:
                    recalled = await asyncio.to_thread(memory_agent.recall_safe, req.user_id, identity_name, 8)
                    listed = await asyncio.to_thread(memory_agent.list_records, req.user_id, 80)
                    facts = list(dict.fromkeys([*recalled, *listed]))
                except Exception as e:  # noqa: BLE001
                    log.warning("Memory identity lookup lỗi (bỏ qua): %s: %s", type(e).__name__, e)
            matches = _identity_fact_matches(facts, identity_name, trusted_only=True)
            yield ev({"type": "final", "text": _identity_memory_answer(identity_name, matches)})
            yield ev({"type": "done"})
            return
        gap = _capability_gap_reply(req.message)
        if gap:
            yield ev({"type": "step", "text": "🧭 Kiểm tra năng lực hiện có"})
            yield ev({"type": "final", "text": gap})
            yield ev({"type": "done"})
            return
        relation_reply = _metric_relationship_reply(req.message)
        if relation_reply:
            yield ev({"type": "step", "text": "🧭 Giải thích quan hệ PageSpeed và SEO"})
            yield ev({"type": "final", "text": relation_reply})
            yield ev({"type": "done"})
            return
        insights_reply = _insights_grounded_reply(req.message)
        if insights_reply:
            yield ev({"type": "step", "text": "🧭 Tra cấu trúc dữ liệu Insights"})
            yield ev({"type": "final", "text": insights_reply})
            yield ev({"type": "done"})
            return
        if _looks_comparison_period_question(req.message):
            yield ev({"type": "step", "text": "🧭 Xác định kỳ so sánh từ context"})
            yield ev({"type": "final", "text": await asyncio.to_thread(_comparison_period_reply, history, req.user_id, req.session_id)})
            yield ev({"type": "done"})
            return
        yield ev({"type": "step", "text": f"🧠 Phân tích yêu cầu ({model})..."})
        try:
            data = await _call_llm_stream(model, req.message, history, system=_unified_prompt() + mem_block)
        except Exception as e:  # noqa: BLE001
            yield ev({"type": "error", "text": f"❌ Lỗi gọi model: {e}"})
            yield ev({"type": "done"})
            return
        if data.get("action") == "reply" and str(data.get("text", "")).startswith("❌ Model không trả về nội dung"):
            kw = _all_keyword_intent(req.message)
            if kw:
                yield ev({"type": "step", "text": "↪️ Nhận diện intent bằng keyword"})
                data = kw

        # batch: gom run_report nhiều tháng
        if data.get("action") == "batch":
            months = [{"year": it.get("year"), "month": it.get("month")}
                      for it in data.get("items", []) if it.get("action") == "run_report"]
            if months:
                data = {"action": "run_report", "months": months}
            else:
                data = data["items"][0]
        action = data.get("action", "reply")
        if action == "query_data":  # alias từ prompt SEO cũ
            action = "seo_query"
        data = _validate_action(data)            # chặn action thiếu field → hỏi lại thay vì làm sai
        action = data.get("action", action)
        override = _hard_intent_override(req.message, action)
        if override:
            data = _validate_action(override)
            action = data.get("action", action)
            yield ev({"type": "step", "text": "↪️ Sửa intent bằng keyword an toàn"})
        patched, param_changed = _keyword_param_patch(req.message, data, action)
        if param_changed:
            data = _validate_action(patched)
            action = data.get("action", action)
            yield ev({"type": "step", "text": "↪️ Sửa khoảng thời gian bằng keyword an toàn"})
        # Lưới an toàn: đang có đề xuất chờ xác nhận + user gõ 'ok/oke/chạy đi' → ép confirm (đừng hỏi lại hoài)
        if action != "confirm" and _looks_confirm(req.message) and (pend_range.get("start") or pend_op.get("data")):
            data = {"action": "confirm"}
            action = "confirm"
        # Lưới an toàn: hỏi năng lực/cách dùng → luôn ra help (model hay lười deflect 'đã trả lời rồi')
        if action == "reply" and _looks_help(req.message):
            data = {"action": "help"}
            action = "help"
        # Lưới an toàn web-aware: model lỡ trả 'reply' cho câu rõ ràng cần info ngoài → Đệ tự tra web
        if action == "reply" and _looks_external(req.message):
            data = {"action": "web_search", "query": req.message}
            action = "web_search"
            yield ev({"type": "step", "text": "🌐 Câu này cần info ngoài — Đệ chủ động tra web"})
        log.info("DeCho action=%s | msg=%r", action, (req.message or "")[:120])

        labels = {"run_check": "Chạy kiểm tra PageSpeed", "query_results": "Phân tích kết quả PageSpeed",
                  "list_urls": "Liệt kê URL", "add_url": "Thêm URL", "remove_url": "Xóa URL",
                  "set_schedule": "Đổi lịch PageSpeed", "run_report": "Chạy báo cáo SEO", "seo_range": "Xác định khoảng thời gian SEO", "confirm": "Xác nhận & thực thi",
                  "seo_query": "Phân tích số liệu SEO", "list_months": "Các tháng có báo cáo SEO",
                  "ads_list": "Danh sách campaign Google Ads", "ads_perf": "Phân tích hiệu suất Google Ads",
                  "ldp_perf": "Hiệu suất landing page", "clarity": "Microsoft Clarity (UX)", "combined": "Phân tích tổng hợp Ads + Clarity",
                  "create_campaign": "Tạo campaign (PAUSED, cần mật khẩu)",
                  "status": "Trạng thái hệ thống", "help": "Hướng dẫn năng lực",
                  "web_search": "Tìm trên web", "web_fetch": "Đọc trang web", "alerts": "Alert monitor",
                  "priority_fix": "Ưu tiên tối ưu", "action_plan": "Kế hoạch hành động",
                  "diagnose_drop": "Chẩn đoán sụt giảm", "fix_suggest": "Gợi ý cách sửa",
                  "tracking_audit": "Audit conversion tracking", "experiment_plan": "Experiment planner",
                  "weekly_autopilot": "Weekly Autopilot",
                  "remember": "Ghi nhớ", "reply": "Trả lời"}
        yield ev({"type": "step", "text": f"⚙️ Action: {labels.get(action, action)}"})

        async def stream_analysis(
            system_prompt: str,
            fallback_on_deflect: str | None = None,
            *,
            include_history: bool = True,
            buffer_until_final: bool = False,
            final_transform=None,
        ):
            payload = {"model": model, "stream": True, "temperature": 0.2, "max_tokens": 4096,
                       "messages": _analysis_messages(system_prompt + mem_block, history, req.message, include_history=include_history)}
            think_re = _re.compile(r"<think>.*?(?:</think>|$)", _re.S)
            buffer_until_final = buffer_until_final or fallback_on_deflect is not None
            raw_acc, sent, reasoning_acc = "", 0, []
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream("POST", f"{MAAS_BASE_URL}/chat/completions", json=payload,
                                             headers={"Authorization": f"Bearer {MAAS_API_KEY}"}) as r:
                        if r.status_code != 200:
                            body = (await r.aread()).decode(errors="replace")[:300]
                            yield ev({"type": "error", "text": f"❌ MaaS trả về HTTP {r.status_code}: {body}"})
                            return
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                delta = json.loads(raw)["choices"][0].get("delta", {})
                            except Exception:  # noqa: BLE001
                                continue
                            rc = delta.get("reasoning_content") or delta.get("reasoning")
                            if rc:
                                reasoning_acc.append(rc)
                            c = delta.get("content")
                            if not c:
                                continue
                            raw_acc += c
                            visible = think_re.sub("", raw_acc).lstrip()
                            safe = max(0, len(visible) - 12)
                            if safe > sent and not buffer_until_final:
                                yield ev({"type": "delta", "delta": visible[sent:safe]})
                                sent = safe
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi phân tích: {type(e).__name__}: {e}"})
                return
            visible = think_re.sub("", raw_acc).strip() or think_re.sub("", "".join(reasoning_acc)).strip()
            if visible:
                if fallback_on_deflect and _seo_deflected(visible):
                    visible = fallback_on_deflect
                    sent = 0
                if final_transform:
                    visible = final_transform(visible)
                    sent = 0
                if len(visible) > sent:
                    yield ev({"type": "delta", "delta": visible[sent:]})
                yield ev({"type": "final", "text": visible})
            else:
                yield ev({"type": "error", "text": "❌ Model không trả về nội dung phân tích."})

        # ── Trạng thái tổng hợp ──
        if action == "status":
            yield ev({"type": "final", "text": f"**PageSpeed**: {_status_text()}\n**SEO**: {_seo_status_text()}"})
            yield ev({"type": "done"})
            return

        # ── Hướng dẫn / hỏi về năng lực (grounded — không bịa) ──
        if action == "help":
            yield ev({"type": "step", "text": "📖 Tra năng lực DeCho..."})
            async for chunk in stream_analysis(_help_prompt()):
                yield chunk
            yield ev({"type": "done"})
            return

        # ── Ghi nhớ theo yêu cầu (lưu fact dài hạn ngay) ──
        if action == "remember":
            fact = (str(data.get("fact") or "").strip() or req.message.strip())[:500]
            if memory_agent.configured() and req.user_id and req.session_id:
                yield ev({"type": "step", "text": "🧠 Đệ ghi vào trí nhớ dài hạn..."})
                threading.Thread(target=memory_agent.remember_fact_safe,
                                 args=(req.user_id, req.session_id, fact), daemon=True).start()
                yield ev({"type": "final", "text": f"Rõ! Đệ khắc cốt ghi tâm nha Đại ca: **{fact}** 🧠\n(Vài giây nữa nó sẽ hiện ở mục “Đệ nhớ gì về Đại ca”.)"})
            else:
                yield ev({"type": "final", "text": "Đệ chưa bật được trí nhớ dài hạn (thiếu cấu hình MEMORY_ID) nên chưa lưu lâu dài được. Trong phiên này thì Đệ vẫn nhớ nha Đại ca."})
            yield ev({"type": "done"})
            return

        # ── Tìm trên web (grounded theo kết quả search, có trích nguồn) ──
        if action == "web_search":
            query = str(data.get("query") or req.message).strip()
            yield ev({"type": "step", "text": f"🌐 Tìm web: {query}"})
            results = await asyncio.to_thread(web_search.search, query, 5)
            if not results:
                yield ev({"type": "final", "text": "Đệ tìm web không ra kết quả sau khi thử vài biến thể query. Có thể web search đang bị chặn/timed out; Đại ca thử từ khoá cụ thể hơn hoặc kiểm tra TAVILY_API_KEY/network nhé."})
                yield ev({"type": "done"})
                return
            for r in results[:5]:
                yield ev({"type": "step", "text": f"• {r['title'][:70]}"})
            # Đọc nội dung 3 trang đầu (snippet thường quá ngắn để trả lời chi tiết)
            yield ev({"type": "step", "text": "📖 Đọc nội dung các trang đầu..."})
            top = results[:3]

            async def _fetch(u):
                try:
                    return await asyncio.to_thread(web_search.fetch_url, u, 3000)
                except Exception:  # noqa: BLE001
                    return None
            pages = await asyncio.gather(*[_fetch(r["url"]) for r in top])
            yield ev({"type": "step", "text": f"🧠 Tổng hợp ({model})..."})
            parts = []
            for i, r in enumerate(results):
                blk = f"[{i+1}] {r['title']}\nURL: {r['url']}\n{r['snippet']}"
                if i < len(pages) and pages[i] and pages[i].get("text"):
                    blk += "\nNỘI DUNG TRANG:\n" + pages[i]["text"]
                parts.append(blk)
            src = "\n\n".join(parts)
            prompt = (
                "Người dùng hỏi: " + req.message + "\n\n"
                "KẾT QUẢ TÌM KIẾM + NỘI DUNG TRANG (chỉ dựa trên đây, KHÔNG thêm thông tin ngoài, KHÔNG bịa):\n\n"
                + src +
                "\n\nTrả lời câu hỏi bằng tiếng Việt, súc tích, dựa DUY NHẤT trên dữ liệu trên — "
                "ưu tiên thông tin cụ thể trong 'NỘI DUNG TRANG'. Trích nguồn bằng [số] sau mỗi ý. "
                "Cuối câu trả lời liệt kê 'Nguồn:' kèm URL. "
                "Nếu dữ liệu không đủ để trả lời thì nói thẳng. KHÔNG dùng LaTeX. /no_think"
            ) + _persona()
            async for chunk in stream_analysis(prompt):
                yield chunk
            yield ev({"type": "done"})
            return

        # ── Đọc 1 URL cụ thể (grounded theo nội dung trang) ──
        if action == "web_fetch":
            url = str(data.get("url") or "").strip()
            if not url:
                mu = _re.search(r"https?://\S+", req.message)
                url = mu.group(0) if mu else ""
            if not url:
                yield ev({"type": "final", "text": "Đại ca dán link cụ thể để Đệ đọc nha (vd: https://...)."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"🌐 Đọc trang: {url[:70]}"})
            try:
                page = await asyncio.to_thread(web_search.fetch_url, url, 6000)
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Đọc trang không được: {type(e).__name__}: {e}"})
                yield ev({"type": "done"})
                return
            if not (page.get("text") or "").strip():
                yield ev({"type": "final", "text": "Trang này Đệ tải về nhưng không trích được nội dung (có thể render bằng JS). Đại ca thử URL bài viết trực tiếp nhé."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"🧠 Tổng hợp ({model})..."})
            prompt = (
                "Người dùng yêu cầu: " + req.message + "\n\n"
                f"NỘI DUNG TRANG ĐÃ TẢI ({page.get('title') or url}):\nURL: {url}\n\n"
                + page["text"] +
                "\n\nDựa DUY NHẤT trên nội dung trang trên để trả lời/tóm tắt bằng tiếng Việt, súc tích. "
                "KHÔNG bịa thông tin ngoài trang. Cuối ghi 'Nguồn: " + url + "'. KHÔNG dùng LaTeX. /no_think"
            ) + _persona()
            async for chunk in stream_analysis(prompt):
                yield chunk
            yield ev({"type": "done"})
            return

        # ── Alert monitor hợp nhất: PSI + SEO + Ads + Clarity ──
        if action == "alerts":
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)
            yield ev({"type": "step", "text": "🚨 Đọc alert monitor PSI + SEO + Ads + Clarity..."})
            report = await asyncio.to_thread(_alert_report, 50)
            report = _filter_alert_report(report, spec)
            if desc:
                yield ev({"type": "step", "text": f"🔎 Áp bộ lọc: {desc}"})
            yield ev({"type": "final", "text": _alerts_text(report)})
            yield ev({"type": "done"})
            return

        # ── Weekly Autopilot: read-only weekly operating plan ──
        if action == "weekly_autopilot":
            yield ev({"type": "step", "text": "🧭 Lập Weekly Autopilot từ alerts + opportunity + tracking..."})
            report = await asyncio.to_thread(_weekly_autopilot_report)
            yield ev({"type": "final", "text": _weekly_autopilot_text(report)})
            yield ev({"type": "done"})
            return

        # ── Conversion Tracking Auditor: Ads campaign + landing page conversion signal ──
        if action == "tracking_audit":
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec, "campaign" if spec.get("scope") == "campaign" else "URL")
            days = int(data.get("days") or 30)
            days = max(1, min(days, 90))
            yield ev({"type": "step", "text": f"🧾 Audit conversion tracking {days} ngày gần nhất..."})
            report = await asyncio.to_thread(_conversion_tracking_report, days)
            if _filter_active(spec):
                report = _filter_tracking_report(report, spec)
            if desc:
                yield ev({"type": "step", "text": f"🔎 Áp bộ lọc: {desc}"})
            yield ev({"type": "final", "text": _tracking_audit_text(report)})
            yield ev({"type": "done"})
            return

        # ── Experiment Planner: deterministic plan from structured candidates ──
        if action == "experiment_plan":
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)
            yield ev({"type": "step", "text": "🧪 Lập experiment plan từ opportunity + tracking signals..."})
            report = await asyncio.to_thread(_experiment_report, 10)
            if _filter_active(spec):
                report = _filter_experiment_report(report, spec)
            if desc:
                yield ev({"type": "step", "text": f"🔎 Áp bộ lọc: {desc}"})
            yield ev({"type": "final", "text": _experiments_text(report)})
            yield ev({"type": "done"})
            return

        # ── Root Cause Engine: deterministic hypotheses from observed signals ──
        if action == "diagnose_drop":
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)
            yield ev({"type": "step", "text": "🧭 Gộp root-cause signals từ SEO + PSI + Ads + Clarity + Tracking..."})
            report = await asyncio.to_thread(_root_cause_report, 18)
            if _filter_active(spec):
                report = _filter_root_cause_report(report, spec)
            if desc:
                yield ev({"type": "step", "text": f"🔎 Áp bộ lọc: {desc}"})
            yield ev({"type": "final", "text": _root_cause_text(report)})
            yield ev({"type": "done"})
            return

        # ── Insight xuyên mảng: ưu tiên tối ưu / kế hoạch / chẩn đoán / gợi ý sửa ──
        if action in ("priority_fix", "action_plan", "fix_suggest"):
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)
            yield ev({"type": "step", "text": "📊 Đọc & gộp dữ liệu PSI + SEO + Ads + Clarity..."})
            j = await asyncio.to_thread(_insight_join)
            opp_report = await asyncio.to_thread(_opportunity_report, 25)
            alert_report = await asyncio.to_thread(_alert_report, 20)
            rows = _filter_rows_by_text(j["rows"], lambda r: r.get("path", ""), spec)
            opps = _filter_rows_by_text(opp_report.get("opportunities") or [],
                                        lambda o: " ".join([o.get("path", ""), " ".join(o.get("evidence") or [])]), spec)
            alerts = _filter_alert_report(alert_report, spec).get("alerts") or []
            if not rows and not opps and action not in ("diagnose_drop", "experiment_plan"):
                msg = f"Không có dữ liệu khớp bộ lọc: {desc}." if desc else "Đệ chưa có đủ dữ liệu để phân tích — chạy PageSpeed/SEO và cấu hình Ads/Clarity nếu muốn score đa kênh nha Đại ca."
                yield ev({"type": "final", "text": msg})
                yield ev({"type": "done"})
                return
            fr = lambda v: ("" if v == "" or v is None else str(v))
            ctxhdr = f"(PSI lần chạy {j['run'] or '—'} · SEO tháng {j['month'] or '—'} · Ads/Clarity best-effort" + (f" · {desc}" if desc else "") + ")"
            opp_tbl = "\n".join(
                f"{o['path']} | score {o['score']} | sources {','.join(o.get('sources') or [])} | "
                f"confidence {o.get('confidence')} | evidence {'; '.join((o.get('evidence') or [])[:4])}"
                for o in opps[:18]
            )
            alert_tbl = "\n".join(
                f"[{a.get('lv')}] {a.get('text')} | confidence {a.get('confidence')} | evidence {'; '.join((a.get('evidence') or [])[:2])}"
                for a in alerts[:10]
            ) or "(không có alert đáng kể)"

            if action == "priority_fix":
                tbl = opp_tbl or "(chưa có opportunity đủ tín hiệu)"
                instr = ("Dựa DUY NHẤT trên bảng Opportunity Score dưới, lập DANH SÁCH ƯU TIÊN TỐI ƯU. "
                         "Score đã gộp PSI slowness + SEO drop/traffic + Ads cost/friction + Clarity signal. "
                         "Mỗi mục PHẢI có Evidence và Confidence.")
            elif action == "fix_suggest":
                want = _path_only(str(data.get("url") or "")) if data.get("url") else ""
                sel = [r for r in rows if want and want in r["path"]] if want else []
                if not sel:
                    sel = [r for r in rows if isinstance(r["psiM"], (int, float)) and r["psiM"] < 60]
                    sel.sort(key=lambda r: r["psiM"])
                tbl = "\n".join(f"{r['path']} | PSI mobile {fr(r['psiM'])}/desktop {fr(r['psiD'])} | LCP {fr(r['lcp'])}ms CLS {fr(r['cls'])} TBT {fr(r['tbt'])}ms"
                                for r in sel[:12]) or "(không có trang nào dưới ngưỡng — web đang khá ổn)"
                instr = ("Dựa trên chỉ số dưới, GỢI Ý CÁCH SỬA cụ thể cho từng trang chậm: chỉ rõ chỉ số nào đang tệ "
                         "(LCP cao → tối ưu ảnh/hero, preload; CLS cao → set kích thước ảnh/khung, tránh layout shift; "
                         "TBT cao → giảm/defer JS, code-split). Mỗi trang 1-2 hành động ưu tiên. Mỗi hành động PHẢI có Evidence và Confidence.")
            else:  # action_plan
                tbl = "OPPORTUNITY SCORE:\n" + (opp_tbl or "(chưa có)") + "\n\nALERT MONITOR:\n" + alert_tbl
                instr = ("Dựa DUY NHẤT trên dữ liệu dưới, lập KẾ HOẠCH HÀNH ĐỘNG TUẦN cho marketer: 4-6 việc xếp theo ƯU TIÊN "
                         "(score cao/alert high làm trước), mỗi việc kèm lý do ngắn dựa số liệu và success metric/rollback nếu phù hợp. Mỗi việc PHẢI có Evidence và Confidence. "
                         "Cuối cùng 1 câu nhắc lịch tự động nếu hợp lý. Không bịa số.")

            yield ev({"type": "step", "text": f"🧠 Tổng hợp {ctxhdr} ({model})..."})
            prompt = (f"Bạn là DeCho — agent marketing all-in-one. {instr}\n\nDỮ LIỆU {ctxhdr}:\n{tbl}\n\n"
                      "TIẾNG VIỆT, súc tích, **đậm** + gạch đầu dòng, KHÔNG LaTeX, trả lời trực tiếp. "
                      "Không đưa khuyến nghị nào thiếu Evidence/Confidence. /no_think") + _knowledge() + _persona()
            async for chunk in stream_analysis(prompt):
                yield chunk
            yield ev({"type": "done"})
            return

        # ── Config: đọc URL → làm ngay; THAY ĐỔI (thêm/xóa URL, đổi lịch) → cần MẬT KHẨU qua popup ──
        if action == "list_urls":
            yield ev({"type": "final", "text": _execute_action(data)})
            yield ev({"type": "done"})
            return
        if action in ("add_url", "remove_url", "set_schedule"):
            change = {"action": action}
            for k in ("url", "schedule_mode", "schedule_time", "schedule_day_of_month", "schedule_weekday"):
                if data.get(k) is not None:
                    change[k] = data[k]
            desc = (f"**thêm URL** {data.get('url')}" if action == "add_url" else _describe_op(data))
            yield ev({"type": "ui", "ui": {"kind": "config_pw", "change": change, "desc": desc}})
            yield ev({"type": "final", "text": "🔒 Đổi cấu hình cần **mật khẩu**. Đệ mở popup — Đại ca nhập mật khẩu để xác nhận nha."})
            yield ev({"type": "done"})
            return

        # ── PSI: phân tích kết quả ──
        if action == "query_results":
            month = str(data.get("month") or "").strip() or None
            if month:
                d = await asyncio.to_thread(sheet_store.read_results_data, month, 2000)
                if month not in (d.get("tabs") or []):
                    have = ", ".join(d.get("tabs") or []) or "chưa có tháng nào"
                    yield ev({"type": "final", "text": f"Tháng {month} Đệ chưa có dữ liệu PageSpeed (lúc đó chưa đo mà Đại ca). Hiện có data các tháng: {have}. Muốn số mới nhất thì nói 'chạy kiểm tra ngay', Đệ đo liền."})
                    yield ev({"type": "done"})
                    return
                tab, headers, rows = month, d["headers"], d["rows"]
            else:
                tab, headers, rows = await asyncio.to_thread(sheet_store.read_results)
            if not rows:
                yield ev({"type": "final", "text": "Chưa có dữ liệu PageSpeed nào — nói 'chạy kiểm tra ngay' trước nhé."})
                yield ev({"type": "done"})
                return
            _remember_data_context(req.user_id, req.session_id, "psi", tab)
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            rows, used_terms = _filter_psi_rows(headers, rows, spec)
            desc = _filter_desc(spec)
            if _filter_active(spec) and not rows:
                yield ev({"type": "final", "text": f"Không thấy dòng PageSpeed nào trong tab {tab} khớp bộ lọc: {desc}. Đại ca thử nhóm URL khác hoặc chạy check nếu danh sách mới vừa đổi."})
                yield ev({"type": "done"})
                return
            flt = f" · {desc}" if desc else ""
            yield ev({"type": "step", "text": f"📊 Đọc {len(rows)} dòng từ PSI Sheet tab {tab}{flt}"})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            extra = ("\nLƯU Ý: đây là dữ liệu ĐÃ ĐO trong tháng " + tab +
                     " (PageSpeed không đo lại quá khứ được). Kết thúc câu trả lời bằng đúng 1 câu mời theo tính cách: "
                     "nếu Đại ca muốn số liệu mới nhất thì nói 'chạy kiểm tra ngay' để Đệ đo liền.") if month else ""
            async for chunk in stream_analysis(
                _results_prompt(tab, headers, rows) + extra + _proactive_suffix(),
                include_history=False,
                buffer_until_final=True,
                final_transform=_repair_pagespeed_report_prefix,
            ):
                yield chunk
            yield ev({"type": "done"})
            return

        # ── PSI: chạy kiểm tra real-time ──
        if action == "run_check":
            if not (config.PSI_API_KEY and config.SHEET_ID):
                yield ev({"type": "error", "text": "❌ Chưa cấu hình PSI_API_KEY / SHEET_ID."})
                yield ev({"type": "done"})
                return
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)
            run_urls = runtime_config.current()["urls"]
            if _filter_active(spec):
                run_urls = _filter_rows_by_text(run_urls, lambda u: u, spec)
                if not run_urls:
                    yield ev({"type": "final", "text": f"Không có URL theo dõi nào khớp bộ lọc: {desc}. Đại ca vào Cấu hình kiểm tra danh sách URL nha."})
                    yield ev({"type": "done"})
                    return
            with _lock:
                if _state["running"]:
                    yield ev({"type": "final", "text": "Đang có một lần kiểm tra chạy rồi — chờ xong đã nhé."})
                    yield ev({"type": "done"})
                    return
                _state["running"] = True
            ok = err_count = 0
            _DONE = object()
            try:
                if desc:
                    yield ev({"type": "step", "text": f"🔎 Chỉ chạy {len(run_urls)} URL khớp bộ lọc: {desc}"})
                it = psi_checker.run_check_iter(run_urls)
                while True:
                    item = await asyncio.to_thread(next, it, _DONE)
                    if item is _DONE:
                        break
                    e = item["event"]
                    now = app_time.time_label()
                    if e == "start":
                        yield ev({"type": "step", "text": f"[{now}] 🚀 Bắt đầu: {item['total']} lượt check → Sheet tab {item['tab']}"})
                    elif e == "check":
                        dur = f" · {item['elapsed']}s" if item.get("elapsed") is not None else ""
                        if item["score"] is not None:
                            ok += 1
                            icon = "🟢" if item["score"] >= 90 else ("🟡" if item["score"] >= 50 else "🔴")
                            retry_note = f" (retry {item['attempts']} lần)" if item.get("attempts", 1) > 1 else ""
                            yield ev({"type": "step", "text": f"[{now}] {icon} [{item['i']}/{item['total']}] {item['url']} ({item['strategy']}) — {item['score']}/100{dur}{retry_note}"})
                        else:
                            err_count += 1
                            reason = item.get("error") or "không rõ"
                            yield ev({"type": "step", "text": f"[{now}] ❌ [{item['i']}/{item['total']}] {item['url']} ({item['strategy']}) — lỗi sau {item.get('attempts', 3)} lần thử: {reason}{dur}"})
                    elif e == "saved":
                        yield ev({"type": "step", "text": f"[{now}] 📊 Đã ghi {item['rows']} dòng + tô màu vào tab {item['tab']}"})
                        await asyncio.to_thread(sheet_store.append_run_log, "chat",
                                                item["total"], item["ok"], item["errors"], item["duration"])
                _state["last_result"] = "success"
                yield ev({"type": "final", "text": f"✅ Hoàn thành! {ok} kết quả{f', {err_count} lỗi' if err_count else ''} — xem chi tiết ở Dashboard hoặc Google Sheet."})
            except Exception as e:  # noqa: BLE001
                _state["last_result"] = f"error: {e}"
                yield ev({"type": "error", "text": f"❌ Lỗi khi chạy kiểm tra: {type(e).__name__}: {e}"})
            finally:
                _state["running"] = False
                _state["last_run"] = app_time.iso_now()
            yield ev({"type": "done"})
            return

        # ── Ads: danh sách campaign ──
        if action == "ads_list":
            import ads_agent

            if not ads_agent.configured():
                yield ev({"type": "error", "text": "❌ Chưa cấu hình Google Ads (GOOGLE_ADS_* trong env)."})
                yield ev({"type": "done"})
                return
            try:
                camps = await asyncio.to_thread(lambda: _cached("ads:camps", 300, lambda: {"campaigns": ads_agent.list_campaigns()}))
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi Google Ads: {_ads_exc_text(e)}"})
                yield ev({"type": "done"})
                return
            cl = camps.get("campaigns", [])
            entity_resolver.register_campaigns(cl)
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "campaign")
            desc = _filter_desc(spec, "campaign")
            cl = _filter_rows_by_text(cl, lambda c: " ".join(str(c.get(k, "")) for k in ("name", "status", "channel")), spec)
            if desc:
                yield ev({"type": "step", "text": f"🔎 Áp bộ lọc: {desc}"})
            listing = "\n".join(f"• **{c['name']}** — {c['status']} ({c['channel']})" for c in cl)
            yield ev({"type": "final", "text": f"Đang có {len(cl)} campaign:\n{listing}" if cl else "Không thấy campaign nào trong tài khoản."})
            yield ev({"type": "done"})
            return

        # ── Ads: phân tích hiệu suất ──
        if action == "ads_perf":
            import ads_agent

            if not ads_agent.configured():
                yield ev({"type": "error", "text": "❌ Chưa cấu hình Google Ads (GOOGLE_ADS_* trong env)."})
                yield ev({"type": "done"})
                return
            days = int(data.get("days") or 7)
            a_start, a_end = str(data.get("start") or ""), str(data.get("end") or "")
            if not (_re.match(r"^\d{4}-\d{2}-\d{2}$", a_start) and _re.match(r"^\d{4}-\d{2}-\d{2}$", a_end)):
                a_start = a_end = ""
            if a_start:
                yield ev({"type": "step", "text": f"💰 Đọc hiệu suất Google Ads {a_start} → {a_end}..."})
            else:
                yield ev({"type": "step", "text": f"💰 Đọc hiệu suất Google Ads {days} ngày gần nhất..."})
            try:
                key = f"ads:perf:{a_start}:{a_end}" if a_start else f"ads:perf:{days}"
                perf = await asyncio.to_thread(lambda: _cached(key, 300, lambda: ads_agent.campaign_perf(days, a_start or None, a_end or None)))
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi Google Ads: {_ads_exc_text(e)}"})
                yield ev({"type": "done"})
                return
            entity_resolver.register_campaigns(perf.get("rows") or [])
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "campaign")
            desc = _filter_desc(spec, "campaign")
            if _filter_active(spec):
                perf = _filter_dict_data(perf, ("name", "status"), spec)
            if not perf.get("rows"):
                rng_txt = f"khoảng {a_start} → {a_end}" if a_start else f"{days} ngày gần nhất"
                if desc:
                    rng_txt += f" với bộ lọc {desc}"
                yield ev({"type": "final", "text": f"Không có dữ liệu Ads trong {rng_txt} — Đại ca thử khoảng khác xem."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"📊 {len(perf['rows'])} dòng ({perf['start']} → {perf['end']})" + (f" · {desc}" if desc else "")})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            async for chunk in stream_analysis(_ads_prompt(perf) + _proactive_suffix()):
                yield chunk
            yield ev({"type": "done"})
            return

        # ── Ads: hiệu suất theo LANDING PAGE (read-only) ──
        if action == "ldp_perf":
            import ads_agent
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)

            if not ads_agent.configured():
                yield ev({"type": "error", "text": "❌ Chưa cấu hình Google Ads (GOOGLE_ADS_* trong env)."})
                yield ev({"type": "done"}); return
            days = int(data.get("days") or 7)
            a_start, a_end = str(data.get("start") or ""), str(data.get("end") or "")
            if not (_re.match(r"^\d{4}-\d{2}-\d{2}$", a_start) and _re.match(r"^\d{4}-\d{2}-\d{2}$", a_end)):
                a_start = a_end = ""
            yield ev({"type": "step", "text": "🔗 Đọc hiệu suất theo landing page..."})
            try:
                key = f"ads:ldp:{a_start}:{a_end}" if a_start else f"ads:ldp:{days}"
                ldp = await asyncio.to_thread(lambda: _cached(key, 300, lambda: ads_agent.landing_page_perf(days, a_start or None, a_end or None)))
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi Google Ads (LDP): {_ads_exc_text(e)}"})
                yield ev({"type": "done"}); return
            if _filter_active(spec):
                ldp = _filter_dict_data(ldp, ("url", "base_url"), spec)
            if not ldp.get("rows"):
                yield ev({"type": "final", "text": "Không có dữ liệu landing page trong khoảng này" + (f" với bộ lọc {desc}." if desc else ".")})
                yield ev({"type": "done"}); return
            yield ev({"type": "step", "text": f"📄 {len(ldp['rows'])} landing page ({ldp['start']} → {ldp['end']})" + (f" · {desc}" if desc else "")})
            yield ev({"type": "step", "text": f"🧠 Phân tích ({model})..."})
            async for chunk in stream_analysis(_ldp_prompt(ldp) + _proactive_suffix()):
                yield chunk
            yield ev({"type": "done"}); return

        # ── Microsoft Clarity: UX insights ──
        if action == "clarity":
            import clarity_agent
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)

            if not clarity_agent.configured():
                yield ev({"type": "error", "text": "❌ Chưa cấu hình Microsoft Clarity (CLARITY_PROJECT_ID / CLARITY_API_TOKEN trong env)."})
                yield ev({"type": "done"}); return
            yield ev({"type": "step", "text": "👁️ Đọc Microsoft Clarity insights..."})
            ins = await asyncio.to_thread(clarity_agent.insights_safe, int(data.get("days") or 3))
            if ins.get("error"):
                yield ev({"type": "error", "text": f"❌ Clarity lỗi: {ins['error']}"})
                yield ev({"type": "done"}); return
            if desc:
                yield ev({"type": "step", "text": f"🔎 Bộ lọc yêu cầu: {desc} (Clarity live insights có thể chỉ là project-level)"})
            yield ev({"type": "step", "text": f"🧠 Phân tích hành vi ({model})..."})
            async for chunk in stream_analysis(_clarity_prompt(ins, desc)):
                yield chunk
            yield ev({"type": "done"}); return

        # ── Combined: Ads + Landing Page + Clarity (user journey) ──
        if action == "combined":
            import ads_agent
            import clarity_agent

            if not ads_agent.configured():
                yield ev({"type": "error", "text": "❌ Chưa cấu hình Google Ads (GOOGLE_ADS_*)."})
                yield ev({"type": "done"}); return
            days = int(data.get("days") or 7)
            yield ev({"type": "step", "text": "📊 Gộp Ads + Landing Page + Clarity..."})
            try:
                ads = await asyncio.to_thread(lambda: _cached(f"ads:perf:{days}", 300, lambda: ads_agent.campaign_perf(days)))
                ldp = await asyncio.to_thread(lambda: _cached(f"ads:ldp:{days}", 300, lambda: ads_agent.landing_page_perf(days)))
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi Google Ads: {_ads_exc_text(e)}"})
                yield ev({"type": "done"}); return
            entity_resolver.register_campaigns(ads.get("rows") or [])
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec, "campaign" if spec.get("scope") == "campaign" else None)
            if _filter_active(spec):
                if spec.get("scope") == "campaign":
                    ads = _filter_dict_data(ads, ("name", "status"), spec)
                else:
                    ldp = _filter_dict_data(ldp, ("url", "base_url"), spec)
            clarity = clarity_agent.insights_safe(3) if clarity_agent.configured() else {"error": "Clarity chưa cấu hình"}
            if desc:
                yield ev({"type": "step", "text": f"🔎 Áp bộ lọc: {desc}"})
            yield ev({"type": "step", "text": f"🧠 Phân tích user journey ({model})..."})
            async for chunk in stream_analysis(_combined_prompt(ads, ldp, clarity) + _proactive_suffix()):
                yield chunk
            yield ev({"type": "done"}); return

        # ── GHI: tạo campaign — KHÔNG nhận mật khẩu qua chat, hướng dẫn dùng form bảo mật ──
        if action == "create_campaign":
            yield ev({"type": "final", "text": "🔒 Để giữ **mật khẩu an toàn**, Đệ không tạo campaign qua chat. Đại ca mở tab **Paid Campaigns** → bấm **“+ Tạo campaign”**, điền tên/ngân sách và nhập **mật khẩu** trong ô riêng (không lưu vào lịch sử chat). Đúng mật khẩu là Đệ tạo ngay ở trạng thái **PAUSED** (chưa tiêu tiền)."})
            yield ev({"type": "done"}); return

        # ── SEO: liệt kê tháng ──
        if action == "list_months":
            try:
                tabs = await asyncio.to_thread(_seo_list_tabs)
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": _seo_sheet_error(e)})
                yield ev({"type": "done"})
                return
            yield ev({"type": "final",
                      "text": (f"Đang có báo cáo SEO của {len(tabs)} tháng: " + ", ".join(tabs)) if tabs
                      else "Chưa có báo cáo SEO tháng nào — nói 'chạy báo cáo SEO' để bắt đầu."})
            yield ev({"type": "done"})
            return

        # ── SEO: báo cáo theo khoảng ngày tự nhiên — bước 1: đề xuất & chờ xác nhận ──
        if action == "seo_range":
            import re as _re3
            from datetime import datetime as _dt, timedelta as _td

            start, end = str(data.get("start") or ""), str(data.get("end") or "")
            if not (_re3.match(r"^\d{4}-\d{2}-\d{2}$", start) and _re3.match(r"^\d{4}-\d{2}-\d{2}$", end)):
                yield ev({"type": "final", "text": "Đệ chưa xác định được khoảng thời gian từ câu của Đại ca — nói rõ hơn giúp Đệ nhé (vd: '3 tháng gần nhất', 'từ 01/05 đến 12/06')."})
                yield ev({"type": "done"})
                return
            try:
                d0 = _dt.strptime(start, "%Y-%m-%d").date()
                d1 = _dt.strptime(end, "%Y-%m-%d").date()
            except ValueError:
                yield ev({"type": "final", "text": "Ngày không hợp lệ — Đại ca thử lại giúp Đệ."})
                yield ev({"type": "done"})
                return
            if d0 > d1:
                d0, d1 = d1, d0
            today = app_time.today()
            if d1 > today:
                d1 = today
            days = (d1 - d0).days + 1
            if days > 366:
                yield ev({"type": "final", "text": f"Khoảng {days} ngày dài quá (tối đa 366) — Đại ca thu hẹp lại nhé."})
                yield ev({"type": "done"})
                return
            p1 = d0 - _td(days=1)
            p0 = p1 - _td(days=days - 1)
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)
            uc = _filter_regex_for_seo(spec)
            pend_op.clear()    # hủy thao tác config đang chờ → 'ok' không lỡ chạy xóa URL/đổi lịch
            pend_range.clear()
            pend_range.update({"start": d0.isoformat(), "end": d1.isoformat(),
                               "url_contains": uc, "filter_spec": spec, "ts": time.time()})
            flt_line = f"\n• **Bộ lọc**: {desc}" if desc else ""
            yield ev({"type": "step", "text": f"📅 Đã xác định khoảng: {d0} → {d1} ({days} ngày)" + (f" · {desc}" if desc else "")})
            yield ev({"type": "final",
                      "text": (f"Đệ xác định được rồi nha:\n• **Khoảng lấy data**: {d0} → {d1} ({days} ngày)\n"
                               f"• **Kỳ so sánh tự động**: {p0} → {p1}{flt_line}\n• **Tên sheet**: {d0}__{d1}\n\n"
                               "Đúng ý thì Đại ca gõ **ok** (hoặc 'chạy đi') để Đệ chạy nhé.")})
            yield ev({"type": "done"})
            return

        # ── Bước 2: user xác nhận → chạy range đang chờ ──
        if action == "confirm":
            if pend_op.get("data") and time.time() - pend_op.get("ts", 0) <= 600 and pend_op.get("ts", 0) >= pend_range.get("ts", 0):
                op = pend_op["data"]
                pend_op.clear()
                yield ev({"type": "step", "text": "✅ Đã xác nhận — Đệ thực thi"})
                yield ev({"type": "final", "text": _execute_action(op)})
                yield ev({"type": "done"})
                return
            if not pend_range.get("start") or time.time() - pend_range.get("ts", 0) > 600:
                yield ev({"type": "final", "text": "Hiện không có đề xuất nào đang chờ xác nhận (hoặc đã quá 10 phút). Đại ca nêu lại yêu cầu nhé."})
                yield ev({"type": "done"})
                return
            if _seo_state["running"]:
                yield ev({"type": "final", "text": "Đang có báo cáo SEO chạy rồi — chờ xong đã nhé."})
                yield ev({"type": "done"})
                return
            rs, re_ = pend_range["start"], pend_range["end"]
            uc = pend_range.get("url_contains") or ""
            desc = _filter_desc(pend_range.get("filter_spec") or {})
            pend_range.clear()
            yield ev({"type": "step", "text": f"▶ Chạy báo cáo SEO khoảng {rs} → {re_}" + (f" · {desc}" if desc else "")})
            log_pos = len(_seo_state["log"])
            t = threading.Thread(target=_run_seo_range_safe, args=(rs, re_, uc or None, desc or None), daemon=True)
            t.start()
            while t.is_alive():
                await asyncio.sleep(1)
                new = _seo_state["log"][log_pos:]
                log_pos += len(new)
                for line in new:
                    yield ev({"type": "step", "text": line})
            for line in _seo_state["log"][log_pos:]:
                yield ev({"type": "step", "text": line})
            result = _seo_state["last_result"] or ""
            icon = "✅" if result.startswith("success") else "❌"
            yield ev({"type": "final", "text": f"{icon} {result}"})
            yield ev({"type": "done"})
            return

        # ── SEO: chạy báo cáo (1 hoặc nhiều tháng) ──
        if action == "run_report":
            if _seo_state["running"]:
                yield ev({"type": "final", "text": "Đang có báo cáo SEO chạy rồi — chờ xong đã nhé."})
                yield ev({"type": "done"})
                return
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)
            uc = _filter_regex_for_seo(spec)
            jobs = []
            for it in (data.get("months") or [{"year": data.get("year"), "month": data.get("month")}]):
                if isinstance(it, dict):
                    y, m = it.get("year"), it.get("month")
                elif isinstance(it, (list, tuple)) and len(it) == 2:
                    y, m = it
                else:
                    continue
                jobs.append((int(y), int(m)) if y and m else (None, None))
            if not jobs:
                jobs = [(None, None)]
            results = []
            for idx, (y, m) in enumerate(jobs, 1):
                label = f"{y}-{m:02d}" if y else "tháng vừa rồi"
                if len(jobs) > 1:
                    yield ev({"type": "step", "text": f"▶ [{idx}/{len(jobs)}] Chạy báo cáo {label}" + (f" · {desc}" if desc else "")})
                log_pos = len(_seo_state["log"])
                t = threading.Thread(target=_run_seo_safe, args=(y, m, uc or None, desc or None), daemon=True)
                t.start()
                while t.is_alive():
                    await asyncio.sleep(1)
                    new = _seo_state["log"][log_pos:]
                    log_pos += len(new)
                    for line in new:
                        yield ev({"type": "step", "text": line})
                for line in _seo_state["log"][log_pos:]:
                    yield ev({"type": "step", "text": line})
                results.append((label, _seo_state["last_result"] or ""))
            if len(results) == 1:
                result = results[0][1]
                # Chạy xong 1 tháng → đi thẳng vào phân tích (không hiện dòng success rồi đè)
                analyzed = False
                if result.startswith("success"):
                    mt = _re.search(r"tab (\S+)", result)
                    if mt:
                        try:
                            tab, headers, rows = await asyncio.to_thread(_seo_read_results, mt.group(1))
                            if rows:
                                _remember_data_context(req.user_id, req.session_id, "seo", tab)
                                yield ev({"type": "step", "text": f"✅ {result}"})
                                yield ev({"type": "step", "text": f"🧠 Phân tích {tab} ({model})..."})
                                async for chunk in stream_analysis(
                                    _seo_results_prompt(tab, headers, rows) + _proactive_suffix(),
                                    _seo_results_fallback(tab, headers, rows),
                                ):
                                    yield chunk
                                analyzed = True
                        except Exception as e:  # noqa: BLE001
                            yield ev({"type": "step", "text": f"⚠️ Lấy data để phân tích lỗi: {type(e).__name__}"})
                if not analyzed:
                    icon = "✅" if result.startswith("success") else "❌"
                    yield ev({"type": "final", "text": f"{icon} {result}"})
            else:
                okn = sum(1 for _, r in results if r.startswith("success"))
                lines = "\n".join(f"{'✅' if r.startswith('success') else '❌'} {lb}: {r}" for lb, r in results)
                yield ev({"type": "final", "text": f"Xong {okn}/{len(results)} tháng:\n{lines}"})
            yield ev({"type": "done"})
            return

        # ── SEO: phân tích số liệu (1 tháng / nhiều tháng / xu hướng) ──
        if action == "seo_query":
            spec = _filter_spec_from_action({**data, "action": action}, req.message, "url")
            desc = _filter_desc(spec)
            months = data.get("months")
            if isinstance(months, str) and months != "all":
                months = [months]
            if months:
                try:
                    tabs_all = await asyncio.to_thread(_seo_list_tabs)
                except Exception as e:  # noqa: BLE001
                    yield ev({"type": "error", "text": _seo_sheet_error(e)})
                    yield ev({"type": "done"})
                    return
                sel = tabs_all if months == "all" else [t for t in months if t in tabs_all]
                sel = sorted(set(sel))[-12:]
                if not sel:
                    yield ev({"type": "final", "text": f"Không tìm thấy tháng nào khớp. Các tháng đang có: {', '.join(tabs_all) or 'chưa có'}."})
                    yield ev({"type": "done"})
                    return
                _remember_data_context(req.user_id, req.session_id, "seo", sel[-1])
                yield ev({"type": "step", "text": f"📚 Đọc {len(sel)} tháng: {', '.join(sel)}"})
                try:  # đọc tất cả tháng bằng 1 lệnh batchGet
                    data_map = await asyncio.to_thread(_seo_read_many, sel, 2000)
                except Exception as e:  # noqa: BLE001
                    yield ev({"type": "error", "text": _seo_sheet_error(e)})
                    yield ev({"type": "done"})
                    return
                summaries = []
                total_filtered_rows = 0
                for t in sel:
                    headers, rows = data_map.get(t, ([], []))
                    rows = _filter_table_rows(headers, rows, spec, fallback_idx=0)
                    total_filtered_rows += len(rows)
                    summaries.append(_seo_month_summary(t, headers, rows))
                    yield ev({"type": "step", "text": f"📊 {t}: {len(rows)} URL" + (f" · {desc}" if desc else "")})
                if desc and total_filtered_rows == 0:
                    yield ev({"type": "final", "text": f"Không thấy dữ liệu SEO khớp bộ lọc: {desc}."})
                    yield ev({"type": "done"})
                    return
                yield ev({"type": "step", "text": f"🧠 Phân tích xu hướng {len(summaries)} tháng ({model})..."})
                async for chunk in stream_analysis(_seo_trend_prompt(summaries) + _proactive_suffix()):
                    yield chunk
                yield ev({"type": "done"})
                return
            try:
                tab, headers, rows = await asyncio.to_thread(_seo_read_results, data.get("month"))
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": _seo_sheet_error(e)})
                yield ev({"type": "done"})
                return
            if not rows:
                yield ev({"type": "final", "text": "Chưa có báo cáo SEO nào — nói 'chạy báo cáo SEO' trước nhé."})
                yield ev({"type": "done"})
                return
            _remember_data_context(req.user_id, req.session_id, "seo", tab)
            rows = _filter_table_rows(headers, rows, spec, fallback_idx=0)
            if _filter_active(spec) and not rows:
                yield ev({"type": "final", "text": f"Không thấy dòng SEO nào trong tab {tab} khớp bộ lọc: {desc}."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"📊 Đọc {len(rows)} dòng từ SEO Sheet tab {tab}" + (f" · {desc}" if desc else "")})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            async for chunk in stream_analysis(
                _seo_results_prompt(tab, headers, rows) + _proactive_suffix(),
                _seo_results_fallback(tab, headers, rows),
            ):
                yield chunk
            yield ev({"type": "done"})
            return

        yield ev({"type": "final", "text": data.get("text") or "Đại ca nói rõ hơn xíu nha — Đệ lo được cả PageSpeed lẫn SEO."})
        yield ev({"type": "done"})

    async def gen_with_memory():
        try:
            async for chunk in gen():
                yield chunk
        finally:
            # Ghi lượt chat vào AgentBase Memory ở thread nền — không chặn response
            if memory_agent.configured() and req.user_id and req.session_id:
                ans = "\n\n".join(final_parts).strip()
                turns = [("user", req.message)] + ([("assistant", ans)] if ans else [])
                threading.Thread(target=memory_agent.persist_turns_safe,
                                 args=(req.user_id, req.session_id, turns), daemon=True).start()

    return StreamingResponse(gen_with_memory(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class DechoAskRequest(BaseModel):
    question: str
    context: str | None = None
    model: str | None = None
    user_id: str | None = None
    session_id: str | None = None


def _range_from_text(text: str) -> dict | None:
    """Parse khoảng ngày từ câu tự nhiên: 'từ 1/5 đến 31/5', 'tháng 5', 'quý 1', '30 ngày gần nhất'..."""
    import re
    from datetime import date, datetime as _dt, timedelta

    from dateutil.relativedelta import relativedelta

    m = text.lower()
    today = app_time.today()
    # "từ d/m(/y) đến d/m(/y)"
    dm = re.search(r"từ\s*(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?\s*(?:đến|tới|->|→)\s*(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?", m)
    if dm:
        try:
            y1 = int(dm.group(3) or today.year); y2 = int(dm.group(6) or y1)
            d0 = date(y1, int(dm.group(2)), int(dm.group(1)))
            d1 = date(y2, int(dm.group(5)), int(dm.group(4)))
            if d0 > d1:
                d0, d1 = d1, d0
            return {"start": d0.isoformat(), "end": min(d1, today).isoformat()}
        except ValueError:
            return None
    rng = _parse_range_vi(m)
    if rng:
        return {"start": rng["start"], "end": rng["end"]}
    tm = re.search(r"tháng\s*(\d{1,2})(?:\s*[/\-]?\s*(?:năm\s*)?(20\d{2}))?", m)
    if tm and 1 <= int(tm.group(1)) <= 12:
        mo, yr = int(tm.group(1)), int(tm.group(2) or today.year)
        s = date(yr, mo, 1)
        e = s + relativedelta(months=1) - timedelta(days=1)
        if s > today:
            return None
        return {"start": s.isoformat(), "end": min(e, today).isoformat()}
    return None


@app.post("/api/decho/ask")
def decho_ask(req: DechoAskRequest, request: Request = None):
    """Hỏi đáp nhanh với DeCho — có bối cảnh màn hình người dùng đang xem."""
    if not (MAAS_API_KEY and MAAS_BASE_URL):
        return {"error": "Chưa cấu hình MAAS_API_KEY / MAAS_BASE_URL."}
    import httpx
    import re as _re5

    model = req.model if req.model in ALLOWED_MODELS else MAAS_MODEL

    # ── Trang Ads: nếu câu hỏi nêu khoảng thời gian khác → DeCho tự chỉnh filter, UI load data mới ──
    mv = _re5.search(r"màn hình:\s*([a-zA-Z_]+)", req.context or "")
    if mv and mv.group(1).lower() == "ads":
        rng = _range_from_text(req.question)
        if rng:
            def _fmt(s):
                y, mo, d = s.split("-")
                return f"{d}/{mo}/{y}"
            reply = (f"Dạ để Đệ chỉnh filter liền: **{_fmt(rng['start'])} → {_fmt(rng['end'])}**. "
                     "Số liệu đang lên màn hình đó Đại ca — xem xong cần Đệ phân tích thì hỏi tiếp nha!")
            if memory_agent.configured() and req.user_id and req.session_id:
                threading.Thread(target=memory_agent.persist_turns_safe,
                                 args=(req.user_id, req.session_id,
                                       [("user", req.question), ("assistant", reply)]), daemon=True).start()
            return {"reply": reply, "action": {"type": "ads_range", **rng}}
    identity_name = _identity_query_name(req.question)
    if identity_name:
        facts = []
        if memory_agent.configured() and req.user_id:
            try:
                facts = list(dict.fromkeys([
                    *memory_agent.recall_safe(req.user_id, identity_name, 8),
                    *memory_agent.list_records(req.user_id, 80),
                ]))
            except Exception as e:  # noqa: BLE001
                log.warning("Memory identity lookup lỗi (dock, bỏ qua): %s: %s", type(e).__name__, e)
        reply = _identity_memory_answer(identity_name, _identity_fact_matches(facts, identity_name, trusted_only=True))
        if memory_agent.configured() and req.user_id and req.session_id:
            threading.Thread(target=memory_agent.persist_turns_safe,
                             args=(req.user_id, req.session_id,
                                   [("user", req.question), ("assistant", reply)]), daemon=True).start()
        return {"reply": reply}
    if _looks_comparison_period_question(req.question):
        reply = _comparison_period_reply([], req.user_id, req.session_id)
        if memory_agent.configured() and req.user_id and req.session_id:
            threading.Thread(target=memory_agent.persist_turns_safe,
                             args=(req.user_id, req.session_id,
                                   [("user", req.question), ("assistant", reply)]), daemon=True).start()
        return {"reply": reply}
    relation_reply = _metric_relationship_reply(req.question)
    if relation_reply:
        if memory_agent.configured() and req.user_id and req.session_id:
            threading.Thread(target=memory_agent.persist_turns_safe,
                             args=(req.user_id, req.session_id,
                                   [("user", req.question), ("assistant", relation_reply)]), daemon=True).start()
        return {"reply": relation_reply}
    insights_reply = _insights_grounded_reply(req.question)
    if insights_reply:
        if memory_agent.configured() and req.user_id and req.session_id:
            threading.Thread(target=memory_agent.persist_turns_safe,
                             args=(req.user_id, req.session_id,
                                   [("user", req.question), ("assistant", insights_reply)]), daemon=True).start()
        return {"reply": insights_reply}
    mem_block = ""
    if memory_agent.configured() and req.user_id:
        mem_block = memory_agent.memory_block(req.user_id, req.question)
    # Hỏi về năng lực/cách dùng → bơm danh sách năng lực thật để trả lời grounded
    ql = (req.question or "").lower()
    cap_block = ""
    if any(k in ql for k in ("làm được", "lam duoc", "tính năng", "tinh nang", "chức năng", "chuc nang",
                             "hướng dẫn", "huong dan", "làm sao", "lam sao", "cách ", "là gì", "la gi",
                             "giúp được", "giup duoc", "dùng sao", "menu nào", "ở đâu", "có thể")):
        cap_block = "\n\n# " + _capabilities()
    system = (
        "Bạn là DeCho — mascot trợ thủ của app DeCho Agent (PageSpeed + SEO + Google Ads). "
        "Bạn đang đứng ở góc màn hình, nhìn cùng màn hình với người dùng.\n"
        "# BỐI CẢNH MÀN HÌNH HIỆN TẠI\n" + (req.context or "(không rõ)") +
        "\n\nTrả lời dựa trên bối cảnh trên: bình thường ngắn gọn (~80 từ), nhưng nếu người dùng hỏi chi tiết/phân tích thì dùng số liệu cụ thể trong bối cảnh, tối đa ~200 từ; "
        "nếu câu hỏi vượt quá dữ liệu đang thấy thì nói thẳng và chỉ người dùng nơi xem "
        "(menu Chat để chạy/phân tích, Dashboard để xem điểm). "
        "Nếu hỏi về năng lực/cách dùng thì trả lời theo phần năng lực bên dưới, không bịa tính năng. "
        "KHÔNG dùng LaTeX. /no_think"
        + cap_block
    ) + mem_block + _knowledge() + _persona()
    try:
        r = httpx.post(
            f"{MAAS_BASE_URL}/chat/completions",
            json={"model": model, "temperature": 0.4, "max_tokens": 2048,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": req.question}]},
            headers={"Authorization": f"Bearer {MAAS_API_KEY}"}, timeout=90)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        reply = _strip_think(msg.get("content") or "") or _strip_think(msg.get("reasoning_content") or "")
        reply = reply or "Đệ bí câu này rồi Đại ca 😅"
        if memory_agent.configured() and req.user_id and req.session_id:
            threading.Thread(target=memory_agent.persist_turns_safe,
                             args=(req.user_id, req.session_id,
                                   [("user", req.question), ("assistant", reply)]), daemon=True).start()
        return {"reply": reply}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


class VisionRequest(BaseModel):
    image: str               # data URL (data:image/...;base64,...)
    question: str | None = None
    model: str | None = None
    user_id: str | None = None
    session_id: str | None = None


@app.post("/api/decho/vision")
def decho_vision(req: VisionRequest):
    """DeCho đọc ảnh (OpenAI-compatible multimodal). Cần model hỗ trợ vision trên MaaS."""
    if not (MAAS_API_KEY and MAAS_BASE_URL):
        return {"error": "Chưa cấu hình MAAS_API_KEY / MAAS_BASE_URL."}
    if not str(req.image).startswith("data:image"):
        return {"error": "Ảnh không hợp lệ."}
    import httpx

    model = os.getenv("MAAS_VISION_MODEL") or (req.model if req.model in ALLOWED_MODELS else MAAS_MODEL)
    q = (req.question or "").strip() or "Đọc và mô tả nội dung ảnh này giúp tôi, nêu các chi tiết/số liệu quan trọng."
    system = ("Bạn là DeCho — trợ lý AI. Người dùng gửi 1 ảnh; hãy xem ảnh và trả lời câu hỏi của họ "
              "bằng tiếng Việt, súc tích, bám vào những gì THẬT SỰ thấy trong ảnh, không bịa. KHÔNG dùng LaTeX. /no_think"
              ) + _rules() + _persona()
    try:
        r = httpx.post(
            f"{MAAS_BASE_URL}/chat/completions",
            json={"model": model, "temperature": 0.3, "max_tokens": 1500,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": [
                                   {"type": "text", "text": q},
                                   {"type": "image_url", "image_url": {"url": req.image}}]}]},
            headers={"Authorization": f"Bearer {MAAS_API_KEY}"}, timeout=120)
        if r.status_code != 200:
            body = r.text[:200]
            return {"error": f"Model '{model}' chưa đọc được ảnh (HTTP {r.status_code}). "
                             f"Cần cấu hình MAAS_VISION_MODEL là model hỗ trợ ảnh. Chi tiết: {body}"}
        msg = r.json()["choices"][0]["message"]
        reply = _strip_think(msg.get("content") or "") or _strip_think(msg.get("reasoning_content") or "")
        reply = reply or "Đệ nhìn ảnh nhưng chưa mô tả được gì rõ ràng 😅"
        if memory_agent.configured() and req.user_id and req.session_id:
            threading.Thread(target=memory_agent.persist_turns_safe,
                             args=(req.user_id, req.session_id,
                                   [("user", f"[gửi 1 ảnh] {q}"), ("assistant", reply)]), daemon=True).start()
        return {"reply": reply, "model": model}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


@app.post("/api/decho/stt")
async def decho_stt(audio: UploadFile = File(...)):
    """Speech-to-Text qua MaaS Whisper (OpenAI-compatible /audio/transcriptions)."""
    if not MAAS_API_KEY:
        return {"error": "Chưa cấu hình MAAS_API_KEY."}
    # STT có endpoint RIÊNG (theo user/model), khác base LLM — lấy từ portal AI Platform.
    stt_url = os.getenv("MAAS_STT_URL", "")
    if not stt_url:
        return {"error": "Chưa cấu hình MAAS_STT_URL (URL Speech-to-Text từ portal, vd .../maas/user-XXXX/openai/whisper-large-v3/v1/audio/transcriptions)."}
    import httpx

    try:
        data = await audio.read()
        if not data:
            return {"error": "Không nhận được dữ liệu âm thanh."}
        files = {"file": (audio.filename or "audio.webm", data, audio.content_type or "audio/webm")}
        form = {"model": os.getenv("MAAS_STT_MODEL", "openai/whisper-large-v3"), "response_format": "json"}
        r = httpx.post(stt_url, files=files, data=form,
                       headers={"Authorization": f"Bearer {MAAS_API_KEY}"}, timeout=120)
        if r.status_code != 200:
            return {"error": f"STT lỗi (HTTP {r.status_code}): {r.text[:200]}"}
        j = r.json()
        return {"text": (j.get("text") or "").strip()}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


_QUIPS_FALLBACK = [
    "Vibe check ✅ Đệ đứng đây canh số liệu, Đại ca cứ chill.",
    "Cần gì cứ gọi Đệ: PageSpeed, SEO, Google Ads — chill thôi nhưng output xịn.",
    "Đại ca hỏi gì hỏi đi, Đệ đứng mãi cũng mỏi chân á.",
    "No cap, qua tab Dashboard xem điểm đi Đại ca, Đệ vẽ chart đẹp lắm.",
    "Lục bát tặng Đại ca:\nWeb nhanh thì khách mới vui,\nLCP chậm chạp thì lui khách liền 🫡",
    "Lục bát nhắc Đại ca:\nTrang nhà tải chậm như rùa,\nkhách chờ mất kiên, lượt mua cũng rời.",
    "Lục bát SEO nè:\nTừ khoá lên top mỗi ngày,\nkhách vào nườm nượp, click bay đầy nhà.",
    "Lục bát động viên:\nĐiểm xanh chín chục trở lên,\nĐại ca cứ ngủ, Đệ nền tảng lo.",
    "Lục bát quảng cáo:\nTiền tiêu mỗi sáng mỗi giờ,\nchi mà đúng chỗ, lời chờ sẵn tay.",
    "Haiku nè Đại ca:\nĐiểm xanh trên bảng,\nclicks về như lá mùa thu —\nSEO thắng lớn 🍂",
]
@app.get("/api/decho/quips")
def decho_quips():
    """Lời thoại nhàn rỗi của DeCho — danh sách tĩnh (không gọi model, khỏi tốn token)."""
    return {"quips": _QUIPS_FALLBACK}


# ── AgentBase Memory endpoints ────────────────────────────────────────────────

@app.get("/api/memory/status")
def memory_status():
    return {"configured": memory_agent.configured()}


@app.get("/api/memory/history")
def memory_history(user_id: str, session_id: str, limit: int = 40):
    """Lịch sử hội thoại từ AgentBase Memory (events) — theo thứ tự thời gian."""
    if not memory_agent.configured():
        return {"configured": False, "messages": []}
    msgs = memory_agent.get_events_safe(user_id, session_id, min(int(limit), 100))
    return {"configured": True,
            "messages": [{"me": m["role"] == "user", "text": m["message"]} for m in msgs]}


@app.get("/api/memory/records")
def memory_records(user_id: str):
    """Fact dài hạn DeCho đã nhớ về người dùng (memory records)."""
    if not memory_agent.configured():
        return {"configured": False, "facts": []}
    try:
        return {"configured": True, "facts": memory_agent.list_records(user_id)}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "facts": [], "error": f"{type(e).__name__}: {e}"}


# ── SEO endpoints ─────────────────────────────────────────────────────────────


def _seo_auth_health() -> dict:
    import seo_agent

    present = seo_agent.auth_config_present()
    if not present:
        return {"auth_configured": False, "auth_usable": False, "auth_error": "", "auth_source": "", "auth_mode": ""}
    try:
        status = seo_agent.auth_status()
        return {"auth_configured": True, "auth_usable": True, "auth_error": "", **status}
    except Exception as e:  # noqa: BLE001
        return {"auth_configured": True, "auth_usable": False, "auth_error": _seo_exc_text(e), "auth_source": "", "auth_mode": ""}


@app.get("/api/seo/config")
def seo_config():
    import seo_agent

    cfg = runtime_config.current()
    auth_health = _seo_auth_health()
    return {
        "site": seo_agent.SITE_URL,
        "ga4_property": seo_agent.GA4_PROPERTY_ID,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{seo_agent.SEO_SHEET_ID}",
        "schedule": f"ngày {cfg.get('seo_run_day_of_month', 8)} hàng tháng lúc {cfg.get('seo_run_time', '08:00')}",
        "tracked_urls": cfg.get("seo_tracked_urls") or "tất cả",
        **auth_health,
    }


@app.get("/api/seo/status")
def seo_status():
    return _seo_state


class SeoRunRequest(BaseModel):
    year: int | None = None
    month: int | None = None


@app.post("/api/seo/run")
def seo_run(body: SeoRunRequest):
    if _seo_state["running"]:
        return {"started": False, "reason": "Đang có báo cáo SEO chạy."}
    if body.year and not (1 <= (body.month or 0) <= 12):
        return {"started": False, "reason": "month phải từ 1-12."}
    threading.Thread(target=_run_seo_safe, args=(body.year, body.month), daemon=True).start()
    return {"started": True}


# ── SEO chat ──────────────────────────────────────────────────────────────────

def _seo_list_tabs() -> list[str]:
    """Liệt kê các tab tháng (YYYY-MM) đang có trong SEO Sheet."""
    import re

    import seo_agent
    from googleapiclient.discovery import build

    svc = build("sheets", "v4", credentials=seo_agent.get_creds(), cache_discovery=False)
    meta = svc.spreadsheets().get(spreadsheetId=seo_agent.SEO_SHEET_ID).execute()
    return sorted(t for t in (s["properties"]["title"] for s in meta.get("sheets", []))
                  if re.match(r"^\d{4}-\d{2}$", t))


def _seo_read_many(tabs: list[str], max_rows: int = 2000) -> dict:
    """Đọc NHIỀU tab tháng bằng MỘT lệnh batchGet. → {tab: (headers, rows)}"""
    import seo_agent
    from googleapiclient.discovery import build

    svc = build("sheets", "v4", credentials=seo_agent.get_creds(), cache_discovery=False)
    res = svc.spreadsheets().values().batchGet(
        spreadsheetId=seo_agent.SEO_SHEET_ID,
        ranges=[f"{t}!A1:K{max_rows}" for t in tabs]).execute()
    out = {}
    for t, vr in zip(tabs, res.get("valueRanges", [])):
        vals = vr.get("values", [])
        out[t] = (vals[0] if vals else [], vals[1:] if len(vals) > 1 else [])
    return out


def _seo_read_results(month: str | None = None, max_rows: int = 150):
    """Đọc báo cáo SEO từ tab tháng (YYYY-MM) trong SEO Sheet. → (tab, headers, rows)"""
    import re

    import seo_agent
    from googleapiclient.discovery import build

    svc = build("sheets", "v4", credentials=seo_agent.get_creds(), cache_discovery=False)
    meta = svc.spreadsheets().get(spreadsheetId=seo_agent.SEO_SHEET_ID).execute()
    tabs = sorted(t for t in (s["properties"]["title"] for s in meta.get("sheets", []))
                  if re.match(r"^\d{4}-\d{2}$", t))
    if not tabs:
        return None, [], []
    tab = month if month in tabs else tabs[-1]
    res = svc.spreadsheets().values().get(
        spreadsheetId=seo_agent.SEO_SHEET_ID, range=f"{tab}!A1:K2000").execute()
    vals = res.get("values", [])
    if not vals:
        return tab, [], []
    return tab, vals[0], vals[1:][-max_rows:]


def _seo_month_summary(tab: str, headers: list, rows: list) -> str:
    """Nén 1 tháng thành tóm tắt gọn (totals + top trang) cho phân tích đa tháng."""
    idx = {h: i for i, h in enumerate(headers)}

    def num(r, c):
        try:
            return float(r[idx[c]])
        except (KeyError, IndexError, ValueError):
            return 0.0

    cols = [c for c in ("views", "users", "clicks", "impressions") if c in idx]
    totals = " | ".join(f"{c}={int(sum(num(r, c) for r in rows)):,}" for c in cols)
    top = sorted(rows, key=lambda r: -num(r, "views"))[:6]
    top_lines = "\n".join(
        f"  - {r[idx['url']] if 'url' in idx and len(r) > idx['url'] else '?'}: "
        f"views={int(num(r, 'views')):,}, clicks={int(num(r, 'clicks')):,}, impressions={int(num(r, 'impressions')):,}"
        for r in top)
    return f"Tháng {tab} ({len(rows)} URL): {totals}\n Top trang theo views:\n{top_lines}"


def _seo_trend_prompt(summaries: list[str]) -> str:
    return (
        "Bạn là DeCho — module SEO (AI). Dưới đây là TÓM TẮT báo cáo SEO của nhiều tháng "
        "(views/users từ GA4; clicks/impressions từ Google Search Console):\n\n"
        + "\n\n".join(summaries) +
        "\n\nPhân tích xu hướng qua các tháng theo câu hỏi của người dùng: tổng thể tăng/giảm thế nào, "
        "tháng nào tốt/kém nhất, trang nào nổi bật. Yêu cầu: TIẾNG VIỆT, ngắn gọn, số liệu cụ thể, "
        "dùng **đậm** và gạch đầu dòng. Với mọi khuyến nghị, kèm Evidence và Confidence. "
        "KHÔNG dùng LaTeX (mũi tên viết là →). Trả lời trực tiếp. /no_think"
    ) + _knowledge() + _persona()


def _seo_intent_prompt() -> str:
    import seo_agent

    return (
        f"Bạn là DeCho — module SEO (luôn khai báo là AI). Bạn quản lý báo cáo SEO hàng tháng cho "
        f"{seo_agent.SITE_URL}: kéo Google Search Console (clicks, impressions) + GA4 (views, users), "
        f"so sánh với tháng trước, ghi vào Google Sheet. Lịch tự chạy: ngày {seo_agent.RUN_DAY_OF_MONTH} "
        f"hàng tháng lúc {seo_agent.RUN_TIME}. Trạng thái: "
        f"{'đang chạy' if _seo_state['running'] else (_seo_state['last_result'] or 'chưa chạy lần nào')}\n"
        "Trả về DUY NHẤT một JSON theo intent:\n"
        '- Chạy báo cáo 1 tháng: {"action":"run_report","year":<năm>,"month":<1-12>} (bỏ year/month nếu không nêu → tháng vừa rồi)\n'
        '- Chạy báo cáo NHIỀU tháng: {"action":"run_report","months":[{"year":2026,"month":1},{"year":2026,"month":2}]}\n'
        '- Hỏi trạng thái: {"action":"status"}\n'
        '- Hỏi/phân tích số liệu SEO 1 tháng (traffic, clicks, tăng giảm, trang tốt/kém...): {"action":"query_data","month":"YYYY-MM hoặc bỏ qua"}\n'
        '- Phân tích NHIỀU tháng / xu hướng / so sánh các tháng: {"action":"query_data","months":["2026-01","2026-02"]} (muốn tất cả các tháng thì "months":"all")\n'
        '- Hỏi đang có data/báo cáo những tháng nào trong Sheet: {"action":"list_months"}\n'
        '- Còn lại: {"action":"reply","text":"<trả lời ngắn>"}'
        + _persona()
    )


def _seo_results_prompt(tab: str, headers: list, rows: list) -> str:
    lines = [" | ".join(map(str, headers))]
    lines += [" | ".join(str(c) for c in r) for r in rows]
    return (
        f"Bạn là DeCho — module SEO (AI). Dưới đây là báo cáo SEO tháng {tab} "
        f"({len(rows)} URL): views/users từ GA4, clicks/impressions từ Google Search Console; "
        "các cột *_change_% là % thay đổi so với THÁNG TRƯỚC (N/A = trang mới).\n\n"
        + "\n".join(lines) +
        "\n\nDỮ LIỆU ĐÃ ĐƯỢC LẤY SẴN từ Google Search Console + GA4 và đã nằm trong bảng trên. "
        "KHÔNG được nói cần lấy thêm dữ liệu từ Google Search Console/GA4, KHÔNG được đề nghị chạy báo cáo "
        "khi bảng có dòng dữ liệu. Nếu người dùng hỏi 'phân tích tháng gần nhất', hãy phân tích ngay tab này.\n\n"
        "Trả lời câu hỏi dựa trên dữ liệu trên. Yêu cầu: TIẾNG VIỆT, ngắn gọn (tối đa ~15 dòng), "
        "nêu số liệu cụ thể, dùng **đậm** và gạch đầu dòng khi phù hợp. Với mọi khuyến nghị, kèm "
        "Evidence (URL + metric) và Confidence (high/medium/low). KHÔNG dùng LaTeX "
        "(mũi tên viết là →). Trả lời trực tiếp, không suy luận dài. /no_think"
        + _knowledge() + _persona()
    )


def _seo_deflected(text: str) -> bool:
    t = (text or "").lower()
    return (
        ("cần" in t or "can " in t)
        and ("google search console" in t or "gsc" in t or "ga4" in t)
        and ("lấy dữ liệu" in t or "chạy báo cáo" in t or "run report" in t)
    )


def _seo_results_fallback(tab: str, headers: list, rows: list) -> str:
    idx = {str(h).strip(): i for i, h in enumerate(headers)}

    def num(r, col):
        try:
            return float(str(r[idx[col]]).replace(",", "").replace("%", ""))
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    def cell(r, col, default=""):
        try:
            return str(r[idx[col]]).strip() or default
        except (KeyError, IndexError):
            return default

    def fmt(n):
        return f"{int(round(n)):,}".replace(",", ".")

    total_views = sum(num(r, "views") for r in rows)
    total_users = sum(num(r, "users") for r in rows)
    total_clicks = sum(num(r, "clicks") for r in rows)
    total_impr = sum(num(r, "impressions") for r in rows)
    top = sorted(rows, key=lambda r: num(r, "clicks"), reverse=True)[:5]
    drops = sorted(
        [r for r in rows if num(r, "clicks_change_%") < 0],
        key=lambda r: num(r, "clicks_change_%"),
    )[:3]
    top_txt = "; ".join(f"{_path_only(cell(r, 'url', '?'))}: {fmt(num(r, 'clicks'))} clicks" for r in top) or "chưa có"
    drop_txt = "; ".join(
        f"{_path_only(cell(r, 'url', '?'))}: {num(r, 'clicks_change_%'):.1f}%"
        for r in drops
    ) or "không thấy trang tụt clicks trong dữ liệu đã đọc"
    return (
        f"**SEO tháng {tab}**: đã đọc {len(rows)} URL từ SEO Sheet.\n"
        f"- Tổng: **{fmt(total_views)} views**, **{fmt(total_users)} users**, "
        f"**{fmt(total_clicks)} clicks**, **{fmt(total_impr)} impressions**.\n"
        f"- Top clicks: {top_txt}.\n"
        f"- Trang tụt clicks: {drop_txt}.\n\n"
        "⚠️ **Cảnh báo**: ưu tiên kiểm tra các trang tụt clicks mạnh hoặc impressions cao nhưng clicks thấp.\n"
        f"Evidence: SEO Sheet tab {tab}, {len(rows)} URL; Top clicks: {top_txt}; Drops: {drop_txt}.\n"
        "Confidence: medium.\n"
        "👉 **Nên làm tiếp**: audit title/meta + SERP của nhóm trang tụt, rồi tối ưu nội dung/truy vấn đang mất CTR."
    )


# ── Insight xuyên mảng: gộp PSI (lần chạy mới nhất) + SEO (tháng mới nhất) theo URL ──

def _path_only(u: str) -> str:
    import re
    raw = str(u or "")
    raw = raw.split("#", 1)[0].split("?", 1)[0]
    raw = re.split(r"&(?:utm_|gclid|gbraid|wbraid|fbclid)", raw, maxsplit=1)[0]
    path = re.sub(r"^https?://[^/]+", "", raw).strip() or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return path


def _num_value(x, default: float = 0.0) -> float:
    try:
        s = str(x).strip().replace(",", "").replace("%", "")
        if not s or s.upper() in {"N/A", "NONE", "NULL", "—"}:
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def _pct_value(x) -> float | None:
    s = str(x).strip()
    if not s or s.upper() in {"N/A", "NONE", "NULL", "—"}:
        return None
    return _num_value(s)


def _fmt_num(n) -> str:
    return f"{int(round(_num_value(n))):,}".replace(",", ".")


def _fmt_money(n) -> str:
    return f"{int(round(_num_value(n))):,} VND".replace(",", ".")


def _confidence(sources: list[str], evidence: list[str]) -> str:
    nsrc = len(set(sources))
    if nsrc >= 3 and len(evidence) >= 3:
        return "high"
    if nsrc >= 2 and len(evidence) >= 2:
        return "medium"
    return "low"


def _unique_strings(values, limit: int | None = None) -> list[str]:
    out, seen = [], set()
    for v in values or []:
        s = str(v or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if limit and len(out) >= limit:
            break
    return out


def _stronger_confidence(*values) -> str:
    rank = {"low": 0, "medium": 1, "high": 2}
    best = "low"
    for v in values:
        s = str(v or "low").lower()
        if rank.get(s, 0) > rank.get(best, 0):
            best = s
    return best


def _target_key(value) -> str:
    raw = str(value or "").strip()
    if raw.startswith(("http://", "https://", "/")):
        return _path_only(raw)
    return raw.lower()


def _merge_hypotheses_by_target(hypotheses: list[dict], limit: int) -> list[dict]:
    merged: dict[str, dict] = {}
    for h in sorted(hypotheses or [], key=lambda x: -_num_value(x.get("score"))):
        key = _target_key(h.get("target")) or str(h.get("target") or "")
        if not key:
            key = str(h.get("evidence") or h)
        if key not in merged:
            clone = dict(h)
            if key.startswith("/"):
                clone["target"] = key
            clone["root_causes"] = _unique_strings(clone.get("root_causes") or [], 6)
            clone["evidence"] = _unique_strings(clone.get("evidence") or [], 10)
            clone["sources"] = sorted(set(clone.get("sources") or []))
            merged[key] = clone
            continue
        cur = merged[key]
        cur["score"] = max(_num_value(cur.get("score")), _num_value(h.get("score")))
        cur["root_causes"] = _unique_strings([*(cur.get("root_causes") or []), *(h.get("root_causes") or [])], 6)
        cur["evidence"] = _unique_strings([*(cur.get("evidence") or []), *(h.get("evidence") or [])], 10)
        cur["sources"] = sorted(set(cur.get("sources") or []) | set(h.get("sources") or []))
        cur["confidence"] = _stronger_confidence(cur.get("confidence"), h.get("confidence"))
    out = list(merged.values())
    out.sort(key=lambda x: -_num_value(x.get("score")))
    return out[:max(1, min(int(limit or 12), 30))]


def _psi_latest_by_path() -> tuple[dict, str | None]:
    """{path: {MOBILE, DESKTOP, lcp, cls, tbt}} của lần chạy mới nhất."""
    try:
        _tab, _h, rows = sheet_store.read_results(5000)
    except Exception:  # noqa: BLE001
        return {}, None
    if not rows:
        return {}, None
    runs = sorted({r[0] for r in rows if r})
    last = runs[-1] if runs else None
    out: dict = {}
    for r in rows:
        if not r or r[0] != last:
            continue
        p, st = _path_only(r[1]), (r[2] if len(r) > 2 else "")
        try:
            sc = float(r[3])
        except (ValueError, IndexError):
            sc = None
        d = out.setdefault(p, {})
        d[st] = sc
        if st == "MOBILE":
            d["lcp"] = r[5] if len(r) > 5 else ""
            d["cls"] = r[6] if len(r) > 6 else ""
            d["tbt"] = r[7] if len(r) > 7 else ""
    return out, last


def _seo_latest_by_path() -> tuple[dict, str | None]:
    """{path: {views, users, clicks, impr, clicksCh, viewsCh}} của tháng SEO mới nhất."""
    try:
        tab, h, rows = _seo_read_results(None, 2000)
    except Exception:  # noqa: BLE001
        return {}, None
    if not rows:
        return {}, tab
    idx = {c: i for i, c in enumerate(h)}

    def g(r, c):
        return r[idx[c]] if c in idx and idx[c] < len(r) else ""

    def num(x):
        try:
            return float(str(x).replace(",", ""))
        except (ValueError, TypeError):
            return 0.0
    out = {}
    for r in rows:
        out[_path_only(g(r, "url"))] = {
            "views": num(g(r, "views")), "users": num(g(r, "users")),
            "clicks": num(g(r, "clicks")), "impr": num(g(r, "impressions")),
            "clicksCh": g(r, "clicks_change_%"), "viewsCh": g(r, "views_change_%")}
    return out, tab


def _insight_join() -> dict:
    """Gộp PSI + SEO theo path; trả về data + các danh sách phục vụ phân tích."""
    psi, run = _psi_latest_by_path()
    seo, month = _seo_latest_by_path()
    paths = set(psi) | set(seo)
    rows = []
    for p in paths:
        ps, se = psi.get(p, {}), seo.get(p, {})
        m = ps.get("MOBILE")
        traffic = (se.get("views") or 0) + (se.get("clicks") or 0)
        slow = (100 - m) / 100 if isinstance(m, (int, float)) else 0
        rows.append({
            "path": p, "psiM": m, "psiD": ps.get("DESKTOP"),
            "lcp": ps.get("lcp", ""), "cls": ps.get("cls", ""), "tbt": ps.get("tbt", ""),
            "views": se.get("views") or 0, "clicks": se.get("clicks") or 0,
            "impr": se.get("impr") or 0, "clicksCh": se.get("clicksCh", ""), "viewsCh": se.get("viewsCh", ""),
            "priority": round(traffic * slow),
        })
    return {"run": run, "month": month, "rows": rows}


def _ads_latest_by_path(days: int = 14) -> tuple[dict, dict]:
    import ads_agent

    if not ads_agent.configured():
        return {}, {"configured": False, "error": "Google Ads chưa cấu hình"}
    try:
        perf = _cached(f"ads:ldp:{days}", 300, lambda: ads_agent.landing_page_perf(days))
    except Exception as e:  # noqa: BLE001
        return {}, {"configured": True, "error": _ads_exc_text(e)}
    out: dict = {}
    for r in perf.get("rows") or []:
        p = _path_only(r.get("base_url") or r.get("url") or "")
        cur = out.setdefault(p, {"cost": 0.0, "clicks": 0.0, "impr": 0.0, "conv": 0.0,
                                 "ctr": 0.0, "bounce": None, "speed": None})
        cur["cost"] += _num_value(r.get("cost"))
        cur["clicks"] += _num_value(r.get("clicks"))
        cur["impr"] += _num_value(r.get("impressions"))
        cur["conv"] += _num_value(r.get("conversions"))
        if r.get("bounce_rate") is not None:
            b = _num_value(r.get("bounce_rate"))
            cur["bounce"] = b if cur["bounce"] is None else max(cur["bounce"], b)
        speeds = [_num_value(v) for v in (cur["speed"], r.get("speed_score")) if v is not None]
        cur["speed"] = min(speeds) if speeds else None
    for cur in out.values():
        cur["ctr"] = round((cur["clicks"] / cur["impr"] * 100), 2) if cur["impr"] else 0.0
    return out, {"configured": True, "start": perf.get("start"), "end": perf.get("end"), "error": perf.get("error")}


def _ads_campaign_alerts(days: int = 7) -> list[dict]:
    import ads_agent
    from datetime import date, timedelta

    if not ads_agent.configured():
        return []
    today = app_time.today()
    cur_start, cur_end = today - timedelta(days=days), today - timedelta(days=1)
    prev_start, prev_end = today - timedelta(days=days * 2), today - timedelta(days=days + 1)
    try:
        cur = _cached(f"ads:perf:{cur_start}:{cur_end}", 300,
                      lambda: ads_agent.campaign_perf(start=cur_start.isoformat(), end=cur_end.isoformat()))
        prev = _cached(f"ads:perf:{prev_start}:{prev_end}", 300,
                       lambda: ads_agent.campaign_perf(start=prev_start.isoformat(), end=prev_end.isoformat()))
    except Exception:
        return []

    def group(rows):
        out: dict = {}
        for r in rows or []:
            g = out.setdefault(r.get("name") or str(r.get("id")), {"cost": 0.0, "clicks": 0.0, "impr": 0.0, "conv": 0.0})
            g["cost"] += _num_value(r.get("cost"))
            g["clicks"] += _num_value(r.get("clicks"))
            g["impr"] += _num_value(r.get("impressions"))
            g["conv"] += _num_value(r.get("conversions"))
        for g in out.values():
            g["ctr"] = g["clicks"] / g["impr"] * 100 if g["impr"] else 0.0
            g["cpa"] = g["cost"] / g["conv"] if g["conv"] else None
        return out

    cost_floor = _num_value(os.getenv("ALERT_ADS_COST_VND", "500000"))
    cur_g, prev_g, alerts = group(cur.get("rows")), group(prev.get("rows")), []
    for name, c in cur_g.items():
        p = prev_g.get(name, {})
        if c["cost"] >= cost_floor and not c["conv"]:
            alerts.append({"lv": "high", "icon": "money", "go": "ads", "source": "Ads",
                           "text": f"{name}: tiêu {_fmt_money(c['cost'])} trong {days} ngày nhưng 0 conversion",
                           "evidence": [f"Ads {cur.get('start')} → {cur.get('end')}: cost={_fmt_money(c['cost'])}, conversions=0"],
                           "confidence": "high", "score": 90})
        if p.get("cost") and c["cost"] >= cost_floor and c["cost"] / p["cost"] >= 1.5:
            alerts.append({"lv": "med", "icon": "trend", "go": "ads", "source": "Ads",
                           "text": f"{name}: chi tiêu tăng {round((c['cost'] / p['cost'] - 1) * 100)}% so với kỳ trước",
                           "evidence": [f"Kỳ này {_fmt_money(c['cost'])}; kỳ trước {_fmt_money(p['cost'])}"],
                           "confidence": "medium", "score": 70})
        if c["impr"] >= 1000 and c["ctr"] < 1:
            alerts.append({"lv": "med", "icon": "down", "go": "ads", "source": "Ads",
                           "text": f"{name}: CTR thấp {round(c['ctr'], 2)}% với {_fmt_num(c['impr'])} impressions",
                           "evidence": [f"Ads CTR={round(c['ctr'], 2)}%, impressions={_fmt_num(c['impr'])}"],
                           "confidence": "medium", "score": 65})
        if c.get("cpa") and p.get("cpa") and c["cpa"] / p["cpa"] >= 1.5:
            alerts.append({"lv": "med", "icon": "trend", "go": "ads", "source": "Ads",
                           "text": f"{name}: CPA tăng {round((c['cpa'] / p['cpa'] - 1) * 100)}%",
                           "evidence": [f"CPA kỳ này {_fmt_money(c['cpa'])}; kỳ trước {_fmt_money(p['cpa'])}"],
                           "confidence": "medium", "score": 68})
    return alerts


def _clarity_signals() -> dict:
    import clarity_agent

    if not clarity_agent.configured():
        return {"configured": False, "signals": [], "score": 0}
    raw = clarity_agent.insights_safe(3)
    if raw.get("error"):
        return {"configured": True, "signals": [], "score": 0, "error": raw.get("error")}
    items: list[tuple[str, float]] = []

    def visit(o, label=""):
        if isinstance(o, dict):
            name = str(o.get("metricName") or o.get("name") or o.get("metric") or o.get("title") or label)
            for key in ("value", "count", "sessions", "percentage", "percent", "score"):
                if key in o:
                    items.append((name, _num_value(o.get(key))))
                    break
            for k, v in o.items():
                visit(v, f"{label}.{k}" if label else str(k))
        elif isinstance(o, list):
            for v in o:
                visit(v, label)

    visit(raw.get("data"))
    signals = []
    for name, val in items:
        n = name.lower()
        if any(k in n for k in ("rage", "dead", "error click", "quick back")) and val >= 5:
            signals.append(f"{name}: {round(val, 1)}")
        elif "scroll" in n and 0 < val < 50:
            signals.append(f"{name}: {round(val, 1)}")
        elif "bot" in n and val >= 10:
            signals.append(f"{name}: {round(val, 1)}")
    return {"configured": True, "signals": signals[:5], "score": min(15, len(signals) * 5),
            "heatmap": clarity_agent.heatmap_url(), "recordings": clarity_agent.recordings_url()}


def _fmt_pct(n) -> str:
    return f"{round(_num_value(n), 2)}%"


def _ads_metric_groups(rows: list[dict], key_fn, label_fn) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in rows or []:
        key = str(key_fn(r) or "").strip()
        if not key:
            continue
        cur = groups.setdefault(key, {
            "key": key,
            "name": str(label_fn(r) or key),
            "impressions": 0.0,
            "clicks": 0.0,
            "cost": 0.0,
            "conversions": 0.0,
        })
        cur["impressions"] += _num_value(r.get("impressions"))
        cur["clicks"] += _num_value(r.get("clicks"))
        cur["cost"] += _num_value(r.get("cost"))
        cur["conversions"] += _num_value(r.get("conversions"))
        for k in ("url", "base_url", "status", "speed_score", "bounce_rate"):
            if r.get(k) is not None and cur.get(k) is None:
                cur[k] = r.get(k)
    for cur in groups.values():
        cur["ctr"] = round(cur["clicks"] / cur["impressions"] * 100, 2) if cur["impressions"] else 0.0
        cur["cpa"] = round(cur["cost"] / cur["conversions"]) if cur["conversions"] else None
    return list(groups.values())


def _conversion_tracking_report(days: int = 30) -> dict:
    import ads_agent

    days = max(1, min(int(days or 30), 90))
    if not ads_agent.configured():
        return {
            "configured": False,
            "days": days,
            "health": "unknown",
            "issues": [{
                "lv": "med",
                "scope": "config",
                "text": "Chưa cấu hình Google Ads nên chưa audit được conversion tracking.",
                "evidence": ["Thiếu GOOGLE_ADS_* trong env"],
                "confidence": "high",
                "score": 60,
            }],
            "campaigns": [],
            "landing_pages": [],
            "totals": {},
        }

    errors: list[str] = []
    try:
        perf = _cached(f"ads:perf:{days}", 300, lambda: ads_agent.campaign_perf(days))
    except Exception as e:  # noqa: BLE001
        errors.append(_ads_exc_text(e))
        perf = {"rows": []}
    try:
        ldp = _cached(f"ads:ldp:{days}", 300, lambda: ads_agent.landing_page_perf(days))
    except Exception as e:  # noqa: BLE001
        errors.append(_ads_exc_text(e))
        ldp = {"rows": []}

    campaigns = _ads_metric_groups(perf.get("rows") or [],
                                   lambda r: r.get("name") or r.get("id"),
                                   lambda r: r.get("name") or r.get("id"))
    if perf.get("rows"):
        entity_resolver.register_campaigns(perf.get("rows") or [])
    landing_pages = _ads_metric_groups(ldp.get("rows") or [],
                                       lambda r: _path_only(r.get("base_url") or r.get("url") or ""),
                                       lambda r: _path_only(r.get("base_url") or r.get("url") or ""))
    for lp in landing_pages:
        lp["path"] = _path_only(lp.get("key") or lp.get("url") or "")

    base_rows = campaigns or landing_pages
    totals = {
        "impressions": sum(_num_value(r.get("impressions")) for r in base_rows),
        "clicks": sum(_num_value(r.get("clicks")) for r in base_rows),
        "cost": sum(_num_value(r.get("cost")) for r in base_rows),
        "conversions": sum(_num_value(r.get("conversions")) for r in base_rows),
    }
    totals["ctr"] = round(totals["clicks"] / totals["impressions"] * 100, 2) if totals["impressions"] else 0.0
    totals["cpa"] = round(totals["cost"] / totals["conversions"]) if totals["conversions"] else None

    cost_floor = _num_value(os.getenv("TRACKING_AUDIT_COST_VND", os.getenv("ALERT_ADS_COST_VND", "500000")))
    click_floor = _num_value(os.getenv("TRACKING_AUDIT_CLICK_FLOOR", "30"))
    issues: list[dict] = []

    if not base_rows and errors:
        issues.append({
            "lv": "high",
            "scope": "auth",
            "text": "Không đọc được Google Ads để audit conversion tracking.",
            "evidence": errors[:2],
            "confidence": "high",
            "score": 95,
        })
    elif not base_rows:
        issues.append({
            "lv": "low",
            "scope": "data",
            "text": f"Không có dữ liệu Ads trong {days} ngày gần nhất.",
            "evidence": [f"Google Ads rows=0, days={days}"],
            "confidence": "medium",
            "score": 35,
        })

    if totals.get("cost", 0) >= cost_floor and totals.get("clicks", 0) >= click_floor and not totals.get("conversions"):
        issues.append({
            "lv": "high",
            "scope": "account",
            "text": "Toàn bộ Ads có spend/click nhưng 0 conversion — cần kiểm tra conversion action/GTM/GA4 event.",
            "evidence": [
                f"Cost {_fmt_money(totals['cost'])}, clicks {_fmt_num(totals['clicks'])}, conversions 0",
                f"Ngưỡng audit: cost >= {_fmt_money(cost_floor)}, clicks >= {_fmt_num(click_floor)}",
            ],
            "confidence": "high",
            "score": 98,
            "metrics": totals,
        })

    for c in campaigns:
        if c["cost"] >= cost_floor and not c["conversions"]:
            issues.append({
                "lv": "high",
                "scope": "campaign",
                "name": c["name"],
                "text": f"Campaign {c['name']} tiêu {_fmt_money(c['cost'])} nhưng 0 conversion.",
                "evidence": [f"Clicks {_fmt_num(c['clicks'])}, impressions {_fmt_num(c['impressions'])}, CTR {_fmt_pct(c['ctr'])}, conversions 0"],
                "confidence": "high",
                "score": 92,
                "metrics": c,
            })
        elif c["clicks"] >= click_floor and not c["conversions"]:
            issues.append({
                "lv": "med",
                "scope": "campaign",
                "name": c["name"],
                "text": f"Campaign {c['name']} có {_fmt_num(c['clicks'])} clicks nhưng 0 conversion.",
                "evidence": [f"Cost {_fmt_money(c['cost'])}, impressions {_fmt_num(c['impressions'])}, conversions 0"],
                "confidence": "medium",
                "score": 70,
                "metrics": c,
            })

    for lp in landing_pages:
        label = lp.get("path") or lp.get("name")
        if lp["cost"] >= cost_floor and not lp["conversions"]:
            issues.append({
                "lv": "high",
                "scope": "landing_page",
                "path": label,
                "text": f"Landing page {label} tiêu {_fmt_money(lp['cost'])} nhưng 0 conversion.",
                "evidence": [f"Clicks {_fmt_num(lp['clicks'])}, impressions {_fmt_num(lp['impressions'])}, CTR {_fmt_pct(lp['ctr'])}, conversions 0"],
                "confidence": "high",
                "score": 90,
                "metrics": lp,
            })
        elif lp["clicks"] >= click_floor and not lp["conversions"]:
            issues.append({
                "lv": "med",
                "scope": "landing_page",
                "path": label,
                "text": f"Landing page {label} có {_fmt_num(lp['clicks'])} clicks nhưng 0 conversion.",
                "evidence": [f"Cost {_fmt_money(lp['cost'])}, conversions 0"],
                "confidence": "medium",
                "score": 68,
                "metrics": lp,
            })

    issues.sort(key=lambda x: -_num_value(x.get("score")))
    health = "bad" if any(i.get("lv") == "high" for i in issues) else ("warn" if issues else "ok")
    campaigns.sort(key=lambda x: (_num_value(x.get("conversions")) <= 0, _num_value(x.get("cost"))), reverse=True)
    landing_pages.sort(key=lambda x: (_num_value(x.get("conversions")) <= 0, _num_value(x.get("cost"))), reverse=True)
    return {
        "configured": True,
        "days": days,
        "start": perf.get("start") or ldp.get("start"),
        "end": perf.get("end") or ldp.get("end"),
        "health": health,
        "errors": errors,
        "thresholds": {"cost": cost_floor, "clicks": click_floor},
        "totals": totals,
        "campaigns": campaigns[:30],
        "landing_pages": landing_pages[:30],
        "issues": issues[:50],
    }


def _filter_tracking_report(report: dict, spec: dict) -> dict:
    if not _filter_active(spec):
        return report
    out = dict(report)
    if spec.get("scope") == "campaign":
        out["campaigns"] = _filter_rows_by_text(
            report.get("campaigns") or [],
            lambda r: " ".join([str(r.get("name", "")), str(r.get("key", ""))]),
            spec,
        )
        out["landing_pages"] = report.get("landing_pages") or []
    else:
        out["landing_pages"] = _filter_rows_by_text(
            report.get("landing_pages") or [],
            lambda r: " ".join([str(r.get("path", "")), str(r.get("url", "")), str(r.get("key", ""))]),
            spec,
        )
        out["campaigns"] = report.get("campaigns") or []
    global_issues = [
        i for i in (report.get("issues") or [])
        if i.get("scope") in {"account", "auth", "config", "data"}
    ]
    scoped_issues = _filter_rows_by_text(
        report.get("issues") or [],
        lambda i: " ".join([
            str(i.get("scope", "")), str(i.get("name", "")), str(i.get("path", "")),
            str(i.get("text", "")), " ".join(str(x) for x in (i.get("evidence") or [])),
        ]),
        spec,
    )
    out["issues"] = list({id(i): i for i in [*global_issues, *scoped_issues]}.values())
    out["filtered"] = True
    return out


def _tracking_audit_text(report: dict, max_items: int = 8) -> str:
    if report.get("error"):
        return f"Không audit được conversion tracking: {report['error']}"
    if not report.get("configured"):
        return "Chưa cấu hình Google Ads nên DeCho chưa audit được conversion tracking."

    totals = report.get("totals") or {}
    lines = [
        f"**Conversion Tracking Audit** ({report.get('start') or '—'} → {report.get('end') or '—'}, {report.get('days')} ngày)",
        f"- Tổng Ads: **{_fmt_money(totals.get('cost'))}**, **{_fmt_num(totals.get('clicks'))} clicks**, "
        f"**{_fmt_num(totals.get('conversions'))} conversions**, CTR **{_fmt_pct(totals.get('ctr'))}**.",
    ]
    if report.get("errors"):
        lines.append("- Lưu ý: một phần dữ liệu Ads lỗi: " + "; ".join(report["errors"][:2]))
    issues = report.get("issues") or []
    if not issues:
        lines.append("- Không thấy dấu hiệu mất conversion tracking rõ ràng trong dữ liệu hiện tại.")
        lines.append("Evidence: có dữ liệu Ads và conversion signal không bằng 0 hoặc spend chưa vượt ngưỡng audit. Confidence: medium.")
        return "\n".join(lines)
    lines.append(f"- Phát hiện **{len(issues)}** vấn đề/điểm nghi ngờ:")
    for i in issues[:max_items]:
        sev = "HIGH" if i.get("lv") == "high" else ("MED" if i.get("lv") == "med" else "LOW")
        evidence = "; ".join((i.get("evidence") or [])[:2]) or "không có evidence chi tiết"
        lines.append(f"- **[{sev}] {i.get('text')}**\n  Evidence: {evidence}\n  Confidence: {i.get('confidence', 'low')}")
    if len(issues) > max_items:
        lines.append(f"- Còn {len(issues) - max_items} issue khác trong API `/api/tracking-audit`.")
    lines.append("Nên kiểm tra: Google Ads conversion action → GTM trigger/tag → GA4 event/key event → thank-you/form submit path.")
    return "\n".join(lines)


def _root_cause_report_from_inputs(opp: dict, tracking: dict, limit: int = 12) -> dict:
    tracking_issues = tracking.get("issues") or []
    hypotheses: list[dict] = []

    def tracking_for_path(path: str) -> list[dict]:
        needle = path.strip("/")
        if not needle:
            return [i for i in tracking_issues if i.get("scope") == "account"]
        return [
            i for i in tracking_issues
            if i.get("scope") in {"landing_page", "account"} and (
                i.get("scope") == "account" or _filter_text_matches(
                    " ".join([str(i.get("path", "")), str(i.get("text", "")), " ".join(i.get("evidence") or [])]),
                    {"include": [needle], "exclude": [], "match_mode": "any"},
                )
            )
        ]

    for o in opp.get("opportunities") or []:
        path = o.get("path") or ""
        evidence = list(o.get("evidence") or [])
        ev_text = " ".join(evidence).lower()
        causes: list[str] = []
        if any(k in ev_text for k in ("psi", "lcp", "cls", "tbt", "speed")) or "PSI" in (o.get("sources") or []):
            causes.append("Core Web Vitals/tốc độ trang có thể làm giảm trải nghiệm và conversion.")
        if any(k in ev_text for k in ("clicks change", "seo ctr", "impressions")) or "SEO" in (o.get("sources") or []):
            causes.append("SEO/CTR/ranking có thể là nguồn kéo traffic/clicks xuống.")
        if any(k in ev_text for k in ("ads cost", "ads ctr", "0 conversion", "landing bounce")) or "Ads" in (o.get("sources") or []):
            causes.append("Paid traffic có spend nhưng chất lượng/landing/conversion signal chưa ổn.")
        if "Clarity" in (o.get("sources") or []):
            causes.append("UX friction từ Clarity có thể làm user rời hoặc không hoàn tất hành động.")
        t_hits = tracking_for_path(path)
        if t_hits:
            causes.append("Conversion tracking trên landing page có dấu hiệu thiếu/mất tín hiệu.")
            for i in t_hits[:2]:
                evidence.extend(i.get("evidence") or [])
        if causes:
            hypotheses.append({
                "target": path,
                "score": o.get("score", 0),
                "root_causes": causes[:4],
                "evidence": evidence[:7],
                "confidence": _confidence(o.get("sources") or [], evidence),
                "sources": sorted(set(o.get("sources") or []) | ({"Tracking"} if t_hits else set())),
            })

    for i in tracking_issues:
        if i.get("scope") not in {"campaign", "account", "auth", "landing_page"}:
            continue
        hypotheses.append({
            "target": i.get("path") or i.get("name") or i.get("scope"),
            "score": i.get("score", 0),
            "root_causes": ["Conversion tracking/conversion action có thể chưa ghi nhận đúng ở cấp campaign/landing/account."],
            "evidence": i.get("evidence") or [],
            "confidence": i.get("confidence", "medium"),
            "sources": ["Tracking", "Ads"],
        })

    hypotheses = _merge_hypotheses_by_target(hypotheses, limit)
    return {
        "run": opp.get("run"),
        "month": opp.get("month"),
        "ads": opp.get("ads"),
        "clarity": opp.get("clarity"),
        "tracking": {"health": tracking.get("health"), "issues": tracking_issues[:8]},
        "hypotheses": hypotheses,
    }


def _root_cause_report(limit: int = 12) -> dict:
    opp = _opportunity_report(max(limit * 2, 20))
    tracking = _conversion_tracking_report(30)
    return _root_cause_report_from_inputs(opp, tracking, limit)


def _filter_root_cause_report(report: dict, spec: dict) -> dict:
    if not _filter_active(spec):
        return report
    out = dict(report)
    out["hypotheses"] = _filter_rows_by_text(
        report.get("hypotheses") or [],
        lambda h: " ".join([
            str(h.get("target", "")),
            " ".join(str(c) for c in (h.get("root_causes") or [])),
            " ".join(str(e) for e in (h.get("evidence") or [])),
            " ".join(str(s) for s in (h.get("sources") or [])),
        ]),
        spec,
    )
    tracking = dict(report.get("tracking") or {})
    tracking["issues"] = _filter_rows_by_text(
        tracking.get("issues") or [],
        lambda i: " ".join([
            str(i.get("scope", "")),
            str(i.get("name", "")),
            str(i.get("path", "")),
            str(i.get("text", "")),
            " ".join(str(e) for e in (i.get("evidence") or [])),
        ]),
        spec,
    )
    out["tracking"] = tracking
    out["filtered"] = True
    return out


def _root_cause_text(report: dict, max_items: int = 20) -> str:
    hypos = report.get("hypotheses") or []
    if not hypos:
        return "Chưa có root-cause signal đủ rõ trong dữ liệu hiện tại. Nên refresh PSI/SEO/Ads hoặc bỏ bớt bộ lọc rồi thử lại."
    lines = [
        f"**Root Cause Engine** (PSI {report.get('run') or '—'} · SEO {report.get('month') or '—'} · tracking {(report.get('tracking') or {}).get('health') or '—'})",
    ]
    for idx, h in enumerate(hypos[:max_items], 1):
        causes = h.get("root_causes") or []
        evidence = h.get("evidence") or []
        lines.append(
            f"\n**{idx}. `{h.get('target')}` — score {h.get('score')}**\n"
            f"- Sources: {', '.join(h.get('sources') or []) or '—'}\n"
            f"- Root cause signals: {'; '.join(causes) or '—'}\n"
            f"- Evidence: {'; '.join(evidence[:4]) or 'không có evidence chi tiết'}\n"
            f"- Confidence: {h.get('confidence', 'medium')}"
        )
    if len(hypos) > max_items:
        lines.append(
            f"\nĐã rút gọn {len(hypos) - max_items} hypothesis ít ưu tiên hơn để chat không quá dài. "
            "Nói rõ URL/campaign muốn xem để Đệ drill-down tiếp."
        )
    lines.append("\nNên làm tiếp: xử lý hypothesis có score cao nhất trước; nếu nguồn là Tracking/Ads thì QA GTM, GA4 key event và Google Ads conversion action trước khi tối ưu UX/content.")
    return "\n".join(lines)


def _experiment_from_opportunity(o: dict, idx: int) -> dict:
    path = o.get("path") or f"opportunity-{idx}"
    evidence = list(o.get("evidence") or [])
    ev = " ".join(evidence).lower()
    if "0 conversion" in ev or "ads cost" in ev:
        kind = "conversion"
        title = f"Kiểm chứng conversion path cho {path}"
        hypothesis = "Nếu conversion event/tag và landing path được đo đúng, campaign có click/spend sẽ ghi nhận conversion hoặc micro-conversion."
        change = "Audit GTM/GA4 key event/conversion action, test form/CTA end-to-end, bật debug view trước khi chỉnh landing."
        success = "Conversion hoặc micro-conversion > 0, CPA đo được, không còn spend/click lớn nhưng 0 conversion."
        rollback = "Nếu debug event không fire hoặc conversion vẫn 0 sau đủ click, dừng mở rộng spend và sửa tracking trước."
    elif any(k in ev for k in ("lcp", "cls", "tbt", "psi", "speed")):
        kind = "speed"
        title = f"Thử nghiệm tăng tốc trang {path}"
        hypothesis = "Nếu giảm LCP/CLS/TBT ở mobile, user sẽ ít rời trang hơn và SEO/Ads landing quality cải thiện."
        change = "Tối ưu hero/ảnh, preload critical asset, defer non-critical JS, cố định kích thước khối gây layout shift."
        success = "PSI mobile +10 điểm hoặc LCP < 2.5s, CLS < 0.1, TBT giảm rõ so với baseline."
        rollback = "Nếu PSI/UX không cải thiện hoặc lỗi visual xuất hiện, revert phần asset/script vừa đổi."
    elif any(k in ev for k in ("seo ctr", "clicks change", "impressions")):
        kind = "seo"
        title = f"Thử nghiệm snippet/content cho {path}"
        hypothesis = "Nếu title/meta và nội dung khớp intent hơn, CTR/clicks sẽ hồi phục trên impressions hiện có."
        change = "Viết lại title/meta theo query intent, refresh intro/FAQ, bổ sung internal link từ trang liên quan."
        success = "CTR tăng 10-15% hoặc clicks hồi phục so với baseline tháng gần nhất."
        rollback = "Nếu CTR giảm sau 14 ngày và impressions đủ lớn, quay lại title/meta cũ hoặc thử biến thể khác."
    elif "clarity" in ev:
        kind = "ux"
        title = f"Thử nghiệm giảm friction UX cho {path}"
        hypothesis = "Nếu xử lý vùng rage/dead click hoặc scroll drop, user sẽ hoàn tất CTA dễ hơn."
        change = "Xem heatmap/recording, sửa CTA/vùng click lỗi, rút ngắn form hoặc đưa CTA lên vùng nhìn thấy."
        success = "Friction signal giảm và CTR/lead click tăng trong 7-14 ngày."
        rollback = "Nếu heatmap xấu hơn hoặc CTA click giảm, revert layout/CTA."
    else:
        kind = "mixed"
        title = f"Thử nghiệm tối ưu {path}"
        hypothesis = "Nếu xử lý tín hiệu score cao nhất, hiệu suất tổng hợp sẽ cải thiện."
        change = "Ưu tiên hạng mục có evidence mạnh nhất, đo trước/sau cùng một cửa sổ thời gian."
        success = "Metric chính cải thiện rõ hơn baseline và không làm xấu kênh còn lại."
        rollback = "Nếu metric chính không cải thiện sau 14 ngày, revert hoặc chia nhỏ giả thuyết."
    return {
        "title": title,
        "target": path,
        "type": kind,
        "priority_score": o.get("score", 0),
        "hypothesis": hypothesis,
        "change": change,
        "baseline": evidence[:5],
        "success_metric": success,
        "target_7d": "Đo sanity/debug + early signal; không kết luận nếu sample quá thấp.",
        "target_14d": "So sánh cùng weekday/window với baseline, giữ nguyên tracking và budget chính.",
        "rollback_rule": rollback,
        "confidence": o.get("confidence", "medium"),
        "sources": o.get("sources") or [],
    }


def _experiment_report_from_inputs(opp: dict, tracking: dict, limit: int = 8) -> dict:
    limit = max(1, min(int(limit or 8), 20))
    experiments = [_experiment_from_opportunity(o, i + 1) for i, o in enumerate((opp.get("opportunities") or [])[:limit * 2])]
    for issue in tracking.get("issues") or []:
        if issue.get("scope") not in {"campaign", "account", "landing_page"}:
            continue
        target = issue.get("path") or issue.get("name") or issue.get("scope")
        experiments.append({
            "title": f"Tracking QA cho {target}",
            "target": target,
            "type": "tracking",
            "priority_score": issue.get("score", 0),
            "hypothesis": "Nếu tracking đúng, traffic có intent sẽ tạo conversion/micro-conversion đo được thay vì 0 tuyệt đối.",
            "change": "Test conversion action, GTM trigger, GA4 key event, thank-you/form-submit path; ghi lại debug evidence.",
            "baseline": issue.get("evidence") or [],
            "success_metric": "Event fire ổn định trong debug + conversions/micro-conversions > 0 khi có click/spend.",
            "target_7d": "Hoàn tất QA tracking và xác nhận event fire trên môi trường thật.",
            "target_14d": "Theo dõi campaign/landing page cùng budget, không còn cost/click vượt ngưỡng nhưng 0 conversion.",
            "rollback_rule": "Không tăng budget cho tới khi conversion signal đo được; revert tag nếu tạo duplicate event.",
            "confidence": issue.get("confidence", "medium"),
            "sources": ["Tracking", "Ads"],
        })
    experiments.sort(key=lambda x: -_num_value(x.get("priority_score")))
    deduped, seen = [], set()
    for exp in experiments:
        key = (exp.get("type"), exp.get("target"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(exp)
    return {
        "run": opp.get("run"),
        "month": opp.get("month"),
        "ads": opp.get("ads"),
        "tracking_health": tracking.get("health"),
        "experiments": deduped[:limit],
    }


def _experiment_report(limit: int = 8) -> dict:
    limit = max(1, min(int(limit or 8), 20))
    opp = _opportunity_report(max(limit * 2, 20))
    tracking = _conversion_tracking_report(30)
    return _experiment_report_from_inputs(opp, tracking, limit)


def _filter_experiment_report(report: dict, spec: dict) -> dict:
    if not _filter_active(spec):
        return report
    out = dict(report)
    out["experiments"] = _filter_rows_by_text(
        report.get("experiments") or [],
        lambda e: " ".join([
            str(e.get("title", "")),
            str(e.get("target", "")),
            str(e.get("type", "")),
            str(e.get("change", "")),
            str(e.get("hypothesis", "")),
            " ".join(str(b) for b in (e.get("baseline") or [])),
        ]),
        spec,
    )
    out["filtered"] = True
    return out


def _experiments_text(report: dict, max_items: int = 20) -> str:
    exps = report.get("experiments") or []
    if not exps:
        return "Chưa có experiment candidate đủ baseline/evidence trong dữ liệu hiện tại. Nên chạy/refresh PSI, SEO, Ads hoặc tracking audit trước rồi thử lại."
    lines = [
        f"**Experiment Planner** (PSI {report.get('run') or '—'} · SEO {report.get('month') or '—'} · tracking {report.get('tracking_health') or '—'})",
    ]
    for idx, e in enumerate(exps[:max_items], 1):
        baseline = "; ".join((e.get("baseline") or [])[:4]) or "chưa có baseline chi tiết"
        lines.append(
            f"\n**{idx}. {e.get('title')}**\n"
            f"- Target: `{e.get('target')}` · Type: `{e.get('type')}` · Priority score: **{e.get('priority_score')}**\n"
            f"- Hypothesis: {e.get('hypothesis')}\n"
            f"- Change: {e.get('change')}\n"
            f"- Baseline: {baseline}\n"
            f"- Success metric: {e.get('success_metric')}\n"
            f"- 7 ngày: {e.get('target_7d')}\n"
            f"- 14 ngày: {e.get('target_14d')}\n"
            f"- Rollback: {e.get('rollback_rule')}\n"
            f"- Evidence: {baseline}\n"
            f"- Confidence: {e.get('confidence', 'medium')}"
        )
    if len(exps) > max_items:
        lines.append(
            f"\nĐã rút gọn {len(exps) - max_items} experiment ít ưu tiên hơn để chat không quá dài. "
            "Nói rõ URL/campaign muốn xem để Đệ drill-down tiếp."
        )
    return "\n".join(lines)


def _opportunity_report(limit: int = 20) -> dict:
    import math

    joined = _insight_join()
    ads_by_path, ads_meta = _ads_latest_by_path(14)
    clarity = _clarity_signals()
    opps = []
    for r in joined["rows"]:
        p = r["path"]
        ad = ads_by_path.get(p, {})
        sources, evidence = [], []
        score = 0.0

        psi_m = r.get("psiM")
        if isinstance(psi_m, (int, float)):
            sources.append("PSI")
            slow = max(0.0, 100.0 - float(psi_m))
            score += min(35, slow * 0.6)
            if psi_m < 60:
                evidence.append(f"PSI mobile {psi_m}/100")
        lcp = _num_value(r.get("lcp"))
        cls = _num_value(r.get("cls"))
        if lcp >= 2500:
            score += 8 if lcp < 4000 else 14
            evidence.append(f"LCP {round(lcp)}ms")
        if cls > 0.1:
            score += 6
            evidence.append(f"CLS {cls}")

        traffic = _num_value(r.get("views")) + _num_value(r.get("clicks"))
        if traffic > 0:
            sources.append("SEO")
            score += min(20, math.log1p(traffic) * 2.8)
            evidence.append(f"SEO traffic {_fmt_num(traffic)} (views+clicks)")
        clicks_ch = _pct_value(r.get("clicksCh"))
        if clicks_ch is not None and clicks_ch < 0:
            score += min(24, abs(clicks_ch) * 0.55)
            evidence.append(f"Clicks change {round(clicks_ch, 1)}%")
        seo_ctr = (_num_value(r.get("clicks")) / _num_value(r.get("impr")) * 100) if _num_value(r.get("impr")) else None
        if seo_ctr is not None and _num_value(r.get("impr")) >= 500 and seo_ctr < 1:
            score += 8
            evidence.append(f"SEO CTR {round(seo_ctr, 2)}% trên {_fmt_num(r.get('impr'))} impressions")

        if ad:
            sources.append("Ads")
            score += min(22, ad.get("cost", 0) / 100_000)
            if ad.get("cost", 0) >= _num_value(os.getenv("ALERT_ADS_COST_VND", "500000")) and not ad.get("conv"):
                score += 16
                evidence.append(f"Ads cost {_fmt_money(ad.get('cost'))}, 0 conversion")
            if ad.get("ctr", 0) < 1 and ad.get("impr", 0) >= 1000:
                score += 8
                evidence.append(f"Ads CTR {ad.get('ctr')}%")
            if ad.get("bounce") is not None and ad["bounce"] >= 70:
                score += 7
                evidence.append(f"Ads landing bounce {ad['bounce']}%")
            if ad.get("speed") is not None and ad["speed"] < 50:
                score += 7
                evidence.append(f"Ads landing speed score {ad['speed']}/100")

        if clarity.get("signals"):
            sources.append("Clarity")
            score += clarity.get("score", 0)
            evidence.append("Clarity: " + "; ".join(clarity["signals"][:2]))

        if score >= 10 and evidence:
            sources = sorted(set(sources))
            opps.append({"path": p, "score": round(min(100, score)), "sources": sources,
                         "evidence": evidence[:6], "confidence": _confidence(sources, evidence)})
    opps.sort(key=lambda x: x["score"], reverse=True)
    return {"run": joined.get("run"), "month": joined.get("month"), "ads": ads_meta,
            "clarity": clarity, "opportunities": opps[:max(1, min(int(limit or 20), 50))]}


def _slice_opportunity_report(report: dict, limit: int) -> dict:
    out = dict(report or {})
    out["opportunities"] = (report.get("opportunities") or [])[:max(1, min(int(limit or 20), 50))]
    return out


def _alert_report_from_opportunity(opp: dict, limit: int = 50) -> dict:
    alerts = []
    for o in opp.get("opportunities", []):
        lv = "high" if o["score"] >= 75 else "med"
        if o["score"] < 45:
            continue
        src = "+".join(o.get("sources") or [])
        alerts.append({"lv": lv, "icon": "warn" if lv == "high" else "trend", "go": "urls", "source": src,
                       "text": f"{o['path']}: opportunity score {o['score']} ({src})",
                       "evidence": o.get("evidence", []), "confidence": o.get("confidence", "low"),
                       "score": o["score"]})
    alerts.extend(_ads_campaign_alerts(7))
    for s in (opp.get("clarity") or {}).get("signals") or []:
        alerts.append({"lv": "med", "icon": "eye", "go": "ads", "source": "Clarity",
                       "text": f"Clarity phát hiện friction: {s}",
                       "evidence": [s], "confidence": "low", "score": 55})
    alerts.sort(key=lambda a: (0 if a.get("lv") == "high" else 1, -a.get("score", 0)))
    return {"alerts": alerts[:max(1, min(int(limit or 50), 100))], "opportunities": opp.get("opportunities", [])[:10],
            "run": opp.get("run"), "month": opp.get("month")}


def _alert_report(limit: int = 50) -> dict:
    opp = _opportunity_report(50)
    return _alert_report_from_opportunity(opp, limit)


def _filter_alert_report(report: dict, spec: dict) -> dict:
    if not _filter_active(spec):
        return report
    out = dict(report)
    out["alerts"] = _filter_rows_by_text(
        report.get("alerts") or [],
        lambda a: " ".join([str(a.get("text", "")), str(a.get("source", "")),
                            " ".join(str(x) for x in (a.get("evidence") or []))]),
        spec,
    )
    out["opportunities"] = _filter_rows_by_text(
        report.get("opportunities") or [],
        lambda o: " ".join([str(o.get("path", "")), " ".join(str(x) for x in (o.get("evidence") or []))]),
        spec,
    )
    return out


def _alerts_text(report: dict, max_items: int = 8) -> str:
    alerts = report.get("alerts") or []
    if not alerts:
        return "Không có alert đáng kể trong dữ liệu hiện tại. Confidence: medium — đã kiểm tra PSI/SEO và các nguồn Ads/Clarity nếu được cấu hình."
    lines = [f"**Alert monitor** — {len(alerts)} cảnh báo (PSI/SEO/Ads/Clarity nếu có cấu hình):"]
    for a in alerts[:max_items]:
        sev = "HIGH" if a.get("lv") == "high" else "MED"
        evidence = "; ".join((a.get("evidence") or [])[:2]) or "không có evidence chi tiết"
        lines.append(
            f"- **[{sev}] {a.get('text')}**\n"
            f"  Evidence: {evidence}\n"
            f"  Confidence: {a.get('confidence', 'low')}"
        )
    if len(alerts) > max_items:
        lines.append(f"- Còn {len(alerts) - max_items} alert khác trong trang Alerts.")
    return "\n".join(lines)


def _seo_keyword_intent(message: str) -> dict | None:
    import re

    m = message.lower()
    month = None
    mm = re.search(r"(20\d{2})-(\d{1,2})", m)
    if mm:
        month = f"{mm.group(1)}-{int(mm.group(2)):02d}"
    if any(k in m for k in ("chạy", "run", "tạo báo cáo", "lấy báo cáo", "báo cáo", "bao cao", "report")):
        # nhiều tháng: "chạy tháng 1, 2, 3 năm nay"
        seg = re.search(r"tháng\s+([\d\s,và]+)", m)
        if seg:
            nums = [int(n) for n in re.findall(r"\b(1[0-2]|[1-9])\b", seg.group(1))]
            if len(nums) > 1:
                ym = re.search(r"\b(20\d{2})\b", m)
                year = int(ym.group(1)) if ym else app_time.now().year
                return {"action": "run_report",
                        "months": [{"year": year, "month": n} for n in dict.fromkeys(nums)]}
        if mm:
            return {"action": "run_report", "year": int(mm.group(1)), "month": int(mm.group(2))}
        return {"action": "run_report"}
    if "tháng nào" in m or ("data" in m and "tháng" in m) or "những tháng" in m:
        return {"action": "list_months"}
    if any(k in m for k in ("phân tích", "số liệu", "traffic", "clicks", "tăng", "giảm", "kết quả", "trang nào", "xu hướng", "so sánh")):
        months_found = re.findall(r"20\d{2}-\d{2}", m)
        if len(months_found) > 1:
            return {"action": "query_data", "months": months_found}
        if any(k in m for k in ("xu hướng", "các tháng", "tất cả", "cả năm", "qua từng tháng", "5 tháng", "6 tháng")):
            return {"action": "query_data", "months": "all"}
        return {"action": "query_data", "month": month}
    if any(k in m for k in ("trạng thái", "status", "xong chưa", "sao rồi")):
        return {"action": "status"}
    return None


def _seo_status_text() -> str:
    if _seo_state["running"]:
        return "🔄 Đang chạy báo cáo SEO..."
    if _seo_state["last_run"]:
        return f"Lần chạy gần nhất (phiên này): {_seo_state['last_run']} — {_seo_state['last_result']}"
    try:
        tabs = _seo_list_tabs()
        if tabs:
            return (f"Phiên server này chưa tự chạy, nhưng SEO Sheet đã có {len(tabs)} tháng "
                    f"({tabs[-1]} mới nhất). Hỏi 'phân tích SEO' để xem, hoặc 'chạy báo cáo' để lấy mới.")
    except Exception:  # noqa: BLE001
        pass
    return "Chưa có báo cáo nào. Nói 'chạy báo cáo' để bắt đầu."


@app.post("/api/seo/chat/stream")
async def seo_chat_stream(req: ChatStreamRequest, request: Request = None):
    """SSE chat cho SEO Agent: chạy báo cáo, đọc & phân tích số liệu từ SEO Sheet."""
    # Legacy alias: giữ URL cũ nhưng chạy chung unified agent để tránh lệch logic/fix.
    return await agent_chat_stream(req, request)

    import asyncio
    import re as _re

    import httpx

    model = req.model if req.model in ALLOWED_MODELS else MAAS_MODEL

    async def gen():
        def ev(obj):
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        if not (MAAS_API_KEY and MAAS_BASE_URL):
            yield ev({"type": "error", "text": "❌ Chưa cấu hình MAAS_API_KEY / MAAS_BASE_URL."})
            yield ev({"type": "done"})
            return

        history = _sanitize_history(req.history)
        yield ev({"type": "step", "text": f"🧠 Phân tích yêu cầu ({model})..."})
        try:
            data = await _call_llm_stream(model, req.message, history, system=_seo_intent_prompt())
        except Exception as e:  # noqa: BLE001
            yield ev({"type": "error", "text": f"❌ Lỗi gọi model: {e}"})
            yield ev({"type": "done"})
            return
        if data.get("action") == "reply" and str(data.get("text", "")).startswith("❌ Model không trả về nội dung"):
            kw = _seo_keyword_intent(req.message)
            if kw:
                yield ev({"type": "step", "text": "↪️ Nhận diện intent bằng keyword"})
                data = kw

        action = data.get("action", "reply")
        labels = {"run_report": "Chạy báo cáo SEO", "seo_range": "Xác định khoảng thời gian SEO", "confirm": "Xác nhận & thực thi", "status": "Xem trạng thái",
                  "query_data": "Đọc & phân tích SEO Sheet", "list_months": "Liệt kê các tháng có data",
                  "reply": "Trả lời"}
        yield ev({"type": "step", "text": f"⚙️ Action: {labels.get(action, action)}"})

        if action == "status":
            yield ev({"type": "final", "text": _seo_status_text()})
            yield ev({"type": "done"})
            return

        if action == "list_months":
            try:
                tabs = await asyncio.to_thread(_seo_list_tabs)
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": _seo_sheet_error(e)})
                yield ev({"type": "done"})
                return
            if tabs:
                yield ev({"type": "final",
                          "text": f"Đang có báo cáo của {len(tabs)} tháng: " + ", ".join(tabs)
                                  + f".\nMuốn xem chi tiết thì hỏi kiểu \"phân tích tháng {tabs[-1]}\" nhé."})
            else:
                yield ev({"type": "final", "text": "Chưa có báo cáo tháng nào trong Sheet — nói 'chạy báo cáo' để bắt đầu."})
            yield ev({"type": "done"})
            return

        # batch nhiều action: gom các run_report thành danh sách tháng
        if action == "batch":
            months = [{"year": it.get("year"), "month": it.get("month")}
                      for it in data.get("items", []) if it.get("action") == "run_report"]
            if months:
                data = {"action": "run_report", "months": months}
                action = "run_report"
                yield ev({"type": "step", "text": f"⚙️ Gom {len(months)} tháng cần chạy"})
            else:
                data = data["items"][0]
                action = data.get("action", "reply")

        if action == "run_report":
            if _seo_state["running"]:
                yield ev({"type": "final", "text": "Đang có báo cáo SEO chạy rồi — chờ xong đã nhé."})
                yield ev({"type": "done"})
                return
            # Danh sách (year, month) cần chạy — 1 hoặc nhiều tháng
            uc = str(data.get("url_contains") or "").strip() or _parse_url_contains(req.message) or ""
            jobs = []
            for it in (data.get("months") or [{"year": data.get("year"), "month": data.get("month")}]):
                if isinstance(it, dict):
                    y, m = it.get("year"), it.get("month")
                elif isinstance(it, (list, tuple)) and len(it) == 2:
                    y, m = it
                else:
                    continue
                jobs.append((int(y), int(m)) if y and m else (None, None))
            if not jobs:
                jobs = [(None, None)]
            results = []
            for idx, (y, m) in enumerate(jobs, 1):
                label = f"{y}-{m:02d}" if y else "tháng vừa rồi"
                if len(jobs) > 1:
                    yield ev({"type": "step", "text": f"▶ [{idx}/{len(jobs)}] Chạy báo cáo {label}" + (f" · lọc '{uc}'" if uc else "")})
                log_pos = len(_seo_state["log"])
                t = threading.Thread(target=_run_seo_safe, args=(y, m, uc or None), daemon=True)
                t.start()
                while t.is_alive():
                    await asyncio.sleep(1)
                    new = _seo_state["log"][log_pos:]
                    log_pos += len(new)
                    for line in new:
                        yield ev({"type": "step", "text": line})
                for line in _seo_state["log"][log_pos:]:
                    yield ev({"type": "step", "text": line})
                results.append((label, _seo_state["last_result"] or ""))
            if len(results) == 1:
                result = results[0][1]
                # Chạy xong 1 tháng → đi thẳng vào phân tích (không hiện dòng success rồi đè)
                analyzed = False
                if result.startswith("success"):
                    mt = _re.search(r"tab (\S+)", result)
                    if mt:
                        try:
                            tab, headers, rows = await asyncio.to_thread(_seo_read_results, mt.group(1))
                            if rows:
                                yield ev({"type": "step", "text": f"✅ {result}"})
                                yield ev({"type": "step", "text": f"🧠 Phân tích {tab} ({model})..."})
                                async for chunk in stream_analysis(
                                    _seo_results_prompt(tab, headers, rows) + _proactive_suffix(),
                                    _seo_results_fallback(tab, headers, rows),
                                ):
                                    yield chunk
                                analyzed = True
                        except Exception as e:  # noqa: BLE001
                            yield ev({"type": "step", "text": f"⚠️ Lấy data để phân tích lỗi: {type(e).__name__}"})
                if not analyzed:
                    icon = "✅" if result.startswith("success") else "❌"
                    yield ev({"type": "final", "text": f"{icon} {result}"})
            else:
                ok = sum(1 for _, r in results if r.startswith("success"))
                lines = "\n".join(f"{'✅' if r.startswith('success') else '❌'} {lb}: {r}" for lb, r in results)
                yield ev({"type": "final", "text": f"Xong {ok}/{len(results)} tháng:\n{lines}"})
            yield ev({"type": "done"})
            return

        async def stream_analysis(system_prompt: str, fallback_on_deflect: str | None = None):
            """Stream phân tích từ LLM: yield delta/final/error events."""
            payload = {"model": model, "stream": True, "temperature": 0.2, "max_tokens": 4096,
                       "messages": [{"role": "system", "content": system_prompt},
                                    *history,
                                    {"role": "user", "content": req.message}]}
            think_re = _re.compile(r"<think>.*?(?:</think>|$)", _re.S)
            buffer_until_final = fallback_on_deflect is not None
            raw_acc, sent, reasoning_acc = "", 0, []
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream("POST", f"{MAAS_BASE_URL}/chat/completions", json=payload,
                                             headers={"Authorization": f"Bearer {MAAS_API_KEY}"}) as r:
                        if r.status_code != 200:
                            body = (await r.aread()).decode(errors="replace")[:300]
                            yield ev({"type": "error", "text": f"❌ MaaS trả về HTTP {r.status_code}: {body}"})
                            return
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                delta = json.loads(raw)["choices"][0].get("delta", {})
                            except Exception:  # noqa: BLE001
                                continue
                            rc = delta.get("reasoning_content") or delta.get("reasoning")
                            if rc:
                                reasoning_acc.append(rc)
                            c = delta.get("content")
                            if not c:
                                continue
                            raw_acc += c
                            visible = think_re.sub("", raw_acc).lstrip()
                            safe = max(0, len(visible) - 12)
                            if safe > sent and not buffer_until_final:
                                yield ev({"type": "delta", "delta": visible[sent:safe]})
                                sent = safe
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi phân tích: {type(e).__name__}: {e}"})
                return
            visible = think_re.sub("", raw_acc).strip()
            if not visible:
                visible = think_re.sub("", "".join(reasoning_acc)).strip()
            if visible:
                if fallback_on_deflect and _seo_deflected(visible):
                    visible = fallback_on_deflect
                    sent = 0
                if len(visible) > sent:
                    yield ev({"type": "delta", "delta": visible[sent:]})
                yield ev({"type": "final", "text": visible})
            else:
                yield ev({"type": "error", "text": "❌ Model không trả về nội dung phân tích."})

        if action == "query_data":
            months = data.get("months")
            if isinstance(months, str) and months != "all":
                months = [months]

            if months:  # ── NHIỀU THÁNG: đọc từng tab, nén tóm tắt, phân tích xu hướng
                try:
                    tabs_all = await asyncio.to_thread(_seo_list_tabs)
                except Exception as e:  # noqa: BLE001
                    yield ev({"type": "error", "text": _seo_sheet_error(e)})
                    yield ev({"type": "done"})
                    return
                sel = tabs_all if months == "all" else [t for t in months if t in tabs_all]
                sel = sorted(set(sel))[-12:]
                if not sel:
                    yield ev({"type": "final", "text": f"Không tìm thấy tháng nào khớp. Các tháng đang có: {', '.join(tabs_all) or 'chưa có'}."})
                    yield ev({"type": "done"})
                    return
                yield ev({"type": "step", "text": f"📚 Đọc {len(sel)} tháng: {', '.join(sel)}"})
                try:  # đọc tất cả tháng bằng 1 lệnh batchGet
                    data_map = await asyncio.to_thread(_seo_read_many, sel, 2000)
                except Exception as e:  # noqa: BLE001
                    yield ev({"type": "error", "text": _seo_sheet_error(e)})
                    yield ev({"type": "done"})
                    return
                summaries = []
                for t in sel:
                    headers, rows = data_map.get(t, ([], []))
                    summaries.append(_seo_month_summary(t, headers, rows))
                    yield ev({"type": "step", "text": f"📊 {t}: {len(rows)} URL"})
                yield ev({"type": "step", "text": f"🧠 Phân tích xu hướng {len(summaries)} tháng ({model})..."})
                async for chunk in stream_analysis(_seo_trend_prompt(summaries) + _proactive_suffix()):
                    yield chunk
                yield ev({"type": "done"})
                return

            # ── 1 THÁNG: dữ liệu đầy đủ
            try:
                tab, headers, rows = await asyncio.to_thread(_seo_read_results, data.get("month"))
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": _seo_sheet_error(e)})
                yield ev({"type": "done"})
                return
            if not rows:
                yield ev({"type": "final", "text": "Chưa có báo cáo nào trong SEO Sheet — nói 'chạy báo cáo' trước nhé."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"📊 Đọc {len(rows)} dòng từ tab {tab}"})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            async for chunk in stream_analysis(
                _seo_results_prompt(tab, headers, rows) + _proactive_suffix(),
                _seo_results_fallback(tab, headers, rows),
            ):
                yield chunk
            yield ev({"type": "done"})
            return

        yield ev({"type": "final", "text": data.get("text") or _seo_status_text()})
        yield ev({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


ACTION_LABELS = {
    "run_check": "Chạy kiểm tra PageSpeed",
    "status": "Xem trạng thái",
    "list_urls": "Liệt kê URL",
    "query_results": "Đọc & phân tích kết quả từ Sheet",
    "add_url": "Thêm URL",
    "remove_url": "Xóa URL",
    "set_schedule": "Đổi lịch chạy",
    "reply": "Trả lời",
}


async def _call_llm_stream(model: str, message: str, history: list[dict] | None = None,
                           system: str | None = None) -> dict:
    """Gọi MaaS (stream để nhận sớm), nuốt phần <think>, trả về JSON action."""
    import httpx

    payload = {
        "model": model, "stream": True, "temperature": 0, "max_tokens": 4096,
        # /no_think: tắt thinking mode của Qwen cho bước phân loại intent (không cần suy luận dài)
        "messages": [{"role": "system", "content": (system or _system_prompt()) + "\nCHỈ trả về JSON thuần (KHÔNG dùng <tool_code>, KHÔNG markdown ```, KHÔNG thẻ XML, KHÔNG 'tool =>'). "
                     "TUYỆT ĐỐI KHÔNG tự diễn cảnh gọi tool trong câu trả lời (không viết '🔍 Đang lấy dữ liệu...', không in {\"url\":...}/{\"query\":...} rồi tự BỊA bảng/kết quả). "
                     "Cần dữ liệu ngoài → chỉ trả ĐÚNG 1 JSON action (web_search/web_fetch) để hệ thống chạy THẬT. Trả JSON ngay, không suy luận dài. /no_think"},
                     *(history or []),
                     {"role": "user", "content": message}],
    }
    import re

    content_parts, reasoning_parts = [], []
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"{MAAS_BASE_URL}/chat/completions",
                                 json=payload,
                                 headers={"Authorization": f"Bearer {MAAS_API_KEY}"}) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="replace")[:300]
                raise RuntimeError(f"MaaS trả về HTTP {r.status_code} (model {model}): {body}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    delta = json.loads(raw)["choices"][0].get("delta", {})
                except Exception:  # noqa: BLE001
                    continue
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if rc:
                    reasoning_parts.append(rc)
                c = delta.get("content")
                if c:
                    content_parts.append(c)

    # Gom toàn bộ rồi mới xử lý — an toàn với thẻ <think> bị cắt giữa các chunk
    raw_text = "".join(content_parts)
    visible = re.sub(r"<think>.*?(?:</think>|$)", "", raw_text, flags=re.S)
    visible = re.sub(r"<tool_code>.*?(?:</tool_code>|$)", "", visible, flags=re.S)  # bỏ khối tool_code khỏi text
    visible = re.sub(r"</?(?:query|fact|tool_code|args)>", "", visible).strip()    # bỏ thẻ lẻ còn sót

    # Tìm JSON action: hỗ trợ cả MẢNG nhiều action lẫn object đơn.
    # Ưu tiên ký tự mở xuất hiện TRƯỚC — tránh vớ nhầm mảng con bên trong object
    # (vd {"action":"query_data","months":[...]}).
    def _repair(cand: str) -> str:
        c = re.sub(r"```(?:json)?", "", cand)     # bỏ code fence ```json ... ```
        c = re.sub(r",\s*([}\]])", r"\1", c)      # bỏ trailing comma trước } hoặc ]
        return c

    def _try_parse(cand: str):
        for variant in (cand, _repair(cand)):     # thử bản gốc, rồi bản đã sửa nhẹ
            pairs = sorted((p for p in (("{", "}"), ("[", "]")) if p[0] in variant and p[1] in variant),
                           key=lambda p: variant.index(p[0]))
            for op, cl in pairs:
                try:
                    return json.loads(variant[variant.index(op):variant.rindex(cl) + 1])
                except Exception:  # noqa: BLE001
                    continue
        return None

    for cand in (visible, raw_text, "".join(reasoning_parts)):
        val = _try_parse(cand)
        if isinstance(val, list):
            items = [x for x in val if isinstance(x, dict) and x.get("action")]
            if items:
                return {"action": "batch", "items": items}
        elif isinstance(val, dict) and val.get("action"):
            return val
    # Model trả <tool_code>/tool=>'x' thay vì JSON → bóc tool ra thực thi (không in raw)
    tool_act = _extract_tool_action(raw_text) or _extract_tool_action("".join(reasoning_parts))
    if tool_act:
        return tool_act
    if visible:
        # Lưới an toàn: bỏ dòng chỉ chứa 1 JSON tool-key (model lỡ in {"url":...}/{"query":...} ra prose)
        clean = re.sub(r'(?m)^\s*\{\s*"(?:url|query|action|tool|fact|args)"\s*:.*\}\s*,?\s*$', "", visible)
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
        return {"action": "reply", "text": clean or visible}
    return {"action": "reply", "text": "❌ Model không trả về nội dung."}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatStreamRequest, request: Request = None):
    """SSE: hiển thị các bước hành động của agent + tiến trình check real-time.

    Events: {"type":"step","text"} | {"type":"final","text"} | {"type":"error","text"} | {"type":"done"}
    """
    # Legacy alias: giữ URL cũ nhưng chạy chung unified agent để tránh lệch logic/fix.
    return await agent_chat_stream(req, request)

    import asyncio

    model = req.model if req.model in ALLOWED_MODELS else MAAS_MODEL

    async def gen():
        def ev(obj):
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        if not (MAAS_API_KEY and MAAS_BASE_URL):
            yield ev({"type": "error",
                      "text": "❌ Chưa cấu hình MAAS_API_KEY / MAAS_BASE_URL trong .env — không gọi được model. "
                              "Kiểm tra /api/llm-test sau khi sửa."})
            yield ev({"type": "done"})
            return

        history = _sanitize_history(req.history)
        yield ev({"type": "step", "text": f"🧠 Phân tích yêu cầu ({model})..."})
        try:
            data = await _call_llm_stream(model, req.message, history)
        except Exception as e:  # noqa: BLE001
            yield ev({"type": "error", "text": f"❌ Lỗi gọi model: {e}"})
            yield ev({"type": "done"})
            return

        # Model không trả nội dung (thinking ăn hết budget) → fallback keyword thay vì báo lỗi
        if data.get("action") == "reply" and str(data.get("text", "")).startswith("❌ Model không trả về nội dung"):
            kw = _keyword_intent(req.message)
            if kw:
                yield ev({"type": "step", "text": "↪️ Model không trả JSON — nhận diện intent bằng keyword"})
                data = kw

        if data.get("action") == "batch":  # PSI: lấy action đầu tiên trong batch
            data = data["items"][0]
        action = data.get("action", "reply")
        yield ev({"type": "step", "text": f"⚙️ Action: {ACTION_LABELS.get(action, action)}"})

        if action == "query_results":
            import re as _re

            import httpx

            tab, headers, rows = await asyncio.to_thread(sheet_store.read_results)
            if not rows:
                yield ev({"type": "final", "text": "Chưa có dữ liệu kết quả nào trong Sheet — nói 'chạy kiểm tra ngay' để đo trước nhé."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"📊 Đọc {len(rows)} dòng kết quả từ tab {tab}"})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})

            payload = {"model": model, "stream": True, "temperature": 0.2, "max_tokens": 4096,
                       "messages": [{"role": "system", "content": _results_prompt(tab, headers, rows)},
                                    *history,
                                    {"role": "user", "content": req.message}]}
            think_re = _re.compile(r"<think>.*?(?:</think>|$)", _re.S)
            raw_acc, sent = "", 0
            reasoning_acc = []
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream("POST", f"{MAAS_BASE_URL}/chat/completions",
                                             json=payload,
                                             headers={"Authorization": f"Bearer {MAAS_API_KEY}"}) as r:
                        if r.status_code != 200:
                            body = (await r.aread()).decode(errors="replace")[:300]
                            yield ev({"type": "error", "text": f"❌ MaaS trả về HTTP {r.status_code}: {body}"})
                            yield ev({"type": "done"})
                            return
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                delta = json.loads(raw)["choices"][0].get("delta", {})
                            except Exception:  # noqa: BLE001
                                continue
                            rc = delta.get("reasoning_content") or delta.get("reasoning")
                            if rc:
                                reasoning_acc.append(rc)  # giữ làm fallback nếu content rỗng
                            c = delta.get("content")
                            if not c:
                                continue
                            raw_acc += c
                            visible = think_re.sub("", raw_acc).lstrip()
                            # giữ lại 12 ký tự cuối phòng thẻ <think>/</think> đang gõ dở
                            safe = max(0, len(visible) - 12)
                            if safe > sent:
                                yield ev({"type": "delta", "delta": visible[sent:safe]})
                                sent = safe
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi phân tích: {type(e).__name__}: {e}"})
                yield ev({"type": "done"})
                return
            visible = think_re.sub("", raw_acc).strip()
            if not visible:
                # content rỗng: model dồn hết vào reasoning → dùng phần đó (đã lọc think)
                visible = think_re.sub("", "".join(reasoning_acc)).strip()
                if visible:
                    yield ev({"type": "final", "text": visible})
                else:
                    yield ev({"type": "error", "text": "❌ Model không trả về nội dung phân tích — thử lại hoặc đổi model khác (Gemma không có thinking)."})
            else:
                if len(visible) > sent:
                    yield ev({"type": "delta", "delta": visible[sent:]})
                yield ev({"type": "final", "text": visible})
            yield ev({"type": "done"})
            return

        if action != "run_check":
            yield ev({"type": "final", "text": _execute_action(data)})
            yield ev({"type": "done"})
            return

        # run_check: chạy NGAY trong stream, hiển thị từng URL real-time
        if not (config.PSI_API_KEY and config.SHEET_ID):
            yield ev({"type": "error", "text": "❌ Chưa cấu hình PSI_API_KEY / SHEET_ID."})
            yield ev({"type": "done"})
            return
        with _lock:
            if _state["running"]:
                yield ev({"type": "final", "text": "Đang có một lần kiểm tra chạy rồi — chờ xong đã nhé."})
                yield ev({"type": "done"})
                return
            _state["running"] = True
        ok = err_count = 0
        _DONE = object()  # sentinel: tránh StopIteration lọt vào Future (Python cấm)
        try:
            it = psi_checker.run_check_iter()
            while True:
                item = await asyncio.to_thread(next, it, _DONE)
                if item is _DONE:
                    break
                e = item["event"]
                now = app_time.time_label()
                if e == "start":
                    yield ev({"type": "step",
                              "text": f"[{now}] 🚀 Bắt đầu: {item['total']} lượt check → Sheet tab {item['tab']}"})
                elif e == "check":
                    dur = f" · {item['elapsed']}s" if item.get("elapsed") is not None else ""
                    if item["score"] is not None:
                        ok += 1
                        icon = "🟢" if item["score"] >= 90 else ("🟡" if item["score"] >= 50 else "🔴")
                        retry_note = f" (retry {item['attempts']} lần)" if item.get("attempts", 1) > 1 else ""
                        yield ev({"type": "step",
                                  "text": f"[{now}] {icon} [{item['i']}/{item['total']}] {item['url']} ({item['strategy']}) — {item['score']}/100{dur}{retry_note}"})
                    else:
                        err_count += 1
                        reason = item.get("error") or "không rõ"
                        yield ev({"type": "step",
                                  "text": f"[{now}] ❌ [{item['i']}/{item['total']}] {item['url']} ({item['strategy']}) — lỗi sau {item.get('attempts', 3)} lần thử: {reason}{dur}"})
                elif e == "saved":
                    yield ev({"type": "step", "text": f"[{now}] 📊 Đã ghi {item['rows']} dòng + tô màu vào tab {item['tab']}"})
                    await asyncio.to_thread(sheet_store.append_run_log, "chat",
                                            item["total"], item["ok"], item["errors"], item["duration"])
            _state["last_result"] = "success"
            yield ev({"type": "final",
                      "text": f"✅ Hoàn thành! {ok} kết quả{f', {err_count} lỗi' if err_count else ''} "
                              f"— xem chi tiết trong Google Sheet."})
        except Exception as e:  # noqa: BLE001
            _state["last_result"] = f"error: {e}"
            yield ev({"type": "error", "text": f"❌ Lỗi khi chạy kiểm tra: {type(e).__name__}: {e}"})
        finally:
            _state["running"] = False
            _state["last_run"] = app_time.iso_now()
        yield ev({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat")
def chat(req: ChatRequest):
    reply = _ask_llm(req.message)
    if reply is None:
        import re

        m = req.message.lower()
        url_match = re.search(r"https?://\S+", req.message)
        if any(k in m for k in ("thêm", "add")) and url_match:
            reply = _execute_action({"action": "add_url", "url": url_match.group(0).rstrip(".,;")})
        elif any(k in m for k in ("xóa", "xoá", "remove", "delete", "bỏ")) and (url_match or True) and any(
                k in m for k in ("url", "http", "trang", "link")):
            target = url_match.group(0).rstrip(".,;") if url_match else req.message.split()[-1]
            reply = _execute_action({"action": "remove_url", "url": target})
        elif any(k in m for k in ("danh sách", "list", "url nào", "những url")):
            reply = _list_urls_text()
        elif any(k in m for k in ("chạy", "check", "kiểm tra", "run", "trigger", "start")):
            reply = _do_trigger()
        elif any(k in m for k in ("trạng thái", "status", "kết quả", "xong chưa", "sao rồi")):
            reply = _status_text()
        else:
            reply = ("Mình là AI agent kiểm tra PageSpeed 🤖. Bạn có thể nói: 'chạy kiểm tra ngay', "
                     "'trạng thái?', 'danh sách URL', 'thêm https://...', 'xóa <url>'.")
    return {"reply": reply}


@app.get("/", response_class=HTMLResponse)
def index():
    from pathlib import Path

    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
