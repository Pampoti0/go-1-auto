# ⚡ PageSpeed Checker Agent

**Track: Automation & Integration — Claw-a-thon 2026**

## Problem
Team marketing/web phải kiểm tra hiệu suất (Core Web Vitals) của hàng chục trang web thủ công trên PageSpeed Insights, từng URL một, rồi tự tổng hợp — tốn hàng giờ mỗi tháng và dễ bỏ sót.

## User
Team quản lý website (marketing, SEO, web dev) cần theo dõi hiệu suất định kỳ nhiều URL.

## Solution
Agent tự động: theo lịch định kỳ (hoặc theo yêu cầu qua API), gọi Google PageSpeed Insights cho toàn bộ danh sách URL (mobile + desktop), trích xuất Performance Score, FCP, LCP, CLS, TBT, INP, TTFB, Speed Index, rồi ghi vào Google Sheet theo tab tháng `YYYY-MM` với màu xanh/vàng/đỏ theo ngưỡng Core Web Vitals của Google — không cần ai thao tác.

## Value
Tiết kiệm hàng giờ thao tác thủ công mỗi tháng; báo cáo nhất quán, có lịch sử theo tháng, nhìn màu là biết trang nào cần tối ưu.

## Tech
- Python + FastAPI (HTTP API) + APScheduler-style `schedule` (chạy nền trong container)
- Google PageSpeed Insights API + Google Sheets API (service account)
- Deploy: Docker → GreenNode AgentBase Runtime

## API

| Endpoint | Mô tả |
|---|---|
| `GET /` | Trang trạng thái + nút chạy ngay |
| `GET /healthz` | Health check |
| `POST /api/check` | Chạy kiểm tra toàn bộ URL ngay (chạy nền) |
| `GET /api/status` | Trạng thái lần chạy gần nhất |

## Chạy local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # điền PSI_API_KEY, SHEET_ID; đặt file service_account.json vào folder
python server.py       # mở http://localhost:8000
# hoặc chạy 1 lần không cần server: python psi_checker.py --once
```

## Deploy (Docker / AgentBase)

```bash
docker build -t pagespeed-checker-agent .
docker run -p 8000:8000 --env-file .env pagespeed-checker-agent
```

Trên AgentBase: dùng `SERVICE_ACCOUNT_JSON` (dán nguyên nội dung JSON vào env var) thay vì file.

## Setup credentials
Xem [SETUP.md](SETUP.md) — hướng dẫn lấy PSI API key, tạo service account, share Google Sheet.

## Data & Disclosure
- Chỉ kiểm tra URL public; dữ liệu ghi vào Sheet là số liệu hiệu suất công khai — không có PII hay dữ liệu nội bộ.
- Giao diện khai báo rõ người dùng đang tương tác với agent tự động (Rulebook 11.1).

## Attribution
Sử dụng bộ skill AgentBase (vngcloud/greennode-agentbase-skills) để deploy. Ngưỡng đánh giá theo Core Web Vitals guidelines của Google.
