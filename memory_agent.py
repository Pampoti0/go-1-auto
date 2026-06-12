"""Memory module của DeCho Agent — GreenNode AgentBase Memory Service (REST).

Short-term: events (từng lượt chat, hết hạn theo eventExpiryDuration).
Long-term: memory records (fact tự chưng cất theo strategy, search vector).

Credentials: GREENNODE_CLIENT_ID / GREENNODE_CLIENT_SECRET — trên AgentBase Runtime
được tự inject; local dev đọc fallback từ .greennode.json (gitignored).
Cấu hình: MEMORY_ID (bắt buộc), MEMORY_STRATEGY_ID (tùy chọn — tự dò nếu thiếu).
Mọi hàm *_safe đều nuốt lỗi: memory hỏng không được làm hỏng chat.
"""

import json
import logging
import os
import threading
import time

import httpx

log = logging.getLogger("memory_agent")

IAM_TOKEN_URL = "https://iam.api.vngcloud.vn/accounts-api/v2/auth/token"
MEMORY_BASE = os.getenv("MEMORY_BASE_URL", "https://agentbase.api.vngcloud.vn/memory")
MEMORY_ID = os.getenv("MEMORY_ID", "")


def _creds() -> tuple[str, str]:
    cid = os.getenv("GREENNODE_CLIENT_ID", "")
    sec = os.getenv("GREENNODE_CLIENT_SECRET", "")
    if not (cid and sec) and os.path.exists(".greennode.json"):
        try:
            with open(".greennode.json", encoding="utf-8") as f:
                j = json.load(f)
            cid, sec = cid or j.get("client_id", ""), sec or j.get("client_secret", "")
        except Exception:  # noqa: BLE001
            pass
    return cid, sec


def configured() -> bool:
    cid, sec = _creds()
    return bool(MEMORY_ID and cid and sec)


_tok_lock = threading.Lock()
_tok: dict = {"value": "", "exp": 0.0}


def _token(force: bool = False) -> str:
    with _tok_lock:
        if not force and _tok["value"] and time.time() < _tok["exp"] - 60:
            return _tok["value"]
        cid, sec = _creds()
        r = httpx.post(IAM_TOKEN_URL, auth=(cid, sec),
                       data={"grant_type": "client_credentials"},
                       headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        _tok["value"] = j["access_token"]
        _tok["exp"] = time.time() + int(j.get("expires_in") or 1500)
        return _tok["value"]


def _req(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict | list:
    url = f"{MEMORY_BASE}{path}"
    for attempt in (1, 2):
        r = httpx.request(method, url, params=params, json=body, timeout=30,
                          headers={"Authorization": f"Bearer {_token(force=attempt == 2)}"})
        if r.status_code == 401 and attempt == 1:
            continue  # token hết hạn → refresh rồi thử lại
        r.raise_for_status()
        return r.json() if r.text else {}
    return {}


def _items(resp) -> list:
    """API có thể bọc list theo nhiều key — không đoán cứng cấu trúc."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("listData", "list_data", "data", "items", "content", "records", "events", "memoryRecords"):
            v = resp.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):  # bọc 2 lớp
                inner = _items(v)
                if inner:
                    return inner
    return []


_strategy: dict = {"id": os.getenv("MEMORY_STRATEGY_ID", ""), "checked": False}


def strategy_id() -> str:
    """Strategy id: env override, không thì tự dò strategy đầu tiên của memory."""
    if _strategy["id"] or _strategy["checked"]:
        return _strategy["id"]
    try:
        items = _items(_req("GET", f"/memories/{MEMORY_ID}/long-term-memory-strategies"))
        if items:
            _strategy["id"] = str(items[0].get("id") or "")
            log.info(f"Memory strategy tự dò: {_strategy['id']}")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Không dò được strategy id: {e}")
    _strategy["checked"] = True
    return _strategy["id"]


def _namespace(actor: str) -> str:
    return f"/strategies/{strategy_id()}/actors/{actor}"


# ── Short-term: events ──

def add_event(actor: str, session: str, role: str, message: str):
    _req("POST", f"/memories/{MEMORY_ID}/actors/{actor}/sessions/{session}/events",
         body={"payload": {"type": "conversational", "role": role, "message": message[:100_000]}})


def add_turns_safe(actor: str, session: str, turns: list[tuple[str, str]]):
    """Ghi nhiều lượt (role, message) — gọi trong thread nền, nuốt lỗi."""
    for role, msg in turns:
        if not (msg or "").strip():
            continue
        try:
            add_event(actor, session, role, msg)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Memory add_event lỗi (bỏ qua): {type(e).__name__}: {e}")
            return


def get_events(actor: str, session: str, size: int = 40) -> list[dict]:
    """Lịch sử hội thoại theo thứ tự thời gian: [{role, message}]."""
    resp = _req("GET", f"/memories/{MEMORY_ID}/actors/{actor}/sessions/{session}/events",
                params={"page": 1, "size": size})
    out = []
    for it in _items(resp):
        p = it.get("payload") if isinstance(it, dict) else None
        p = p if isinstance(p, dict) else (it if isinstance(it, dict) else {})
        role, msg = p.get("role"), p.get("message")
        if role and msg:
            out.append({"role": role, "message": msg})
    return list(reversed(out))  # API trả mới nhất trước → đảo lại


def get_events_safe(actor: str, session: str, size: int = 40) -> list[dict]:
    try:
        return get_events(actor, session, size)
    except Exception as e:  # noqa: BLE001
        log.warning(f"Memory get_events lỗi (bỏ qua): {type(e).__name__}: {e}")
        return []


# ── Long-term: records ──

def _record_text(it) -> str:
    if not isinstance(it, dict):
        return str(it)
    v = it.get("memory") or it.get("content") or it.get("text") or ""
    if isinstance(v, dict):
        v = v.get("text") or v.get("memory") or json.dumps(v, ensure_ascii=False)
    return str(v)


def recall(actor: str, query: str, k: int = 5, threshold: float = 0.45) -> list[str]:
    """Vector search fact dài hạn của actor theo câu hỏi hiện tại."""
    if not strategy_id():
        return []
    resp = _req("POST", f"/memories/{MEMORY_ID}/memory-records:search",
                params={"namespace": _namespace(actor)},
                body={"query": query[:1000], "limit": max(5, k), "scoreThreshold": threshold})
    return [t for t in (_record_text(it) for it in _items(resp)[:k]) if t.strip()]


_recall_cache: dict = {}


def recall_safe(actor: str, query: str, k: int = 5) -> list[str]:
    key = f"{actor}:{(query or '')[:120]}"
    hit = _recall_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        facts = recall(actor, query, k)
    except Exception as e:  # noqa: BLE001
        log.warning(f"Memory recall lỗi (bỏ qua): {type(e).__name__}: {e}")
        facts = []
    if len(_recall_cache) > 200:
        _recall_cache.clear()
    _recall_cache[key] = (time.time(), facts)
    return facts


def memory_block(actor: str, query: str) -> str:
    """Block chèn vào system prompt — rỗng nếu không có fact."""
    facts = recall_safe(actor, query)
    if not facts:
        return ""
    return ("\n# TRÍ NHỚ DÀI HẠN VỀ NGƯỜI DÙNG (từ các hội thoại trước — dùng khi liên quan, đừng nhắc lại máy móc)\n"
            + "\n".join(f"- {f}" for f in facts))


def list_records(actor: str, limit: int = 50) -> list[str]:
    if not strategy_id():
        return []
    resp = _req("GET", f"/memories/{MEMORY_ID}/memory-records",
                params={"namespace": _namespace(actor), "limit": limit})
    return [t for t in (_record_text(it) for it in _items(resp)) if t.strip()]
