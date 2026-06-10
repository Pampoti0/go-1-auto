"""HTTP wrapper cho PSI Checker — phục vụ deploy lên GreenNode AgentBase.

Endpoints:
  GET  /            — trang trạng thái
  GET  /healthz     — health check (BTC dùng để chấm PASS)
  POST /api/check   — chạy kiểm tra PSI ngay (chạy nền, ghi vào Google Sheet)
  GET  /api/status  — trạng thái lần chạy gần nhất

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

app = FastAPI(title="PageSpeed Checker Agent")

_state = {"running": False, "last_run": None, "last_result": None}
_lock = threading.Lock()


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


@app.put("/api/config")
def put_config(body: ConfigUpdate):
    partial = {k: v for k, v in body.model_dump().items() if v is not None}
    if not partial:
        return {"ok": False, "error": "Không có trường nào để cập nhật."}
    try:
        cfg = runtime_config.update(partial)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if any(k.startswith("schedule") for k in partial):
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


@app.get("/api/models")
def models():
    return {"models": ALLOWED_MODELS, "default": MAAS_MODEL}


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


async def _call_llm_stream(model: str, message: str) -> dict:
    """Gọi MaaS (stream để nhận sớm), nuốt phần <think>, trả về JSON action."""
    import httpx

    payload = {
        "model": model, "stream": True, "temperature": 0, "max_tokens": 4096,
        # /no_think: tắt thinking mode của Qwen cho bước phân loại intent (không cần suy luận dài)
        "messages": [{"role": "system", "content": _system_prompt() + "\nTrả về JSON ngay, không suy luận dài. /no_think"},
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

    # Tìm JSON action: ưu tiên phần ngoài think → toàn bộ content → reasoning
    for cand in (visible, raw_text, "".join(reasoning_parts)):
        if "{" in cand and "}" in cand:
            try:
                return json.loads(cand[cand.index("{"):cand.rindex("}") + 1])
            except Exception:  # noqa: BLE001
                continue
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

        yield ev({"type": "step", "text": f"🧠 Phân tích yêu cầu ({model})..."})
        try:
            data = await _call_llm_stream(model, req.message)
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
