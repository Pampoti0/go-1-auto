# 🐻 DeCho Agent — AI Marketing Operations

**Track: Agentic Assistant — GreenNode Claw-a-thon 2026**

> "Đệ là DeCho — biết làm thơ, bắt trend, đo web nhanh hơn Đại ca F5."

## Problem
Team marketing phải nhảy qua 3-4 tool (PageSpeed Insights, Search Console, GA4, Sheets) để trả lời những câu hỏi cơ bản: web có chậm không, traffic tăng hay giảm, trang nào đang tụt — mỗi tháng tốn hàng giờ thao tác tay và dễ bỏ sót bất thường.

## User
Marketer, content creator, growth team quản lý website nhiều trang (case thật: greennode.ai với 19 URL).

## Solution
**DeCho** — agent all-in-one có nhân vật riêng, chat tiếng Việt tự nhiên:

- **PageSpeed Intelligence**: tự quét Core Web Vitals theo lịch (3 luồng song song, retry), ghi Google Sheet tô màu theo ngưỡng; chat "chạy kiểm tra ngay" thấy tiến trình từng URL real-time
- **SEO Performance Tracking**: kéo GSC + GA4 hàng tháng, so sánh tháng trước, chạy backfill nhiều tháng qua chat
- **Hỏi đáp trên dữ liệu thật**: "trang nào LCP tệ nhất?", "traffic 5 tháng xu hướng sao?" — DeCho đọc Sheet, phân tích bằng LLM (GreenNode MaaS), trả lời kèm số liệu, stream từng đoạn
- **Tổng quan & Alerts**: dashboard KPI/trend, tự phát hiện bất thường (score tụt ≥10 điểm, clicks giảm ≥20%) và trỏ tới nơi xử lý
- **URL Intelligence**: hợp nhất traffic + PageSpeed theo từng URL, drill-down chi tiết
- **DeCho mascot**: nhân vật 3D/2D phản ứng theo trạng thái hệ thống (nghĩ/vui/buồn), hỏi nhanh theo bối cảnh màn hình đang xem; tính cách định nghĩa bằng SOUL.md + AGENT.md (hot-reload, sửa là áp dụng ngay)

## Value
Một nơi duy nhất thay cho 4 tool; cảnh báo chủ động thay vì phát hiện muộn; thao tác bằng ngôn ngữ tự nhiên — không cần biết kỹ thuật.

## Kiến trúc

```
┌─ UI: static/index.html (React + Tailwind qua CDN, no build) ─┐
│ Tổng quan · Chat với DeCho · URL Intelligence · PageSpeed    │
│ Dashboard · Alerts · Cấu hình  +  DeCho dock (3D/sprite/pose)│
└──────────────────────────┬───────────────────────────────────┘
                           │ REST + SSE
┌─ FastAPI (server.py) ────┴───────────────────────────────────┐
│ Intent router all-in-one (LLM intent + keyword fallback)     │
│ · psi_checker.py  — PSI API, 3 workers, retry, ghi Sheet     │
│ · seo_agent.py    — GSC + GA4 → Sheet, so sánh MoM           │
│ · sheet_store.py  — persist config/log + đọc kết quả         │
│ · runtime_config  — config động, đồng bộ Sheet (_config),    │
│                     sống qua container recreate              │
│ Scheduler nền: PSI (daily/weekly/monthly) + SEO (monthly)    │
│ Cache đọc Sheet (chống quota 60 reads/min)                   │
└──────────────────────────┬───────────────────────────────────┘
              GreenNode MaaS (Gemma/Qwen/MiniMax)
              Google Sheets / PSI API / GSC / GA4
```

## Chạy local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # điền PSI_API_KEY, SHEET_ID, MAAS_*, SEO_*
# credentials: service_account.json (PSI Sheet) + token.json (GSC/GA4/SEO Sheet)
python server.py            # http://localhost:8000
```

## Deploy (GreenNode AgentBase)

```bash
docker build --platform linux/amd64 -t go-1-auto .
# hoặc dùng bộ skill AgentBase trong Claude Code: /agentbase-deploy
```
Secrets inject qua env lúc runtime (không bake vào image / không commit): `SERVICE_ACCOUNT_JSON`, `SEO_TOKEN_JSON` — dán nguyên nội dung file JSON vào env var.

## API chính

| Endpoint | Mô tả |
|---|---|
| `GET /healthz` | Health check |
| `POST /api/agent/chat/stream` | Chat all-in-one (SSE: action steps + delta + kết quả real-time) |
| `POST /api/decho/ask` | Hỏi nhanh DeCho theo bối cảnh màn hình đang xem |
| `GET /api/results` · `/api/seo/results` · `/api/seo/summary` | Dữ liệu dashboard (cache TTL) |
| `GET/PUT /api/config` | Config động PSI + SEO (persist qua Sheet) |
| `POST /api/check` · `POST /api/seo/run` | Trigger chạy trực tiếp |
| `GET /api/logs` · `GET /api/llm-test` | Vận hành & chẩn đoán |

## Nhân vật DeCho
UI tự chọn hình thức theo asset có trong `static/`: `poses/*.png` (pose 2D + CSS animation) → `sprites/*.png` (12-frame spritesheet) → `decho.glb` (3D three.js). Mọi tương tác đi qua `dechoBus` (busy/say/act) nên đổi hình thức không đổi hành vi.

## Data & Disclosure
- Chỉ dùng dữ liệu công khai (PSI của trang public) và analytics site công ty qua quyền OAuth được cấp; không PII, không dữ liệu khách hàng.
- UI và agent luôn khai báo người dùng đang tương tác với AI (Rulebook 11.1).

## Attribution
- Deploy bằng bộ skill [vngcloud/greennode-agentbase-skills](https://github.com/vngcloud/greennode-agentbase-skills)
- Design dashboard tham khảo [Cruip Mosaic](https://github.com/cruip/tailwind-dashboard-template) (style; chart SVG tự viết)
- Ngưỡng đánh giá theo Core Web Vitals guidelines của Google
