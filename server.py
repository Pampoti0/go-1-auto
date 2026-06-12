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


def _persona() -> str:
    """SOUL.md — TÍNH CÁCH: nhúng vào mọi prompt có sinh văn bản cho người dùng."""
    return _md_file("SOUL_FILE", "SOUL.md",
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


def _run_seo_safe(year: int | None = None, month: int | None = None):
    with _seo_lock:
        if _seo_state["running"]:
            return
        _seo_state["running"] = True
    try:
        import seo_agent

        if year and month:
            result = seo_agent.run_for_month(year, month)
        else:
            result = seo_agent.run()
        _seo_state["last_result"] = f"success: {result['rows']} URL → tab {result['label']}"
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
    try:
        tabs = _seo_list_tabs()[-max(1, min(limit, 12)):]
        out = []
        for t in tabs:
            tab, h, rows = _seo_read_results(t, 2000)
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
        "Bạn là DeCho — AI agent all-in-one (app DeCho Agent, luôn khai báo là AI), quản lý 2 mảng:\n"
        f"A. PAGESPEED: kiểm tra Core Web Vitals cho {len(cfg['urls'])} URL, lịch {cfg['schedule_mode']} "
        f"lúc {cfg['schedule_time']}, ghi Google Sheet. Trạng thái: {_status_text()}\n"
        f"B. SEO: báo cáo GSC + GA4 hàng tháng cho {seo_agent.SITE_URL}, so sánh tháng trước, ghi Sheet riêng. "
        f"Trạng thái: {_seo_status_text()}\n"
        "Trả về DUY NHẤT một JSON theo intent:\n"
        '- Chạy kiểm tra PageSpeed ngay: {"action":"run_check"}\n'
        '- Phân tích KẾT QUẢ PageSpeed (điểm, score, LCP/CLS, trang nhanh/chậm): {"action":"query_results"}\n'
        '- Danh sách URL theo dõi: {"action":"list_urls"}\n'
        '- Thêm URL: {"action":"add_url","url":"<url>"}\n'
        '- Xóa URL: {"action":"remove_url","url":"<url hoặc từ khóa>"}\n'
        '- Đổi lịch PageSpeed: {"action":"set_schedule","schedule_mode":"daily|weekly|monthly","schedule_time":"HH:MM","schedule_day_of_month":<1-28>,"schedule_weekday":"<thứ>"} (chỉ kèm field người dùng nêu)\n'
        '- Chạy báo cáo SEO 1 tháng: {"action":"run_report","year":<năm>,"month":<1-12>} (bỏ year/month → tháng vừa rồi)\n'
        '- Chạy báo cáo SEO NHIỀU tháng: {"action":"run_report","months":[{"year":2026,"month":1},...]}\n'
        '- Phân tích số liệu SEO (traffic, views, users, clicks, impressions): {"action":"seo_query","month":"YYYY-MM hoặc bỏ"} '
        'hoặc nhiều tháng/xu hướng: {"action":"seo_query","months":["2026-01",...]} (tất cả: "months":"all")\n'
        '- Các tháng có báo cáo SEO: {"action":"list_months"}\n'
        '- Trạng thái hệ thống: {"action":"status"}\n'
        '- Còn lại: {"action":"reply","text":"<trả lời ngắn>"}\n'
        "Phân biệt: kiểm tra/điểm/score/LCP/CLS/pagespeed → PageSpeed; báo cáo/traffic/clicks/GSC/GA4/SEO → SEO."
    ) + _persona()


def _all_keyword_intent(message: str) -> dict | None:
    m = message.lower()
    psiish = any(k in m for k in ("lcp", "cls", "fcp", "tbt", "inp", "ttfb", "score", "điểm",
                                  "pagespeed", "kiểm tra", "web vitals", "chậm", "nhanh"))
    seoish = any(k in m for k in ("seo", "traffic", "clicks", "gsc", "ga4", "impression",
                                  "báo cáo", "views", "users"))
    kw_seo = _seo_keyword_intent(message)
    if kw_seo and kw_seo.get("action") == "query_data":
        kw_seo = {**kw_seo, "action": "seo_query"}
    kw_psi = _keyword_intent(message)
    if psiish and not seoish:
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
            data = await _call_llm_stream(model, req.message, history, system=_unified_prompt())
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
                  "set_schedule": "Đổi lịch PageSpeed", "run_report": "Chạy báo cáo SEO",
                  "seo_query": "Phân tích số liệu SEO", "list_months": "Các tháng có báo cáo SEO",
                  "status": "Trạng thái hệ thống", "reply": "Trả lời"}
        yield ev({"type": "step", "text": f"⚙️ Action: {labels.get(action, action)}"})

        async def stream_analysis(system_prompt: str):
            payload = {"model": model, "stream": True, "temperature": 0.2, "max_tokens": 4096,
                       "messages": [{"role": "system", "content": system_prompt},
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

        # ── PSI: quản lý URL / lịch ──
        if action in ("list_urls", "add_url", "remove_url", "set_schedule"):
            yield ev({"type": "final", "text": _execute_action(data)})
            yield ev({"type": "done"})
            return

        # ── PSI: phân tích kết quả ──
        if action == "query_results":
            tab, headers, rows = await asyncio.to_thread(sheet_store.read_results)
            if not rows:
                yield ev({"type": "final", "text": "Chưa có dữ liệu PageSpeed nào — nói 'chạy kiểm tra ngay' trước nhé."})
                yield ev({"type": "done"})
                return
            yield ev({"type": "step", "text": f"📊 Đọc {len(rows)} dòng từ PSI Sheet tab {tab}"})
            yield ev({"type": "step", "text": f"🧠 Phân tích dữ liệu ({model})..."})
            async for chunk in stream_analysis(_results_prompt(tab, headers, rows)):
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

        # ── SEO: chạy báo cáo (1 hoặc nhiều tháng) ──
        if action == "run_report":
            if _seo_state["running"]:
                yield ev({"type": "final", "text": "Đang có báo cáo SEO chạy rồi — chờ xong đã nhé."})
                yield ev({"type": "done"})
                return
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
                    yield ev({"type": "step", "text": f"▶ [{idx}/{len(jobs)}] Chạy báo cáo {label}"})
                log_pos = len(_seo_state["log"])
                t = threading.Thread(target=_run_seo_safe, args=(y, m), daemon=True)
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
                summaries = []
                for t in sel:
                    try:
                        tab, headers, rows = await asyncio.to_thread(_seo_read_results, t, 2000)
                        summaries.append(_seo_month_summary(tab, headers, rows))
                        yield ev({"type": "step", "text": f"📊 {tab}: {len(rows)} URL"})
                    except Exception as e:  # noqa: BLE001
                        yield ev({"type": "step", "text": f"⚠️ {t}: lỗi đọc — {type(e).__name__}"})
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

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class DechoAskRequest(BaseModel):
    question: str
    context: str | None = None
    model: str | None = None


@app.post("/api/decho/ask")
def decho_ask(req: DechoAskRequest):
    """Hỏi đáp nhanh với DeCho — có bối cảnh màn hình người dùng đang xem."""
    if not (MAAS_API_KEY and MAAS_BASE_URL):
        return {"error": "Chưa cấu hình MAAS_API_KEY / MAAS_BASE_URL."}
    import httpx

    model = req.model if req.model in ALLOWED_MODELS else MAAS_MODEL
    system = (
        "Bạn là DeCho — mascot trợ thủ của app DeCho Agent (PageSpeed + SEO). "
        "Bạn đang đứng ở góc màn hình, nhìn cùng màn hình với người dùng.\n"
        "# BỐI CẢNH MÀN HÌNH HIỆN TẠI\n" + (req.context or "(không rõ)") +
        "\n\nTrả lời câu hỏi NGẮN GỌN (tối đa ~80 từ), bám sát bối cảnh trên; "
        "nếu câu hỏi vượt quá dữ liệu đang thấy thì nói thẳng và chỉ người dùng nơi xem "
        "(menu Chat để chạy/phân tích, Dashboard để xem điểm). KHÔNG dùng LaTeX. /no_think"
    ) + _knowledge() + _persona()
    try:
        r = httpx.post(
            f"{MAAS_BASE_URL}/chat/completions",
            json={"model": model, "temperature": 0.4, "max_tokens": 1024,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": req.question}]},
            headers={"Authorization": f"Bearer {MAAS_API_KEY}"}, timeout=90)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        reply = _strip_think(msg.get("content") or "") or _strip_think(msg.get("reasoning_content") or "")
        return {"reply": reply or "Đệ bí câu này rồi Đại ca 😅"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


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
        labels = {"run_report": "Chạy báo cáo SEO", "status": "Xem trạng thái",
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
                    yield ev({"type": "step", "text": f"▶ [{idx}/{len(jobs)}] Chạy báo cáo {label}"})
                log_pos = len(_seo_state["log"])
                t = threading.Thread(target=_run_seo_safe, args=(y, m), daemon=True)
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
                summaries = []
                for t in sel:
                    try:
                        tab, headers, rows = await asyncio.to_thread(_seo_read_results, t, 2000)
                        summaries.append(_seo_month_summary(tab, headers, rows))
                        yield ev({"type": "step", "text": f"📊 {tab}: {len(rows)} URL"})
                    except Exception as e:  # noqa: BLE001
                        yield ev({"type": "step", "text": f"⚠️ {t}: lỗi đọc — {type(e).__name__}"})
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
