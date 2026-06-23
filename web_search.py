"""Web Search cho DeCho Agent.

Provider:
- Tavily (khuyến nghị cho agent) nếu có TAVILY_API_KEY / SEARCH_API_KEY.
- Fallback DuckDuckGo Instant Answer (không cần key, kết quả hạn chế).

Trên AgentBase nhớ allowlist domain (api.tavily.com / api.duckduckgo.com).
Mọi hàm fail-soft: lỗi mạng không làm sập chat.
"""

import logging
import os
from datetime import date

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


def _rss_search(url: str, params: dict, k: int) -> list[dict]:
    import html as _html
    import re
    import xml.etree.ElementTree as ET

    r = httpx.get(url, timeout=20, params=params,
                  headers={"User-Agent": "Mozilla/5.0 (DeCho-Agent RSS search)"})
    r.raise_for_status()
    root = ET.fromstring(r.text)

    def text(node, name: str) -> str:
        child = node.find(name)
        return (child.text or "").strip() if child is not None else ""

    def strip(s: str) -> str:
        return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

    out = []
    for item in root.findall(".//item"):
        title = strip(text(item, "title"))
        link = strip(text(item, "link"))
        desc = strip(text(item, "description"))[:500]
        if title and link.startswith("http"):
            out.append({"title": title[:140], "url": link, "snippet": desc})
        if len(out) >= k:
            break
    return out


def _bing_rss(query: str, k: int) -> list[dict]:
    return _rss_search("https://www.bing.com/search", {"q": query, "format": "rss"}, k)


def _google_news_rss(query: str, k: int) -> list[dict]:
    return _rss_search("https://news.google.com/rss/search",
                       {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}, k)


def _norm_query(s: str) -> str:
    import re
    import unicodedata

    raw = unicodedata.normalize("NFD", s or "")
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", raw.lower()).strip()


def _query_variants(query: str) -> list[str]:
    """Sinh vài query fallback cho câu tiếng Việt/broad/latest."""
    year = date.today().year
    q = (query or "").strip()
    norm = _norm_query(q)
    variants = [q]

    def add(v: str):
        v = " ".join((v or "").split())
        if v and v.lower() not in {x.lower() for x in variants}:
            variants.append(v)

    translated = _translated_query(q)
    terms = _important_terms(q)
    subject = " ".join(terms[:4]) or translated or q
    latestish = any(k in norm for k in ("moi nhat", "latest", "tin moi", "cap nhat", "ra mat", "announcement", "launch"))
    productish = any(k in norm for k in ("san pham", "product", "thong tin", "spec", "thong so"))
    priceish = any(k in norm for k in ("gia", "price", "pricing", "chi phi", "cost", "bao nhieu"))
    compareish = any(k in norm for k in ("so sanh", "compare", "vs", "doi thu", "competitor"))

    if translated and translated != norm:
        add(translated)
    if latestish or productish:
        add(f"{subject} latest product announcements {year}")
        add(f"{subject} product news {year}")
        base = translated or subject
        add(f"{base} {'news' if 'latest' in base else 'latest news'} {year}")
    if priceish:
        add(f"{subject} price pricing {year}")
        if any(k in norm for k in ("cloud", "thue", "rent", "rental")):
            add(f"{subject} cloud rental pricing {year}")
    if compareish:
        add(f"{subject} comparison alternatives {year}")
    return variants[:5]


def _translated_query(query: str) -> str:
    s = _norm_query(query)
    replacements = (
        ("bang gia", "pricing"),
        ("bao nhieu", "price"),
        ("chi phi", "cost"),
        ("moi nhat", "latest"),
        ("cap nhat", "latest"),
        ("tin moi", "latest news"),
        ("thong tin", "information"),
        ("san pham", "product"),
        ("thong so", "specs"),
        ("cau hinh", "specs"),
        ("ra mat", "launch announcement"),
        ("so sanh", "comparison"),
        ("doi thu", "competitors"),
        ("gia", "price"),
        ("mua", "buy"),
        ("thue", "rent"),
    )
    for src, dst in replacements:
        s = s.replace(src, dst)
    stop = {"cua", "ve", "cho", "la", "ai", "gi", "nao", "mot", "cac", "nhung"}
    return " ".join(t for t in s.split() if t not in stop)


def _important_terms(query: str) -> list[str]:
    import re

    raw_terms = re.findall(r"[A-Za-z][A-Za-z0-9+._-]*|\d+[A-Za-z0-9+._-]*", query or "")
    norm = _translated_query(query)
    intent_stop = {
        "information", "product", "products", "latest", "news", "announcement", "launch",
        "price", "pricing", "cost", "buy", "rent", "comparison", "competitors", "specs",
        "thong", "tin", "san", "pham", "moi", "nhat", "gia", "cua", "nam",
    }
    out = []

    def add(t: str):
        n = _norm_query(t)
        if n and n not in intent_stop and n not in out and len(n) > 1:
            out.append(n)

    for t in raw_terms:
        if t.isdigit() and re.fullmatch(r"(?:19|20)\d{2}", t):
            continue
        if t.isupper() or any(ch.isdigit() for ch in t) or (any(ch.isupper() for ch in t[1:]) and len(t) > 2):
            add(t)
    for t in norm.split():
        if len(out) >= 4:
            break
        if t not in intent_stop and not t.isdigit():
            add(t)
    return out


def _dedupe(results: list[dict], k: int) -> list[dict]:
    out, seen = [], set()
    for r in results or []:
        url = (r.get("url") or "").split("#")[0]
        title = (r.get("title") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({**r, "title": title, "url": url})
        if len(out) >= k:
            break
    return out


def _filter_relevant(results: list[dict], query: str) -> list[dict]:
    from urllib.parse import urlparse

    norm = _norm_query(query)
    must = _important_terms(query)[:3]
    broad = any(k in norm for k in ("moi nhat", "latest", "san pham", "product", "gia", "price", "pricing"))
    generic_titles = (
        "wikipedia",
        "download",
        "driver",
        "login",
        "sign in",
        "homepage",
        "home page",
    )
    out = []
    for r in results or []:
        title_norm = _norm_query(r.get("title") or "")
        blob = _norm_query(" ".join([r.get("title") or "", r.get("snippet") or "", r.get("url") or ""]))
        if broad and any(g in title_norm for g in generic_titles):
            continue
        path = urlparse(r.get("url") or "").path.rstrip("/").lower()
        shallow = path in ("", "/", "/en", "/en-us", "/vi", "/vi-vn", "/index")
        intent_hit = any(k in blob for k in (
            "latest", "new", "news", "product", "announcement", "launch", "price", "pricing", "cost", "spec", "review",
            "moi nhat", "san pham", "gia",
        ))
        if broad and shallow and not intent_hit:
            continue
        if must and not all(t in blob for t in must):
            continue
        out.append(r)
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
    queries = _query_variants(query)
    try:
        if TAVILY_KEY:
            results = _dedupe(_tavily(query, k), k)
            log.info(f"web_search '{query[:60]}' → {len(results)} kết quả (tavily)")
            if results:
                return results
    except Exception as e:  # noqa: BLE001
        log.warning(f"web_search lỗi: {type(e).__name__}: {e}")

    # Keyless fallback: thử query gốc + variants, nhất là khi Tavily rỗng/hỏng.
    merged = []
    for q in queries:
        for label, provider in (("ddg", _duckduckgo), ("bing-rss", _bing_rss), ("google-news-rss", _google_news_rss)):
            try:
                got = _filter_relevant(provider(q, k), q)
                if got:
                    log.info(f"web_search fallback '{q[:60]}' → {len(got)} kết quả ({label})")
                    merged.extend(got)
                    deduped = _dedupe(merged, k)
                    if len(deduped) >= k:
                        return deduped
            except Exception as e:  # noqa: BLE001
                log.warning(f"web_search fallback lỗi {label} ({q[:50]}): {type(e).__name__}: {e}")
    results = _dedupe(merged, k)
    log.info(f"web_search '{query[:60]}' → {len(results)} kết quả sau fallback")
    return results
