# 🐻 DeCho Agent — AI Marketing Operations

**Track: Agentic Assistant — GreenNode Claw-a-thon 2026**

> "Đệ là DeCho — biết làm thơ, bắt trend, đo web nhanh hơn Đại ca F5."

## Problem
Team marketing phải nhảy qua nhiều tool (PageSpeed Insights, Search Console, GA4, Google Ads, Microsoft Clarity, Sheets) để trả lời những câu hỏi cơ bản: web có chậm không, traffic tăng hay giảm, trang nào đang tụt, campaign nào đang tốn tiền mà không convert — mỗi tháng tốn hàng giờ thao tác tay và dễ bỏ sót bất thường.

## User
Marketer, content creator, growth team quản lý website nhiều trang (case thật: greennode.ai với 19 URL).

## Solution
**DeCho** — agent all-in-one có nhân vật riêng, chat tiếng Việt tự nhiên:

- **PageSpeed Intelligence**: tự quét Core Web Vitals theo lịch (3 luồng song song, retry), ghi Google Sheet tô màu theo ngưỡng; chat "chạy kiểm tra ngay" thấy tiến trình từng URL real-time
- **SEO Performance Tracking**: kéo GSC + GA4 hàng tháng, so sánh tháng trước, chạy backfill nhiều tháng qua chat
- **Paid Campaigns + Clarity**: đọc Google Ads campaign/landing page, kết hợp Microsoft Clarity live insights để soi hành vi UX; campaign tạo mới chỉ qua form bảo mật và luôn ở trạng thái `PAUSED`
- **Hỏi đáp trên dữ liệu thật**: "trang nào LCP tệ nhất?", "traffic 5 tháng xu hướng sao?", "campaign nào CPA cao?" — DeCho đọc Sheet/API, phân tích bằng LLM (GreenNode MaaS), trả lời kèm số liệu/evidence, stream từng đoạn
- **Opportunity Score & Alerts**: gộp PSI + SEO + Ads + Clarity để xếp ưu tiên tối ưu; alert monitor phát hiện clicks drop, LCP xấu, spend tăng, CTR/CPA bất thường; mọi khuyến nghị có Evidence + Confidence
- **URL Intelligence**: hợp nhất traffic + PageSpeed theo từng URL, drill-down chi tiết
- **DeCho mascot + memory**: nhân vật 3D/2D phản ứng theo trạng thái hệ thống, hỏi nhanh theo bối cảnh màn hình; long-term memory chỉ lưu fact khi user chủ động "ghi nhớ" và có nút reset actor local

## Value
Một nơi duy nhất thay cho PageSpeed Insights, Search Console, GA4, Google Ads, Microsoft Clarity và Sheets; cảnh báo chủ động thay vì phát hiện muộn; thao tác bằng ngôn ngữ tự nhiên — không cần biết kỹ thuật.

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
│ · ads_agent.py    — Google Ads read-only + tạo campaign PAUSED│
│ · clarity_agent.py— Clarity live insights + RAM/disk cache    │
│ · sheet_store.py  — persist config/log + đọc kết quả         │
│ · memory_agent.py — AgentBase Memory history + fact xác nhận  │
│ · runtime_config  — config động, đồng bộ Sheet (_config),    │
│                     sống qua container recreate              │
│ Scheduler nền: PSI (daily/weekly/monthly) + SEO (monthly)    │
│ Cache đọc Sheet/API; Clarity cache RAM + disk chống quota     │
└──────────────────────────┬───────────────────────────────────┘
              GreenNode MaaS (Gemma/Qwen/MiniMax)
              Google Sheets / PSI API / GSC / GA4 / Ads / Clarity
```

## Chạy local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # điền PSI_API_KEY, SHEET_ID, MAAS_*, SEO_*
# credentials: service_account.json (PSI Sheet) + token.json (GSC/GA4/SEO Sheet)
python server.py            # http://localhost:8000
```

Nếu SEO báo `invalid_grant` / token expired-or-revoked, tạo lại OAuth token:

```bash
python generate_seo_token.py --token token.json
```

Nếu chưa có token cũ chứa `client_id/client_secret`, chạy thêm `--client-secrets client_secret.json`. `client_secret.json` là OAuth Desktop app trong Google Cloud Console. Cần enable Google Search Console API, Google Analytics Data API và Google Sheets API. Khi deploy, cập nhật lại secret `SEO_TOKEN_JSON` bằng nội dung `token.json` mới rồi restart runtime.

### Clarity cache khi dev

Microsoft Clarity Data Export có quota thấp, nên DeCho cache live insights theo 2 lớp:

- RAM cache trong process Python: nhanh, dùng khi server chưa restart.
- Disk cache `.cache/clarity_insights.json`: sống qua restart local/dev, mặc định TTL `86400` giây.

Env liên quan:

```bash
CLARITY_CACHE_TTL=86400
CLARITY_CACHE_FILE=.cache/clarity_insights.json
```

File `.cache/` đã được ignore, không commit dữ liệu API. Response Clarity trả thêm `cache: "api" | "memory" | "disk"` để kiểm tra nguồn cache.

## Deploy (GreenNode AgentBase)

```bash
docker build --platform linux/amd64 -t go-1-auto .
# hoặc dùng bộ skill AgentBase trong Claude Code: /agentbase-deploy
```
Secrets inject qua env lúc runtime (không bake vào image / không commit): `SERVICE_ACCOUNT_JSON`, `SEO_TOKEN_JSON`, `GOOGLE_ADS_*`, `CLARITY_*`, `MEMORY_ID` — dán nguyên nội dung file JSON vào env var khi cần. Với Clarity trên AgentBase, disk cache chỉ sống nếu runtime giữ được filesystem giữa các lần restart; nếu container recreate hoàn toàn thì request Clarity đầu tiên sẽ gọi API lại.

## API chính

| Endpoint | Mô tả |
|---|---|
| `GET /healthz` | Health check |
| `POST /api/agent/chat/stream` | Chat all-in-one (SSE: action steps + delta + kết quả real-time) |
| `POST /api/decho/ask` | Hỏi nhanh DeCho theo bối cảnh màn hình đang xem |
| `GET /api/results` · `/api/seo/results` · `/api/seo/summary` | Dữ liệu dashboard (cache TTL) |
| `GET /api/opportunities` | Opportunity Score gộp PSI + SEO + Ads + Clarity |
| `GET /api/alerts` | Alert monitor hợp nhất, kèm Evidence + Confidence |
| `GET /api/ads/*` · `GET /api/clarity` | Google Ads/landing page/Clarity insights |
| `GET/PUT /api/config` | Config động PSI + SEO (persist qua Sheet) |
| `POST /api/check` · `POST /api/seo/run` | Trigger chạy trực tiếp |
| `GET /api/memory/records` | Fact dài hạn đã được user ghi nhớ rõ ràng |
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
