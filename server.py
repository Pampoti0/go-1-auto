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
from datetime import datetime

import schedule
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

import config
import memory_agent
import psi_checker
import runtime_config
import sheet_store

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

app = FastAPI(title="DeCho Agent")

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
        _seo_state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {record.getMessage()}")
        del _seo_state["log"][:-200]


__import__("logging").getLogger("seo_agent").addHandler(_SeoLogHandler())
__import__("logging").getLogger("seo_agent").setLevel(__import__("logging").INFO)


def _run_seo_safe(year: int | None = None, month: int | None = None, url_contains: str | None = None):
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
        flt = f" · lọc URL chứa '{url_contains}'" if url_contains else ""
        _seo_state["last_result"] = f"success: {result['rows']} URL → tab {result['label']}{flt}"
    except Exception as e:  # noqa: BLE001
        _seo_state["last_result"] = f"error: {type(e).__name__}: {e}"
        _seo_state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ {type(e).__name__}: {e}")
    finally:
        _seo_state["running"] = False
        _seo_state["last_run"] = datetime.now().isoformat(timespec="seconds")
        _invalidate_cache()


_pending_range: dict = {}  # đề xuất date range đang chờ user xác nhận


def _run_seo_range_safe(start: str, end: str, url_contains: str | None = None):
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
        flt = f" · lọc URL chứa '{url_contains}'" if url_contains else ""
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
        _seo_state["last_result"] = f"error: {type(e).__name__}: {e}"
        _seo_state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ {type(e).__name__}: {e}")
    finally:
        _seo_state["running"] = False
        _seo_state["last_run"] = datetime.now().isoformat(timespec="seconds")
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
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")
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
            if datetime.now().day == cfg["schedule_day_of_month"]:
                _run_check_safe()
        schedule.every().day.at(at).do(monthly)

    # SEO agent: chạy hàng tháng — ngày/giờ chỉnh được qua UI (runtime_config)
    def seo_monthly():
        if datetime.now().day == int(runtime_config.current().get("seo_run_day_of_month", 8)):
            _run_seo_safe()
    schedule.every().day.at(cfg.get("seo_run_time", "08:00")).do(seo_monthly)


def _scheduler_loop():
    _build_schedule()
    while True:
        schedule.run_pending()
        time.sleep(30)


@app.on_event("startup")
def startup():
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


@app.get("/api/config")
def get_config():
    return runtime_config.current()


# ── Cache đọc Sheet (tránh 429: quota 60 reads/phút) ──────────────────────────
_api_cache: dict = {}


def _cached(key: str, ttl: int, fn):
    now = time.time()
    ent = _api_cache.get(key)
    if ent and now - ent[0] < ttl:
        return ent[1]
    val = fn()
    # không cache kết quả lỗi
    if not (isinstance(val, dict) and val.get("error")):
        _api_cache[key] = (now, val)
    return val


def _invalidate_cache():
    _api_cache.clear()


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
        return {"error": f"{type(e).__name__}: {e}", "tab": None, "tabs": [], "headers": [], "rows": []}


@app.get("/api/seo/summary")
def api_seo_summary(limit: int = 6):
    """Tổng views/users/clicks/impressions theo từng tháng (cache 10 phút)."""
    def fetch():
        return _seo_summary_fetch(limit)
    try:
        return _cached(f"seosum:{limit}", 600, fetch)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "months": []}


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
        return {"error": f"{type(e).__name__}: {e}", "months": []}


@app.get("/api/ads/campaigns")
def api_ads_campaigns():
    """Danh sách campaign Google Ads (cache 5 phút)."""
    import ads_agent

    if not ads_agent.configured():
        return {"error": "Chưa cấu hình Google Ads (GOOGLE_ADS_* trong env).", "campaigns": []}
    try:
        return _cached("ads:camps", 300, lambda: {"campaigns": ads_agent.list_campaigns()})
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "campaigns": []}


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
        return _cached(key, 300, lambda: ads_agent.campaign_perf(days, start, end))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "rows": []}


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
        "TIẾNG VIỆT, ngắn gọn, số liệu cụ thể, **đậm** + gạch đầu dòng. KHÔNG dùng LaTeX. Trả lời trực tiếp. /no_think"
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


@app.put("/api/config")
def put_config(body: ConfigUpdate):
    partial = {k: v for k, v in body.model_dump().items() if v is not None}
    if not partial:
        return {"ok": False, "error": "Không có trường nào để cập nhật."}
    try:
        cfg = runtime_config.update(partial)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if any(k.startswith("schedule") or k.startswith("seo_run") for k in partial):
        _build_schedule()  # áp lịch mới ngay
    return {"ok": True, "config": cfg}


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
            f"Chạy nền vài phút, kết quả ghi vào Google Sheet (tab {datetime.now().strftime('%Y-%m')}).")


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
        return f"Lần chạy gần nhất: {_state['last_run']} — kết quả: {_state['last_result']}."
    return "Chưa có lần chạy nào kể từ khi khởi động. Nói 'chạy kiểm tra' để bắt đầu."


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


def _execute_action(data: dict) -> str:
    action = data.get("action")
    if action == "run_check":
        return _do_trigger()
    if action == "status":
        return _status_text()
    if action == "list_urls":
        return _list_urls_text()
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
        "nêu số liệu cụ thể, có thể dùng **đậm** và gạch đầu dòng. KHÔNG dùng ký hiệu LaTeX "
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
    if any(k in m for k in ("phân tích", "kết quả", "điểm", "chậm nhất", "nhanh nhất", "so sánh", "analyze")):
        return {"action": "query_results"}
    if any(k in m for k in ("danh sách", "list", "url nào", "những url")):
        return {"action": "list_urls"}
    if any(k in m for k in ("chạy", "check", "kiểm tra", "run", "trigger", "start")):
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


def _sanitize_history(history: list[dict] | None, limit: int = 10) -> list[dict]:
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

    return (
        f"Hôm nay là {datetime.now().strftime('%Y-%m-%d')} (thứ {datetime.now().isoweekday()+1 if datetime.now().isoweekday()<7 else 'CN'}).\n"
        "Bạn là DeCho — AI agent all-in-one (app DeCho Agent, luôn khai báo là AI), quản lý 2 mảng:\n"
        f"A. PAGESPEED: kiểm tra Core Web Vitals cho {len(cfg['urls'])} URL, lịch {cfg['schedule_mode']} "
        f"lúc {cfg['schedule_time']}, ghi Google Sheet. Trạng thái: {_status_text()}\n"
        f"B. SEO: báo cáo GSC + GA4 hàng tháng cho {seo_agent.SITE_URL}, so sánh tháng trước, ghi Sheet riêng. "
        f"Trạng thái: {_seo_status_text()}\n"
        "C. PAID CAMPAIGNS: theo dõi Google Ads (read-only).\n"
        "Trả về DUY NHẤT một JSON theo intent:\n"
        '- Chạy kiểm tra PageSpeed ngay: {"action":"run_check"}\n'
        "QUY TẮC THỜI GIAN PAGESPEED — PageSpeed là phép đo REAL-TIME tại thời điểm chạy, KHÔNG thể chạy cho quá khứ hay tương lai:\n"
        '  + "chạy pagespeed tháng <quá khứ>" → hiểu là muốn XEM data đã đo tháng đó: {"action":"query_results","month":"YYYY-MM"}\n'
        '  + tháng TƯƠNG LAI → {"action":"reply","text":"<từ chối dí dỏm đúng tính cách: tháng đó chưa tới, Đệ chưa biết du hành thời gian; mời chạy ngay hoặc xem data tháng đã có>"}\n'
        '  + tháng hiện tại hoặc không nêu tháng → {"action":"run_check"}\n'
        '  + KHÔNG RÕ tháng nào/năm nào (vd nói "tháng 12" khi chưa rõ năm) → {"action":"reply","text":"<hỏi lại cho rõ tháng/năm>"} — đừng đoán.\n'
        '- Phân tích KẾT QUẢ PageSpeed (điểm, score, LCP/CLS, trang nhanh/chậm): {"action":"query_results"} — thêm "month":"YYYY-MM" nếu người dùng chỉ định tháng\n'
        '- Danh sách URL theo dõi: {"action":"list_urls"}\n'
        '- Thêm URL: {"action":"add_url","url":"<url>"}\n'
        '- Xóa URL: {"action":"remove_url","url":"<url hoặc từ khóa>"}\n'
        '- Đổi lịch PageSpeed: {"action":"set_schedule","schedule_mode":"daily|weekly|monthly","schedule_time":"HH:MM","schedule_day_of_month":<1-28>,"schedule_weekday":"<thứ>"} (chỉ kèm field người dùng nêu)\n'
        '- Chạy báo cáo SEO 1 tháng: {"action":"run_report","year":<năm>,"month":<1-12>} (bỏ year/month → tháng vừa rồi)\n'
        '- Chạy báo cáo SEO NHIỀU tháng TÁCH RIÊNG TỪNG THÁNG, mỗi tháng tự so với THÁNG LIỀN TRƯỚC (dùng khi user nói "so sánh từng tháng"/"tháng sau so tháng trước"/"backfill theo tháng"): {"action":"run_report","months":[{"year":2026,"month":3},{"year":2026,"month":4},...]}. KHÁC với seo_range (chỉ 1 kỳ + so với kỳ liền trước).\n'
        '- Phân tích số liệu SEO (traffic, views, users, clicks, impressions): {"action":"seo_query","month":"YYYY-MM hoặc bỏ"} '
        'hoặc nhiều tháng/xu hướng: {"action":"seo_query","months":["2026-01",...]} (tất cả: "months":"all")\n'
        '- Các tháng có báo cáo SEO: {"action":"list_months"}\n'
        '- Báo cáo SEO theo KHOẢNG THỜI GIAN tự nhiên ("3 tháng gần nhất", "tuần trước", "quý 1 2026", "từ 01/05 đến 12/06", "từ đầu năm đến nay", "cả năm 2025", "năm ngoái", "6 tháng đầu năm"...): '
        '{"action":"seo_range","start":"YYYY-MM-DD","end":"YYYY-MM-DD"} — TỰ TÍNH ngày từ hôm nay. '
        'Quy ước: "N tháng gần nhất" = N tháng TRỌN VẸN trước tháng hiện tại (vd hôm nay 2026-06-12 thì "3 tháng gần nhất" = 2026-03-01 → 2026-05-31); '
        '"N ngày gần nhất" = N ngày kết thúc hôm qua; "tuần trước" = thứ 2 → CN tuần trước. '
        'Nếu input mơ hồ không tính được ngày → đừng đoán, dùng {"action":"reply"} hỏi lại.\n'
        'LỌC URL: nếu user muốn chỉ một nhóm URL (vd "các url chứa /tutorial", "bài blog", "trang /product"), THÊM field "url_contains":"<từ khóa>" vào run_report HOẶC seo_range. Bỏ field này nếu xét toàn bộ.\n'
        '- Người dùng XÁC NHẬN đề xuất ngay trước đó ("ok", "đồng ý", "chạy đi"): {"action":"confirm"}\n'
        '- Danh sách campaign Google Ads: {"action":"ads_list"}\n'
        '- Hiệu suất/chi tiêu/CPA Google Ads: {"action":"ads_perf","days":<số ngày, mặc định 7>} '
        'hoặc khoảng thời gian tự nhiên ("ads tháng 5", "chi tiêu từ 01/05 đến 31/05", "quảng cáo quý 1"): '
        '{"action":"ads_perf","start":"YYYY-MM-DD","end":"YYYY-MM-DD"} — tự tính ngày như seo_range\n'
        '- Trạng thái hệ thống: {"action":"status"}\n'
        '- HƯỚNG DẪN / HỎI VỀ NĂNG LỰC ("DeCho làm được gì", "có tính năng nào", "làm sao thêm URL", "đổi lịch ở đâu", "LCP là gì", "score bao nhiêu là tốt", "lọc URL được không"): {"action":"help"}\n'
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
        "## Cấu hình (menu Cấu hình)\n"
        "- Thêm/xóa URL theo dõi, đổi lịch PageSpeed (daily/weekly/monthly + giờ), đổi lịch & URL theo dõi SEO — chỉnh bằng lời ('thêm https://...', 'đổi lịch sang daily 8h') hoặc trong trang Cấu hình. Lưu là áp dụng ngay + đồng bộ Google Sheet.\n"
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

    today = date.today()
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
    import re
    for pat in (r"(?:url|trang|bài|đường dẫn|path)\s*(?:nào\s*)?(?:có\s*)?chứa\s*[\"']?(/[\w\-/]+)",
                r"chứa\s*[\"']?(/[\w\-/]+)",
                r"(?:url|trang|bài|path)\s+(/[\w\-/]{2,})"):
        mt = re.search(pat, message, re.I)
        if mt:
            return mt.group(1).rstrip("/")
    return None


def _all_keyword_intent(message: str) -> dict | None:
    import re as _re2

    m = message.lower()
    if m.strip() in ("ok", "oke", "okay", "đồng ý", "dong y", "chạy đi", "chay di", "xác nhận", "xac nhan", "confirm", "yes", "lgtm"):
        return {"action": "confirm"}
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
    rng = _parse_range_vi(m)
    if rng and any(k in m for k in ("seo", "traffic", "báo cáo", "clicks", "gsc", "ga4")):
        uc = _parse_url_contains(message)
        return {**rng, **({"url_contains": uc} if uc else {})}
    if any(k in m for k in ("ads", "campaign", "quảng cáo", "cpa", "chi tiêu", "spend", "ngân sách")):
        if any(k in m for k in ("danh sách", "list", "những campaign", "campaign nào đang")):
            return {"action": "ads_list"}
        if rng:  # khoảng thời gian tự nhiên ("tháng 5", "quý 1", "từ đầu năm"...) → lọc ads theo range
            return {"action": "ads_perf", "start": rng["start"], "end": rng["end"]}
        tm = _re2.search(r"tháng\s*(\d{1,2})(?:\s*[/\-]?\s*(?:năm\s*)?(20\d{2}))?", m)
        if tm:
            from datetime import date, timedelta

            from dateutil.relativedelta import relativedelta
            mo, yr = int(tm.group(1)), int(tm.group(2) or datetime.now().year)
            if 1 <= mo <= 12:
                s = date(yr, mo, 1)
                return {"action": "ads_perf", "start": s.isoformat(),
                        "end": (s + relativedelta(months=1) - timedelta(days=1)).isoformat()}
        dm = _re2.search(r"(\d{1,2})\s*ngày", m)
        return {"action": "ads_perf", "days": int(dm.group(1)) if dm else 7}
    psiish = any(k in m for k in ("lcp", "cls", "fcp", "tbt", "inp", "ttfb", "score", "điểm",
                                  "pagespeed", "kiểm tra", "web vitals", "chậm", "nhanh"))
    seoish = any(k in m for k in ("seo", "traffic", "clicks", "gsc", "ga4", "impression",
                                  "báo cáo", "views", "users"))
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
            today = date.today()
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
        return kw_psi if kw_psi and kw_psi.get("action") != "run_check" or "chạy" in m or "kiểm tra" in m \
            else {"action": "query_results"}
    if seoish:
        return kw_seo or kw_psi
    return kw_psi or kw_seo


@app.post("/api/agent/chat/stream")
async def agent_chat_stream(req: ChatStreamRequest):
    """DeCho all-in-one: một chat xử lý mọi action của cả PageSpeed lẫn SEO."""
    import asyncio
    import re as _re

    import httpx

    model = req.model if req.model in ALLOWED_MODELS else MAAS_MODEL

    final_parts: list[str] = []  # gom câu trả lời để ghi AgentBase Memory

    async def gen():
        def ev(obj):
            if obj.get("type") == "final" and obj.get("text"):
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
                yield ev({"type": "step", "text": "🧠 Đệ nhớ ra vài điều liên quan về Đại ca"})
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

        labels = {"run_check": "Chạy kiểm tra PageSpeed", "query_results": "Phân tích kết quả PageSpeed",
                  "list_urls": "Liệt kê URL", "add_url": "Thêm URL", "remove_url": "Xóa URL",
                  "set_schedule": "Đổi lịch PageSpeed", "run_report": "Chạy báo cáo SEO", "seo_range": "Xác định khoảng thời gian SEO", "confirm": "Xác nhận & thực thi",
                  "seo_query": "Phân tích số liệu SEO", "list_months": "Các tháng có báo cáo SEO",
                  "ads_list": "Danh sách campaign Google Ads", "ads_perf": "Phân tích hiệu suất Google Ads",
                  "status": "Trạng thái hệ thống", "help": "Hướng dẫn năng lực", "reply": "Trả lời"}
        yield ev({"type": "step", "text": f"⚙️ Action: {labels.get(action, action)}"})

        async def stream_analysis(system_prompt: str):
            payload = {"model": model, "stream": True, "temperature": 0.2, "max_tokens": 4096,
                       "messages": [{"role": "system", "content": system_prompt + mem_block},
                                    *history, {"role": "user", "content": req.message}]}
            think_re = _re.compile(r"<think>.*?(?:</think>|$)", _re.S)
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
                            visible = think_re.sub("", raw_acc)
                            safe = max(0, len(visible) - 12)
                            if safe > sent:
                                yield ev({"type": "delta", "delta": visible[sent:safe]})
                                sent = safe
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi phân tích: {type(e).__name__}: {e}"})
                return
            visible = think_re.sub("", raw_acc).strip() or think_re.sub("", "".join(reasoning_acc)).strip()
            if visible:
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

        # ── PSI: quản lý URL / lịch ──
        if action in ("list_urls", "add_url", "remove_url", "set_schedule"):
            yield ev({"type": "final", "text": _execute_action(data)})
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
            yield ev({"type": "step", "text": f"📊 Đọc {len(rows)} dòng từ PSI Sheet tab {tab}"})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            extra = ("\nLƯU Ý: đây là dữ liệu ĐÃ ĐO trong tháng " + tab +
                     " (PageSpeed không đo lại quá khứ được). Kết thúc câu trả lời bằng đúng 1 câu mời theo tính cách: "
                     "nếu Đại ca muốn số liệu mới nhất thì nói 'chạy kiểm tra ngay' để Đệ đo liền.") if month else ""
            async for chunk in stream_analysis(_results_prompt(tab, headers, rows) + extra):
                yield chunk
            yield ev({"type": "done"})
            return

        # ── PSI: chạy kiểm tra real-time ──
        if action == "run_check":
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
            _DONE = object()
            try:
                it = psi_checker.run_check_iter()
                while True:
                    item = await asyncio.to_thread(next, it, _DONE)
                    if item is _DONE:
                        break
                    e = item["event"]
                    now = datetime.now().strftime("%H:%M:%S")
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
                _state["last_run"] = datetime.now().isoformat(timespec="seconds")
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
                yield ev({"type": "error", "text": f"❌ Lỗi Google Ads: {type(e).__name__}: {e}"})
                yield ev({"type": "done"})
                return
            cl = camps.get("campaigns", [])
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
                yield ev({"type": "error", "text": f"❌ Lỗi Google Ads: {type(e).__name__}: {e}"})
                yield ev({"type": "done"})
                return
            if not perf.get("rows"):
                rng_txt = f"khoảng {a_start} → {a_end}" if a_start else f"{days} ngày gần nhất"
                yield ev({"type": "final", "text": f"Không có dữ liệu Ads trong {rng_txt} — Đại ca thử khoảng khác xem."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"📊 {len(perf['rows'])} dòng ({perf['start']} → {perf['end']})"})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            async for chunk in stream_analysis(_ads_prompt(perf)):
                yield chunk
            yield ev({"type": "done"})
            return

        # ── SEO: liệt kê tháng ──
        if action == "list_months":
            try:
                tabs = await asyncio.to_thread(_seo_list_tabs)
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Không đọc được SEO Sheet: {type(e).__name__}: {e}"})
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
            today = _dt.now().date()
            if d1 > today:
                d1 = today
            days = (d1 - d0).days + 1
            if days > 366:
                yield ev({"type": "final", "text": f"Khoảng {days} ngày dài quá (tối đa 366) — Đại ca thu hẹp lại nhé."})
                yield ev({"type": "done"})
                return
            p1 = d0 - _td(days=1)
            p0 = p1 - _td(days=days - 1)
            uc = str(data.get("url_contains") or "").strip() or _parse_url_contains(req.message) or ""
            _pending_range.clear()
            _pending_range.update({"start": d0.isoformat(), "end": d1.isoformat(),
                                   "url_contains": uc, "ts": time.time()})
            flt_line = f"\n• **Lọc URL chứa**: `{uc}`" if uc else ""
            yield ev({"type": "step", "text": f"📅 Đã xác định khoảng: {d0} → {d1} ({days} ngày)" + (f" · lọc '{uc}'" if uc else "")})
            yield ev({"type": "final",
                      "text": (f"Đệ xác định được rồi nha:\n• **Khoảng lấy data**: {d0} → {d1} ({days} ngày)\n"
                               f"• **Kỳ so sánh tự động**: {p0} → {p1}{flt_line}\n• **Tên sheet**: {d0}__{d1}\n\n"
                               "Đúng ý thì Đại ca gõ **ok** (hoặc 'chạy đi') để Đệ chạy nhé.")})
            yield ev({"type": "done"})
            return

        # ── Bước 2: user xác nhận → chạy range đang chờ ──
        if action == "confirm":
            if not _pending_range.get("start") or time.time() - _pending_range.get("ts", 0) > 600:
                yield ev({"type": "final", "text": "Hiện không có đề xuất nào đang chờ xác nhận (hoặc đã quá 10 phút). Đại ca nêu lại yêu cầu nhé."})
                yield ev({"type": "done"})
                return
            if _seo_state["running"]:
                yield ev({"type": "final", "text": "Đang có báo cáo SEO chạy rồi — chờ xong đã nhé."})
                yield ev({"type": "done"})
                return
            rs, re_ = _pending_range["start"], _pending_range["end"]
            uc = _pending_range.get("url_contains") or ""
            _pending_range.clear()
            yield ev({"type": "step", "text": f"▶ Chạy báo cáo SEO khoảng {rs} → {re_}" + (f" · lọc '{uc}'" if uc else "")})
            log_pos = len(_seo_state["log"])
            t = threading.Thread(target=_run_seo_range_safe, args=(rs, re_, uc or None), daemon=True)
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
            months = data.get("months")
            if isinstance(months, str) and months != "all":
                months = [months]
            if months:
                try:
                    tabs_all = await asyncio.to_thread(_seo_list_tabs)
                except Exception as e:  # noqa: BLE001
                    yield ev({"type": "error", "text": f"❌ Không đọc được SEO Sheet: {type(e).__name__}: {e}"})
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
                    yield ev({"type": "error", "text": f"❌ Không đọc được SEO Sheet: {type(e).__name__}: {e}"})
                    yield ev({"type": "done"})
                    return
                summaries = []
                for t in sel:
                    headers, rows = data_map.get(t, ([], []))
                    summaries.append(_seo_month_summary(t, headers, rows))
                    yield ev({"type": "step", "text": f"📊 {t}: {len(rows)} URL"})
                yield ev({"type": "step", "text": f"🧠 Phân tích xu hướng {len(summaries)} tháng ({model})..."})
                async for chunk in stream_analysis(_seo_trend_prompt(summaries)):
                    yield chunk
                yield ev({"type": "done"})
                return
            try:
                tab, headers, rows = await asyncio.to_thread(_seo_read_results, data.get("month"))
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Không đọc được SEO Sheet: {type(e).__name__}: {e}"})
                yield ev({"type": "done"})
                return
            if not rows:
                yield ev({"type": "final", "text": "Chưa có báo cáo SEO nào — nói 'chạy báo cáo SEO' trước nhé."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"📊 Đọc {len(rows)} dòng từ SEO Sheet tab {tab}"})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            async for chunk in stream_analysis(_seo_results_prompt(tab, headers, rows)):
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
                threading.Thread(target=memory_agent.add_turns_safe,
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
    today = date.today()
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
def decho_ask(req: DechoAskRequest):
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
                threading.Thread(target=memory_agent.add_turns_safe,
                                 args=(req.user_id, req.session_id,
                                       [("user", req.question), ("assistant", reply)]), daemon=True).start()
            return {"reply": reply, "action": {"type": "ads_range", **rng}}
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
            threading.Thread(target=memory_agent.add_turns_safe,
                             args=(req.user_id, req.session_id,
                                   [("user", req.question), ("assistant", reply)]), daemon=True).start()
        return {"reply": reply}
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

@app.get("/api/seo/config")
def seo_config():
    import seo_agent

    cfg = runtime_config.current()
    return {
        "site": seo_agent.SITE_URL,
        "ga4_property": seo_agent.GA4_PROPERTY_ID,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{seo_agent.SEO_SHEET_ID}",
        "schedule": f"ngày {cfg.get('seo_run_day_of_month', 8)} hàng tháng lúc {cfg.get('seo_run_time', '08:00')}",
        "tracked_urls": cfg.get("seo_tracked_urls") or "tất cả",
        "token_configured": bool(os.getenv("SEO_TOKEN_JSON") or os.path.exists(seo_agent.TOKEN_FILE)),
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
        "dùng **đậm** và gạch đầu dòng. KHÔNG dùng LaTeX (mũi tên viết là →). Trả lời trực tiếp. /no_think"
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
        "\n\nTrả lời câu hỏi dựa trên dữ liệu trên. Yêu cầu: TIẾNG VIỆT, ngắn gọn (tối đa ~15 dòng), "
        "nêu số liệu cụ thể, dùng **đậm** và gạch đầu dòng khi phù hợp. KHÔNG dùng LaTeX "
        "(mũi tên viết là →). Trả lời trực tiếp, không suy luận dài. /no_think"
        + _knowledge() + _persona()
    )


def _seo_keyword_intent(message: str) -> dict | None:
    import re

    m = message.lower()
    month = None
    mm = re.search(r"(20\d{2})-(\d{1,2})", m)
    if mm:
        month = f"{mm.group(1)}-{int(mm.group(2)):02d}"
    if any(k in m for k in ("chạy", "run", "tạo báo cáo", "lấy báo cáo", "report")):
        # nhiều tháng: "chạy tháng 1, 2, 3 năm nay"
        seg = re.search(r"tháng\s+([\d\s,và]+)", m)
        if seg:
            nums = [int(n) for n in re.findall(r"\b(1[0-2]|[1-9])\b", seg.group(1))]
            if len(nums) > 1:
                ym = re.search(r"\b(20\d{2})\b", m)
                year = int(ym.group(1)) if ym else datetime.now().year
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
        return f"Lần chạy gần nhất: {_seo_state['last_run']} — {_seo_state['last_result']}"
    return "Chưa có lần chạy nào kể từ khi khởi động. Nói 'chạy báo cáo' để bắt đầu."


@app.post("/api/seo/chat/stream")
async def seo_chat_stream(req: ChatStreamRequest):
    """SSE chat cho SEO Agent: chạy báo cáo, đọc & phân tích số liệu từ SEO Sheet."""
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
                yield ev({"type": "error", "text": f"❌ Không đọc được SEO Sheet: {type(e).__name__}: {e}"})
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
                icon = "✅" if result.startswith("success") else "❌"
                yield ev({"type": "final", "text": f"{icon} {result}"})
            else:
                ok = sum(1 for _, r in results if r.startswith("success"))
                lines = "\n".join(f"{'✅' if r.startswith('success') else '❌'} {lb}: {r}" for lb, r in results)
                yield ev({"type": "final", "text": f"Xong {ok}/{len(results)} tháng:\n{lines}"})
            yield ev({"type": "done"})
            return

        async def stream_analysis(system_prompt: str):
            """Stream phân tích từ LLM: yield delta/final/error events."""
            payload = {"model": model, "stream": True, "temperature": 0.2, "max_tokens": 4096,
                       "messages": [{"role": "system", "content": system_prompt},
                                    *history,
                                    {"role": "user", "content": req.message}]}
            think_re = _re.compile(r"<think>.*?(?:</think>|$)", _re.S)
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
                            visible = think_re.sub("", raw_acc)
                            safe = max(0, len(visible) - 12)
                            if safe > sent:
                                yield ev({"type": "delta", "delta": visible[sent:safe]})
                                sent = safe
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Lỗi phân tích: {type(e).__name__}: {e}"})
                return
            visible = think_re.sub("", raw_acc).strip()
            if not visible:
                visible = think_re.sub("", "".join(reasoning_acc)).strip()
            if visible:
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
                    yield ev({"type": "error", "text": f"❌ Không đọc được SEO Sheet: {type(e).__name__}: {e}"})
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
                    yield ev({"type": "error", "text": f"❌ Không đọc được SEO Sheet: {type(e).__name__}: {e}"})
                    yield ev({"type": "done"})
                    return
                summaries = []
                for t in sel:
                    headers, rows = data_map.get(t, ([], []))
                    summaries.append(_seo_month_summary(t, headers, rows))
                    yield ev({"type": "step", "text": f"📊 {t}: {len(rows)} URL"})
                yield ev({"type": "step", "text": f"🧠 Phân tích xu hướng {len(summaries)} tháng ({model})..."})
                async for chunk in stream_analysis(_seo_trend_prompt(summaries)):
                    yield chunk
                yield ev({"type": "done"})
                return

            # ── 1 THÁNG: dữ liệu đầy đủ
            try:
                tab, headers, rows = await asyncio.to_thread(_seo_read_results, data.get("month"))
            except Exception as e:  # noqa: BLE001
                yield ev({"type": "error", "text": f"❌ Không đọc được SEO Sheet: {type(e).__name__}: {e}"})
                yield ev({"type": "done"})
                return
            if not rows:
                yield ev({"type": "final", "text": "Chưa có báo cáo nào trong SEO Sheet — nói 'chạy báo cáo' trước nhé."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"📊 Đọc {len(rows)} dòng từ tab {tab}"})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            async for chunk in stream_analysis(_seo_results_prompt(tab, headers, rows)):
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
        "messages": [{"role": "system", "content": (system or _system_prompt()) + "\nTrả về JSON ngay, không suy luận dài. /no_think"},
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
    visible = re.sub(r"<think>.*?(?:</think>|$)", "", raw_text, flags=re.S).strip()

    # Tìm JSON action: hỗ trợ cả MẢNG nhiều action lẫn object đơn.
    # Ưu tiên ký tự mở xuất hiện TRƯỚC — tránh vớ nhầm mảng con bên trong object
    # (vd {"action":"query_data","months":[...]}).
    def _try_parse(cand: str):
        pairs = sorted((p for p in (("{", "}"), ("[", "]")) if p[0] in cand and p[1] in cand),
                       key=lambda p: cand.index(p[0]))
        for op, cl in pairs:
            try:
                return json.loads(cand[cand.index(op):cand.rindex(cl) + 1])
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
    if visible:
        return {"action": "reply", "text": visible}
    return {"action": "reply", "text": "❌ Model không trả về nội dung."}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatStreamRequest):
    """SSE: hiển thị các bước hành động của agent + tiến trình check real-time.

    Events: {"type":"step","text"} | {"type":"final","text"} | {"type":"error","text"} | {"type":"done"}
    """
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
                            visible = think_re.sub("", raw_acc)
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
                now = datetime.now().strftime("%H:%M:%S")
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
            _state["last_run"] = datetime.now().isoformat(timespec="seconds")
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
            reply = _add_url(url_match.group(0).rstrip(".,;"))
        elif any(k in m for k in ("xóa", "xoá", "remove", "delete", "bỏ")) and (url_match or True) and any(
                k in m for k in ("url", "http", "trang", "link")):
            target = url_match.group(0).rstrip(".,;") if url_match else req.message.split()[-1]
            reply = _remove_url(target)
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
