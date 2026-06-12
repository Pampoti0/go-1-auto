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


def list_campaigns() -> list[dict]:
    svc = _service()
    q = ("SELECT campaign.id, campaign.name, campaign.status, "
         "campaign.advertising_channel_type FROM campaign ORDER BY campaign.name")
    out = []
    for r in svc.search(customer_id=CUSTOMER_ID, query=q):
        c = r.campaign
        out.append({"id": c.id, "name": c.name, "status": c.status.name,
                    "channel": c.advertising_channel_type.name})
    log.info(f"Ads: {len(out)} campaign")
    return out


def campaign_perf(days: int = 7) -> dict:
    """Metrics theo từng campaign × từng ngày trong N ngày gần nhất (trừ hôm nay)."""
    from datetime import date, timedelta

    svc = _service()
    days = max(1, min(int(days or 7), 90))
    start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    end = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    q = f"""
        SELECT campaign.id, campaign.name, campaign.status, segments.date,
               metrics.impressions, metrics.clicks, metrics.ctr,
               metrics.cost_micros, metrics.conversions, metrics.average_cpc
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY segments.date
    """
    rows = []
    for r in svc.search(customer_id=CUSTOMER_ID, query=q):
        m = r.metrics
        cost = m.cost_micros / 1_000_000
        rows.append({
            "id": r.campaign.id, "name": r.campaign.name, "status": r.campaign.status.name,
            "date": r.segments.date,
            "impressions": int(m.impressions), "clicks": int(m.clicks),
            "ctr": round(m.ctr * 100, 2), "cost": round(cost, 2),
            "conversions": round(m.conversions, 1),
            "cpa": round(cost / m.conversions) if m.conversions else None,
        })
    log.info(f"Ads perf {start}→{end}: {len(rows)} dòng")
    return {"start": start, "end": end, "rows": rows}
