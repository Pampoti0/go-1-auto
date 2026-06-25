"""Self-learning marketing entity catalog and resolver.

The resolver learns lightweight entities from first-party sources that DeCho
already has: tracked URLs and Google Ads campaign/landing rows. It is purposely
deterministic so natural intent words do not become filters.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse


def norm(text: str) -> str:
    raw = unicodedata.normalize("NFD", text or "")
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    raw = raw.replace("đ", "d").replace("Đ", "D")
    raw = re.sub(r"([0-9])([A-Z][a-z])", r"\1 \2", raw)
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    raw = raw.lower()
    raw = re.sub(r"[_\-./]+", " ", raw)
    raw = re.sub(r"[^a-z0-9\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm(text))


def sep_norm(text: str) -> str:
    raw = unicodedata.normalize("NFD", text or "")
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
    raw = raw.replace("đ", "d").replace("Đ", "D")
    raw = raw.lower()
    raw = re.sub(r"[^a-z0-9_\-/\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _has_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    hay = sep_norm(text)
    for phrase in phrases:
        parts = [re.escape(p) for p in re.split(r"[-_\s/]+", norm(str(phrase or ""))) if p]
        if not parts:
            continue
        needle = r"[-_\s/]+".join(parts)
        if re.search(rf"(?<![a-z0-9_\-/]){needle}(?![a-z0-9_\-/])", hay):
            return True
    return False


STOP = {
    "a", "an", "and", "or", "the", "va", "voi", "hay", "hoac",
    "trang", "page", "pages", "url", "path", "landing", "campaign", "chien", "dich",
    "on", "khong", "ko", "hong", "tot", "xau", "the", "nao", "ra", "sao",
    "hieu", "suat", "hieu qua", "google", "ads", "ad", "paid", "performance",
    "phan", "tich", "bao", "cao", "xem", "ket", "qua", "score", "diem",
    "ngay", "tuan", "thang", "gan", "nhat", "vua", "roi", "nay",
    "nen", "lam", "gi", "can", "uu", "tien", "fix", "sua",
    "search", "gg", "gdn", "pmax", "display", "video",
}


@dataclass
class Entity:
    id: str
    label: str
    scope: str
    type: str
    aliases: set[str] = field(default_factory=set)
    patterns: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    confidence: float = 0.7
    last_seen_at: float = field(default_factory=time.time)

    def merge(self, other: "Entity") -> None:
        self.aliases.update(other.aliases)
        self.patterns.update(other.patterns)
        self.sources.update(other.sources)
        self.confidence = max(self.confidence, other.confidence)
        self.last_seen_at = max(self.last_seen_at, other.last_seen_at)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "scope": self.scope,
            "type": self.type,
            "aliases": sorted(self.aliases),
            "patterns": sorted(self.patterns),
            "sources": sorted(self.sources),
            "confidence": round(self.confidence, 2),
            "last_seen_at": int(self.last_seen_at),
        }


_campaign_entities: dict[str, Entity] = {}


def _entity_id(scope: str, label: str) -> str:
    base = compact(label) or "entity"
    return f"{scope}:{base[:48]}"


def _slug_aliases(text: str) -> set[str]:
    n = norm(text)
    if not n:
        return set()
    aliases = {n}
    c = compact(n)
    if c and c != n.replace(" ", ""):
        aliases.add(c)
    words = [w for w in n.split() if w not in STOP and not w.isdigit()]
    if len(words) >= 2:
        aliases.add(" ".join(words))
        aliases.add("".join(words))
    for w in words:
        if len(w) >= 3 and (len(words) == 1 or words[0] == "vdb"):
            aliases.add(w)
    return {a for a in aliases if len(a) >= 2 and a not in STOP}


def _add_entity(catalog: dict[str, Entity], ent: Entity) -> None:
    if ent.id in catalog:
        catalog[ent.id].merge(ent)
    else:
        catalog[ent.id] = ent


def _path_from_url(url: str) -> str:
    parsed = urlparse(url if re.match(r"^https?://", url or "", re.I) else "https://x/" + (url or "").lstrip("/"))
    return parsed.path.strip("/")


def _url_entities(urls: list[str]) -> dict[str, Entity]:
    catalog: dict[str, Entity] = {}
    product_slugs: list[str] = []
    for url in urls or []:
        path = _path_from_url(str(url))
        if not path:
            continue
        parts = [p for p in path.split("/") if p]
        first = parts[0]
        first_norm = norm(first)
        if first_norm:
            aliases = _slug_aliases(first) | ({
                "san pham", "product", "trang san pham",
            } if first_norm == "product" else set())
            if first_norm == "tutorial":
                aliases |= {"huong dan", "bai huong dan"}
            ent = Entity(
                id=_entity_id("url", first_norm),
                label=first_norm,
                scope="url",
                type="url_group",
                aliases=aliases,
                patterns={first.lower()},
                sources={"url_inventory"},
                confidence=0.9,
            )
            _add_entity(catalog, ent)
        slug = parts[-1]
        if len(parts) >= 2:
            if first_norm == "product":
                product_slugs.append(slug)
            label = norm(slug)
            aliases = _slug_aliases(slug)
            if slug.lower().startswith("vdb-"):
                aliases.discard("vdb")
            if aliases:
                _add_entity(catalog, Entity(
                    id=_entity_id("url", label),
                    label=label,
                    scope="url",
                    type="url",
                    aliases=aliases,
                    patterns={path.lower()},
                    sources={"url_inventory"},
                    confidence=0.88,
                ))

    if any(s.lower().startswith("vdb-") for s in product_slugs):
        _add_entity(catalog, Entity(
            id="url:vdb",
            label="vdb",
            scope="url",
            type="product_family",
            aliases={"vdb", "virtual database", "database"},
            patterns={"product/vdb-"},
            sources={"url_inventory"},
            confidence=0.95,
        ))
    return catalog


def _campaign_name_entities(names: list[str]) -> dict[str, Entity]:
    catalog: dict[str, Entity] = {}
    competitor_terms = {"fpt", "viettel", "cmc", "aws", "azure", "gcp"}
    for name in names or []:
        raw = str(name or "").strip()
        if not raw:
            continue
        n = norm(raw)
        aliases = _slug_aliases(raw) | {n}
        _add_entity(catalog, Entity(
            id=_entity_id("campaign", n),
            label=raw,
            scope="campaign",
            type="campaign",
            aliases=aliases,
            patterns={n},
            sources={"campaign_inventory"},
            confidence=0.9,
        ))
        words = [w for w in n.split() if w not in STOP and not w.isdigit() and len(w) > 1]
        if any(w in {"greennode", "green", "node", "vng", "vngcloud"} for w in words):
            _add_entity(catalog, Entity(
                id="campaign:brand",
                label="brand campaign",
                scope="campaign",
                type="campaign_group",
                aliases={"brand", "brand campaign", "greennode", "green node", "vng cloud", "vngcloud"},
                patterns={"greennode", "green node", "vng", "vngcloud"},
                sources={"campaign_inventory"},
                confidence=0.9,
            ))
        comp = sorted(w for w in words if w in competitor_terms)
        if comp:
            _add_entity(catalog, Entity(
                id="campaign:competitor",
                label="competitor campaign",
                scope="campaign",
                type="campaign_group",
                aliases={"competitor", "doi thu", "đối thủ", *comp},
                patterns=set(comp),
                sources={"campaign_inventory"},
                confidence=0.88,
            ))
        # Useful topic entities from campaign names, e.g. CloudServer -> cloud server.
        for i in range(len(words) - 1):
            phrase = " ".join(words[i:i + 2])
            if phrase not in STOP and len(phrase) >= 5:
                _add_entity(catalog, Entity(
                    id=_entity_id("campaign", phrase),
                    label=phrase,
                    scope="campaign",
                    type="campaign_topic",
                    aliases={phrase, phrase.replace(" ", "")},
                    patterns={phrase, phrase.replace(" ", "")},
                    sources={"campaign_inventory"},
                    confidence=0.72,
                ))
    return catalog


def register_campaigns(campaigns: list[dict] | list[str]) -> None:
    names: list[str] = []
    for item in campaigns or []:
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("campaign") or ""))
        else:
            names.append(str(item or ""))
    for key, ent in _campaign_name_entities(names).items():
        if key in _campaign_entities:
            _campaign_entities[key].merge(ent)
        else:
            _campaign_entities[key] = ent


def catalog(urls: list[str] | None = None) -> list[dict]:
    if urls is None:
        try:
            import runtime_config

            urls = runtime_config.current().get("urls") or []
        except Exception:  # noqa: BLE001
            urls = []
    merged = _url_entities(urls)
    for key, ent in _campaign_entities.items():
        if key in merged:
            merged[key].merge(ent)
        else:
            merged[key] = ent
    return [e.as_dict() for e in sorted(merged.values(), key=lambda x: (x.scope, x.type, x.label))]


def _split_include_exclude(message: str) -> tuple[str, str]:
    n = sep_norm(message)
    parts = re.split(r"\b(?:tru|ngoai tru|loai tru|khong gom|exclude|except)\b", n, maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _strip_negated_references(text: str) -> str:
    parts = re.split(r"\b(?:khong phai|not)\b", sep_norm(text), maxsplit=1)
    return parts[0]


def _path_matches(text: str, entities: list[dict], scope: str) -> list[dict]:
    if scope != "url" or "/" not in text:
        return []
    hay = sep_norm(text)
    matches: list[dict] = []
    seen: set[str] = set()
    for ent in entities:
        if ent.get("scope") != "url":
            continue
        for raw in ent.get("patterns") or []:
            path = _path_from_url(str(raw or "")).lower() if "://" in str(raw or "") else str(raw or "").lower().strip("/")
            if not path:
                continue
            if re.search(rf"(?<![a-z0-9_\-/])/?{re.escape(path)}(?![a-z0-9_\-/])", hay):
                if ent["id"] not in seen:
                    seen.add(ent["id"])
                    matches.append(ent)
                break
    return matches


def _alias_surfaces(ent: dict) -> list[str]:
    surfaces: list[str] = []
    seen: set[str] = set()
    for raw in [ent.get("label"), *(ent.get("aliases") or []), *(ent.get("patterns") or [])]:
        val = str(raw or "").strip()
        if not val:
            continue
        for candidate in (norm(val), compact(val) if re.search(r"[a-z][A-Z]|[0-9][A-Z]|[_-]", val) else ""):
            if candidate and candidate not in seen and candidate not in STOP and len(candidate) >= 2:
                seen.add(candidate)
                surfaces.append(candidate)
    return surfaces


def _surface_pos(text: str, surface: str) -> int | None:
    hay_sep = sep_norm(text)
    hay_compact = compact(text)
    if "/" in surface:
        idx = hay_sep.find(surface.strip("/").lower())
        return idx if idx >= 0 else None
    if " " in surface:
        parts = [re.escape(p) for p in surface.split() if p]
        if parts:
            m = re.search(rf"(?<![a-z0-9_\-/]){r'[-_\s/]+'.join(parts)}(?![a-z0-9_\-/])", hay_sep)
            if m:
                return m.start()
        compact_surface = re.sub(r"[^a-z0-9]+", "", surface)
        if len(compact_surface) >= 8:
            idx = hay_compact.find(compact_surface)
            if idx >= 0:
                return idx
    else:
        m = re.search(rf"(?<![a-z0-9_\-/]){re.escape(surface)}(?![a-z0-9_\-/])", hay_sep)
        if m:
            return m.start()
        if len(surface) >= 8:
            idx = hay_compact.find(surface)
            if idx >= 0:
                return idx
    return None


def _entity_pos(text: str, ent: dict) -> int:
    positions: list[int] = []
    for raw in ent.get("patterns") or []:
        path = _path_from_url(str(raw or "")).lower() if "://" in str(raw or "") else str(raw or "").lower().strip("/")
        if path:
            pos = _surface_pos(text, path)
            if pos is not None:
                positions.append(pos)
    for surface in _alias_surfaces(ent):
        pos = _surface_pos(text, surface)
        if pos is not None:
            positions.append(pos)
    return min(positions) if positions else 10**9


def _should_order_by_text(text: str) -> bool:
    return bool(
        re.search(r"https?://|(?<![a-z0-9])/?[\w-]+/[\w\-/]+", text or "", re.I)
        or re.search(r"[a-z][A-Z]|[0-9][A-Z]", text or "")
        or re.search(r"[,;]", text or "")
        or _has_phrase(text, ("phai chua ca", "chua ca", "match all", "tat ca keyword"))
    )


def _match_entities(text: str, entities: list[dict], scope: str) -> list[dict]:
    path_hits = _path_matches(text, entities, scope)
    matches: list[dict] = list(path_hits)
    seen: set[str] = {ent["id"] for ent in path_hits}
    hay_compact = compact(text)
    hay_sep = sep_norm(text)
    for ent in entities:
        if ent.get("scope") != scope:
            continue
        aliases = _alias_surfaces(ent)
        aliases = [a for a in aliases if a and a not in STOP and len(a) >= 2]
        hit = False
        for alias in sorted(aliases, key=len, reverse=True):
            if " " in alias:
                parts = [re.escape(p) for p in alias.split() if p]
                needle = r"[-_\s/]+".join(parts)
                if re.search(rf"(?<![a-z0-9_\-/]){needle}(?![a-z0-9_\-/])", hay_sep):
                    hit = True
                    break
            else:
                if (
                    re.search(rf"(?<![a-z0-9_\-/]){re.escape(alias)}(?![a-z0-9_\-/])", hay_sep)
                    or (len(alias) >= 6 and re.search(rf"(?:^|_){re.escape(alias)}(?:_|$)", hay_sep))
                    or alias == hay_compact
                ):
                    hit = True
                    break
        if hit and ent["id"] not in seen:
            seen.add(ent["id"])
            matches.append(ent)
    if _should_order_by_text(text):
        matches.sort(key=lambda ent: (_entity_pos(text, ent), -(len(str(ent.get("label") or "")))))
    return _prefer_specific(matches, text)


def _prefer_specific(matches: list[dict], text: str = "") -> list[dict]:
    drop: set[str] = set()
    hay = sep_norm(text)
    for broad in matches:
        bpats = set(broad.get("patterns") or [])
        blabel = norm(str(broad.get("label") or ""))
        for other in matches:
            if other is broad:
                continue
            olabel = norm(str(other.get("label") or ""))
            opats = set(other.get("patterns") or [])
            if broad.get("scope") == "campaign" and broad.get("type") == "campaign_group" and other.get("type") == "campaign":
                group_words = {
                    w for w in norm(str(broad.get("label") or "")).split()
                    if w and w not in STOP and w != "campaign"
                }
                other_terms = {
                    norm(str(x or ""))
                    for x in [other.get("label"), *(other.get("aliases") or []), *(other.get("patterns") or [])]
                    if str(x or "").strip()
                }
                for term in other_terms:
                    parts = [re.escape(p) for p in term.split() if p]
                    if len(parts) < 2:
                        continue
                    needle = r"[-_\s/]+".join(parts)
                    if re.search(rf"(?<![a-z0-9_\-/]){needle}(?![a-z0-9_\-/])", hay):
                        words = term.split()
                        if any(g in words[1:] for g in group_words):
                            continue
                        drop.add(broad["id"])
                        break
            if broad.get("type") in {"product_family", "url_group"}:
                if blabel and olabel.startswith(blabel + " "):
                    drop.add(broad["id"])
                elif any(str(op).startswith(str(bp).rstrip("-/")) and str(op) != str(bp) for bp in bpats for op in opats):
                    drop.add(broad["id"])
                elif broad.get("type") == "url_group" and any(
                    str(op).startswith(str(bp).strip("/") + "/") for bp in bpats for op in opats
                ):
                    drop.add(broad["id"])
            if broad.get("scope") == "url" and other.get("scope") == "url":
                broad_terms = {
                    norm(str(x or ""))
                    for x in [broad.get("label"), *(broad.get("aliases") or [])]
                    if str(x or "").strip()
                }
                other_terms = {
                    norm(str(x or ""))
                    for x in [other.get("label"), *(other.get("aliases") or [])]
                    if str(x or "").strip()
                }
                for bt in broad_terms:
                    if not bt or bt in STOP:
                        continue
                    for ot in other_terms:
                        if bt == ot or len(bt.split()) >= len(ot.split()):
                            continue
                        if re.search(rf"(?<![a-z0-9]){re.escape(bt)}(?![a-z0-9])", ot):
                            drop.add(broad["id"])
    return [m for m in matches if m.get("id") not in drop]


def resolve(message: str, default_scope: str = "url", urls: list[str] | None = None) -> dict:
    url_cue = _has_phrase(message, ("url", "trang", "page", "path", "landing", "ldp", "seo"))
    campaign_cue = _has_phrase(message, ("campaign", "chien dich"))
    scope = "campaign" if default_scope == "campaign" and campaign_cue and not url_cue else default_scope
    match_mode = "all" if _has_phrase(message, ("phai chua ca", "chua ca", "match all", "tat ca keyword")) else "any"
    inc_text, exc_text = _split_include_exclude(message)
    inc_text = _strip_negated_references(inc_text)
    entities = catalog(urls)
    inc = _prefer_specific(_match_entities(inc_text, entities, scope))
    exc = _prefer_specific(_match_entities(exc_text, entities, scope)) if exc_text else []

    def patterns(ms: list[dict]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for ent in ms:
            for p in ent.get("patterns") or []:
                raw = str(p or "")
                if ent.get("scope") == "url":
                    pp = _path_from_url(raw).lower() if "://" in raw else raw.lower().strip("/")
                else:
                    pp = re.sub(r"\s+", "-", norm(raw))
                if pp and pp not in seen:
                    seen.add(pp)
                    out.append(pp)
        return out

    return {
        "scope": scope,
        "include": patterns(inc),
        "exclude": patterns(exc),
        "match_mode": match_mode,
        "entities": [m["id"] for m in inc],
        "entity_labels": [m["label"] for m in inc],
        "exclude_entities": [m["id"] for m in exc],
    }
