"""Paid Campaigns module của DeCho Agent — Google Ads API (read-only monitor).

Chỉ ĐỌC dữ liệu campaign/metrics để theo dõi & phân tích — không tạo/sửa campaign.
Credentials qua env (GOOGLE_ADS_*) — TUYỆT ĐỐI không hardcode vào code.
"""

import logging
import os

log = logging.getLogger("ads_agent")

DEV_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN", "")
LOGIN_CUSTOMER_ID = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")  # MCC
CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "") or LOGIN_CUSTOMER_ID


def configured() -> bool:
    return all([DEV_TOKEN, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, LOGIN_CUSTOMER_ID, CUSTOMER_ID])


_svc_cache = None
_svc_lock = None


def _service():
    """Client Google Ads được cache module-level — init 1 lần (đổi token + dựng
    gRPC channel mất ~1-2s), các call sau dùng lại nên nhanh hơn hẳn."""
    global _svc_cache, _svc_lock
    if _svc_cache is not None:
        return _svc_cache
    if not configured():
        raise RuntimeError("Thiếu env Google Ads (GOOGLE_ADS_*) — xem .env.example.")
    import threading

    if _svc_lock is None:
        _svc_lock = threading.Lock()
    with _svc_lock:
        if _svc_cache is None:
            from google.ads.googleads.client import GoogleAdsClient

            client = GoogleAdsClient.load_from_dict({
                "developer_token": DEV_TOKEN,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN,
                "login_customer_id": LOGIN_CUSTOMER_ID,
                "use_proto_plus": True,
            })
            _svc_cache = client.get_service("GoogleAdsService")
            log.info("Google Ads client khởi tạo xong (cached)")
    return _svc_cache


def warmup():
    """Khởi tạo client trước (gọi nền lúc server start) — người dùng đầu tiên khỏi chờ."""
    try:
        if configured():
            _service()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Ads warmup lỗi (bỏ qua): {e}")


def _reset():
    """Bỏ client đang cache — gRPC channel idle lâu có thể bị server ngắt."""
    global _svc_cache
    _svc_cache = None


_TRANSIENT = ("unavailable", "timed out", "timeout", "stream removed",
              "deadline", "goaway", "connection reset", "socket closed")


def _search(query: str) -> list:
    """svc.search với retry: lỗi kết nối transient → làm mới channel, thử lại 1 lần."""
    for attempt in (1, 2):
        try:
            svc = _service()
            return list(svc.search(customer_id=CUSTOMER_ID, query=query))
        except Exception as e:  # noqa: BLE001
            if attempt == 1 and any(k in str(e).lower() for k in _TRANSIENT):
                log.warning(f"Ads kết nối hỏng ({type(e).__name__}) — làm mới client, thử lại...")
                _reset()
                continue
            raise
    return []  # không tới được


def list_campaigns() -> list[dict]:
    q = ("SELECT campaign.id, campaign.name, campaign.status, "
         "campaign.advertising_channel_type FROM campaign ORDER BY campaign.name")
    out = []
    for r in _search(q):
        c = r.campaign
        out.append({"id": c.id, "name": c.name, "status": c.status.name,
                    "channel": c.advertising_channel_type.name})
    log.info(f"Ads: {len(out)} campaign")
    return out


def campaign_perf(days: int = 7, start: str | None = None, end: str | None = None) -> dict:
    """Metrics theo từng campaign × từng ngày.

    Mặc định N ngày gần nhất (trừ hôm nay); truyền start/end (YYYY-MM-DD) để lọc
    khoảng bất kỳ — validate bằng strptime (chặn injection), tối đa 366 ngày.
    """
    from datetime import date, datetime, timedelta

    if start and end:
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        d1 = datetime.strptime(end, "%Y-%m-%d").date()
        if d0 > d1:
            d0, d1 = d1, d0
        if d1 > date.today():
            d1 = date.today()
        if (d1 - d0).days + 1 > 366:
            raise RuntimeError(f"Khoảng quá dài ({(d1 - d0).days + 1} ngày) — tối đa 366 ngày.")
        start, end = d0.isoformat(), d1.isoformat()
    else:
        days = max(1, min(int(days or 7), 90))
        start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        end = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    q = f"""
        SELECT campaign.id, campaign.name, campaign.status, segments.date,
               metrics.impressions, metrics.clicks, metrics.ctr,
               metrics.cost_micros, metrics.conversions, metrics.average_cpc,
               metrics.absolute_top_impression_percentage, metrics.top_impression_percentage
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY segments.date
    """
    rows = []
    for r in _search(q):
        m = r.metrics
        cost = m.cost_micros / 1_000_000
        rows.append({
            "id": r.campaign.id, "name": r.campaign.name, "status": r.campaign.status.name,
            "date": r.segments.date,
            "impressions": int(m.impressions), "clicks": int(m.clicks),
            "ctr": round(m.ctr * 100, 2), "cost": round(cost, 2),
            "conversions": round(m.conversions, 1),
            "cpa": round(cost / m.conversions) if m.conversions else None,
            "absTopPct": round(m.absolute_top_impression_percentage * 100, 2),
            "topPct": round(m.top_impression_percentage * 100, 2),
        })
    log.info(f"Ads perf {start}→{end}: {len(rows)} dòng")
    return {"start": start, "end": end, "rows": rows}


def landing_page_perf(days: int = 7, start: str | None = None, end: str | None = None) -> dict:
    """Hiệu suất theo từng LANDING PAGE (read-only): metrics Ads + tốc độ/bounce + link Clarity.

    Account-wide (mọi campaign). Ngày: mặc định N ngày gần nhất, hoặc start/end (YYYY-MM-DD).
    """
    from datetime import date, datetime, timedelta

    if start and end:
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        d1 = datetime.strptime(end, "%Y-%m-%d").date()
        if d0 > d1:
            d0, d1 = d1, d0
        if d1 > date.today():
            d1 = date.today()
        if (d1 - d0).days + 1 > 366:
            raise RuntimeError(f"Khoảng quá dài ({(d1 - d0).days + 1} ngày) — tối đa 366 ngày.")
        start, end = d0.isoformat(), d1.isoformat()
    else:
        days = max(1, min(int(days or 7), 90))
        start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        end = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    q = f"""
        SELECT landing_page_view.unexpanded_final_url,
               metrics.impressions, metrics.clicks, metrics.ctr,
               metrics.average_cpc, metrics.cost_micros, metrics.conversions,
               metrics.bounce_rate, metrics.mobile_friendly_clicks_percentage, metrics.speed_score
        FROM landing_page_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY metrics.clicks DESC
    """
    rows = []
    for r in _search(q):
        m = r.metrics
        url = r.landing_page_view.unexpanded_final_url
        rows.append({
            "url": url, "base_url": url.split("?")[0],
            "impressions": int(m.impressions), "clicks": int(m.clicks),
            "ctr": round(m.ctr * 100, 2),
            "avg_cpc": round(m.average_cpc / 1_000_000),
            "cost": round(m.cost_micros / 1_000_000),
            "conversions": round(m.conversions, 1),
            "bounce_rate": round(m.bounce_rate * 100, 1) if m.bounce_rate else None,
            "mobile_friendly_pct": round(m.mobile_friendly_clicks_percentage, 1) if m.mobile_friendly_clicks_percentage else None,
            "speed_score": int(m.speed_score) if m.speed_score else None,
        })
    out = {"start": start, "end": end, "rows": rows}
    try:  # kèm link Clarity nếu đã cấu hình (không bắt buộc)
        import clarity_agent
        if clarity_agent.configured():
            out["clarity_heatmap"] = clarity_agent.heatmap_url()
            out["clarity_recordings"] = clarity_agent.recordings_url()
    except Exception:  # noqa: BLE001
        pass
    log.info(f"Ads LDP {start}→{end}: {len(rows)} landing page")
    return out


# ── GHI: tạo campaign (luôn PAUSED — không tiêu tiền tới khi bật tay) ──────────

_client_cache = None


def _client():
    """GoogleAdsClient (cache) cho thao tác GHI — khác _service() (chỉ GoogleAdsService)."""
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    if not configured():
        raise RuntimeError("Thiếu env Google Ads (GOOGLE_ADS_*).")
    from google.ads.googleads.client import GoogleAdsClient

    _client_cache = GoogleAdsClient.load_from_dict({
        "developer_token": DEV_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN, "login_customer_id": LOGIN_CUSTOMER_ID, "use_proto_plus": True,
    })
    return _client_cache


_DEFAULT_HEADLINES = [
    "Triển Khai Server Trong 5 Phút", "Chi phí thuê Cloud Server", "Cloud server Hiệu Suất Cao",
    "Cloud Server Hệ Windows", "Cloud server Việt Nam", "Thuê Cloud server theo nhu cầu",
    "Server cloud cho doanh nghiệp", "Cloud Server Cho AI & Big Data", "Không Lo Gián Đoạn Server",
    "Cloud server Uptime 99.99%", "Cloud Server – Bảo Mật Cao", "Đạt Chứng Chỉ ISO 27001",
    "Di Chuyển Cloud Miễn Phí", "Giảm 30% Chi Phí Cloud Server", "1000+ Doanh Nghiệp Tin Dùng",
]
_DEFAULT_DESCRIPTIONS = [
    "Mở rộng tài nguyên chỉ bằng 1 click. Hỗ trợ kỹ thuật 24/7, tư vấn triển khai miễn phí.",
    "Cloud Computing toàn diện, hiệu suất cao, đạt chuẩn ISO 27001 và bảo mật hệ thống.",
    "Miễn phí chuyển đổi máy chủ lên Cloud. Đội ngũ chuyên gia hỗ trợ 24/7. Đăng ký tư vấn ngay!",
    "SLA 99.99%. Kiến Trúc Multi-Region. Tăng tốc chuyển đổi với hạ tầng đạt chuẩn Tier III.",
]
_DEFAULT_SITELINKS = [
    {"text": "Dịch vụ Cloud Server", "url": "https://greennode.ai/product/cpu-instances", "desc1": "Hiệu suất cao, bảo mật ISO 27001", "desc2": "Khởi tạo nhanh, hỗ trợ 24/7"},
    {"text": "Đăng ký dùng thử ngay", "url": "https://register.vngcloud.vn/signup", "desc1": "1 click đăng ký, dùng thử miễn phí", "desc2": "Không cần thẻ tín dụng"},
    {"text": "Tư vấn miễn phí ngay", "url": "https://greennode.ai/contact", "desc1": "Chuyên gia tư vấn giải pháp Cloud", "desc2": "Hỗ trợ triển khai 24/7"},
    {"text": "GreenNode Cloud", "url": "https://greennode.ai/", "desc1": "1000+ doanh nghiệp tin dùng", "desc2": "Cloud server Việt Nam chuẩn pháp lý"},
]


def create_campaign(name: str, budget_vnd: int = 100_000, final_url: str | None = None,
                    headlines: list | None = None, descriptions: list | None = None,
                    sitelinks: list | None = None, cpc_bid_vnd: int = 2_000) -> dict:
    """Tạo Search campaign Ở TRẠNG THÁI PAUSED: budget + campaign + geo VN + ngôn ngữ VI
    + ad group + RSA ad + sitelinks. PAUSED nên KHÔNG tiêu tiền tới khi người dùng tự bật.
    """
    import uuid

    client = _client()
    cust = CUSTOMER_ID
    name = (name or f"DeCho_Campaign_{uuid.uuid4().hex[:6]}").strip()[:120]
    final_url = (final_url or "https://greennode.ai/product/cpu-instances").strip()
    headlines = headlines or _DEFAULT_HEADLINES
    descriptions = descriptions or _DEFAULT_DESCRIPTIONS
    sitelinks = _DEFAULT_SITELINKS if sitelinks is None else sitelinks

    # 1) Budget
    bsvc = client.get_service("CampaignBudgetService")
    bop = client.get_type("CampaignBudgetOperation")
    b = bop.create
    b.name = f"Budget_{uuid.uuid4().hex[:8]}"
    b.amount_micros = int(budget_vnd) * 1_000_000
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    bres = bsvc.mutate_campaign_budgets(customer_id=cust, operations=[bop]).results[0].resource_name

    # 2) Campaign — PAUSED
    csvc = client.get_service("CampaignService")
    cop = client.get_type("CampaignOperation")
    c = cop.create
    c.name = name
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    c.status = client.enums.CampaignStatusEnum.PAUSED
    c.campaign_budget = bres
    c.manual_cpc.enhanced_cpc_enabled = False
    c.network_settings.target_google_search = True
    c.network_settings.target_search_network = True
    c.network_settings.target_content_network = False
    cres = csvc.mutate_campaigns(customer_id=cust, operations=[cop]).results[0].resource_name

    # 3) Geo Vietnam + 4) Language Vietnamese
    crit = client.get_service("CampaignCriterionService")
    gop = client.get_type("CampaignCriterionOperation")
    gop.create.campaign = cres
    gop.create.location.geo_target_constant = client.get_service("GeoTargetConstantService").geo_target_constant_path("2704")
    lop = client.get_type("CampaignCriterionOperation")
    lop.create.campaign = cres
    lop.create.language.language_constant = "languageConstants/1000"
    crit.mutate_campaign_criteria(customer_id=cust, operations=[gop, lop])

    # 5) Ad group
    agsvc = client.get_service("AdGroupService")
    agop = client.get_type("AdGroupOperation")
    ag = agop.create
    ag.name = f"{name} - group 1"
    ag.campaign = cres
    ag.status = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ag.cpc_bid_micros = int(cpc_bid_vnd) * 1_000_000
    agres = agsvc.mutate_ad_groups(customer_id=cust, operations=[agop]).results[0].resource_name

    # 6) RSA ad
    def _asset(text):
        a = client.get_type("AdTextAsset")
        a.text = text
        return a

    adsvc = client.get_service("AdGroupAdService")
    aop = client.get_type("AdGroupAdOperation")
    aga = aop.create
    aga.ad_group = agres
    aga.status = client.enums.AdGroupAdStatusEnum.ENABLED
    aga.ad.final_urls.append(final_url)
    rsa = aga.ad.responsive_search_ad
    rsa.headlines.extend([_asset(h[:30]) for h in headlines[:15]])
    rsa.descriptions.extend([_asset(d[:90]) for d in descriptions[:4]])
    adsvc.mutate_ad_group_ads(customer_id=cust, operations=[aop])

    # 7) Sitelinks
    asvc = client.get_service("AssetService")
    casvc = client.get_service("CampaignAssetService")
    n_sl = 0
    for sl in (sitelinks or [])[:8]:
        asop = client.get_type("AssetOperation")
        a = asop.create
        a.name = f"Sitelink_{sl['text'][:20]}_{uuid.uuid4().hex[:6]}"
        a.sitelink_asset.link_text = sl["text"]
        a.sitelink_asset.final_urls.append(sl["url"])
        if sl.get("desc1"):
            a.sitelink_asset.description1 = sl["desc1"]
        if sl.get("desc2"):
            a.sitelink_asset.description2 = sl["desc2"]
        ares = asvc.mutate_assets(customer_id=cust, operations=[asop]).results[0].resource_name
        caop = client.get_type("CampaignAssetOperation")
        ca = caop.create
        ca.campaign = cres
        ca.asset = ares
        ca.field_type = client.enums.AssetFieldTypeEnum.SITELINK
        casvc.mutate_campaign_assets(customer_id=cust, operations=[caop])
        n_sl += 1

    log.info(f"Ads create_campaign: {cres} (PAUSED, budget {budget_vnd} VND, {n_sl} sitelinks)")
    return {"ok": True, "campaign": cres, "name": name, "status": "PAUSED",
            "budget_vnd": int(budget_vnd), "headlines": len(headlines[:15]),
            "descriptions": len(descriptions[:4]), "sitelinks": n_sl}
