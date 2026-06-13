"""Web Search cho DeCho Agent.

Provider:
- Tavily (khuyến nghị cho agent) nếu có TAVILY_API_KEY / SEARCH_API_KEY.
- Fallback DuckDuckGo Instant Answer (không cần key, kết quả hạn chế).

Trên AgentBase nhớ allowlist domain (api.tavily.com / api.duckduckgo.com).
Mọi hàm fail-soft: lỗi mạng không làm sập chat.
"""

import logging
import os

import httpx

log = logging.getLogger("web_search")

TAVILY_KEY = os.getenv("TAVILY_API_KEY", "") or os.getenv("SEARCH_API_KEY", "")


def configured() -> bool:
    return True  # luôn dùng được (Tavily nếu có key, không thì DuckDuckGo)


def _tavily(query: str, k: int) -> list[dict]:
    r = httpx.post("https://api.tavily.com/search", timeout=20, json={
        "api_key": TAVILY_KEY, "query": query, "max_results": max(1, min(k, 10)),
        "search_depth": "basic", "include_answer": False,
    })
    r.raise_for_status()
    out = []
    for it in (r.json().get("results") or [])[:k]:
        out.append({"title": it.get("title") or "", "url": it.get("url") or "",
                    "snippet": (it.get("content") or "")[:500]})
    return out


def _ddg_html(query: str, k: int) -> list[dict]:
    """DuckDuckGo HTML (keyless) — trả về link kết quả thật, parse bằng regex."""
    import html as _html
    import re
    from urllib.parse import parse_qs, unquote, urlparse

    r = httpx.post("https://html.duckduckgo.com/html/", timeout=20,
                   data={"q": query, "kl": "vn-vi"},
                   headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    r.raise_for_status()
    h = r.text
    links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', h, re.I | re.S)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', h, re.I | re.S)
    strip = lambda s: _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
    out = []
    for idx, (href, title) in enumerate(links):
        snip = snips[idx] if idx < len(snips) else ""
        if href.startswith("//duckduckgo.com/l/") or "uddg=" in href:
            qs = parse_qs(urlparse(("https:" + href) if href.startswith("//") else href).query)
            href = unquote(qs.get("uddg", [href])[0])
        t = strip(title)
        if not t or not href.startswith("http"):
            continue
        out.append({"title": t[:120], "url": href, "snippet": strip(snip)[:500]})
        if len(out) >= k:
            break
    return out


def _duckduckgo(query: str, k: int) -> list[dict]:
    # ưu tiên kết quả tìm kiếm thật (HTML); nếu trống thử Instant Answer
    try:
        res = _ddg_html(query, k)
        if res:
            return res
    except Exception as e:  # noqa: BLE001
        log.warning(f"ddg html lỗi: {e}")
    r = httpx.get("https://api.duckduckgo.com/", timeout=20,
                  params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
    r.raise_for_status()
    j = r.json()
    out = []
    if j.get("AbstractText"):
        out.append({"title": j.get("Heading") or query,
                    "url": j.get("AbstractURL") or "", "snippet": j["AbstractText"][:500]})
    for t in (j.get("RelatedTopics") or []):
        if len(out) >= k:
            break
        if isinstance(t, dict) and t.get("Text") and t.get("FirstURL"):
            out.append({"title": t["Text"][:80], "url": t["FirstURL"], "snippet": t["Text"][:500]})
    return out


def fetch_url(url: str, max_chars: int = 6000) -> dict:
    """Tải 1 URL và trích text đọc được (không cần browser).

    Trả {title, url, text}. JS-render nặng có thể trống — khi đó cần trình duyệt thật.
    """
    import html as _html
    import re

    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    r = httpx.get(url, timeout=25, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (DeCho-Agent web_fetch)"})
    r.raise_for_status()
    h = r.text
    title = ""
    mt = re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S)
    if mt:
        title = _html.unescape(re.sub(r"\s+", " ", mt.group(1))).strip()[:200]
    # bỏ script/style/noscript/svg rồi strip tag
    h = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?is)<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", h)
    text = re.sub(r"(?is)<[^>]+>", " ", h)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    log.info(f"web_fetch '{url[:70]}' → {len(text)} ký tự")
    return {"title": title, "url": url, "text": text[:max_chars]}


def search(query: str, k: int = 5) -> list[dict]:
    """Trả về [{title, url, snippet}] — rỗng nếu không có kết quả."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        results = _tavily(query, k) if TAVILY_KEY else _duckduckgo(query, k)
        log.info(f"web_search '{query[:60]}' → {len(results)} kết quả ({'tavily' if TAVILY_KEY else 'ddg'})")
        return results
    except Exception as e:  # noqa: BLE001
        log.warning(f"web_search lỗi: {type(e).__name__}: {e}")
        # thử fallback nếu Tavily lỗi
        if TAVILY_KEY:
            try:
                return _duckduckgo(query, k)
            except Exception:  # noqa: BLE001
                pass
        return []
