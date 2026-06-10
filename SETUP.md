# Hướng dẫn cài đặt PSI Checker

## Bước 1 — Lấy PageSpeed Insights API Key

1. Vào https://console.cloud.google.com/
2. Tạo project mới (hoặc chọn project có sẵn)
3. Vào **APIs & Services → Library**, tìm "PageSpeed Insights API" → Enable
4. Vào **APIs & Services → Credentials → Create Credentials → API Key**
5. Copy API Key → dán vào `config.py` ở trường `PSI_API_KEY`

## Bước 2 — Tạo Service Account để ghi Google Sheet

1. Vào **IAM & Admin → Service Accounts → Create Service Account**
2. Đặt tên bất kỳ (ví dụ: `psi-checker`) → Create
3. Không cần gán role gì → Done
4. Click vào service account vừa tạo → Tab **Keys → Add Key → JSON**
5. File JSON được tải về → đổi tên thành `service_account.json`
6. Đặt file này vào cùng thư mục với `psi_checker.py`

## Bước 3 — Chia sẻ Google Sheet với Service Account

1. Tạo Google Sheet mới (hoặc dùng sheet có sẵn)
2. Copy Sheet ID từ URL:
   `https://docs.google.com/spreadsheets/d/**SHEET_ID_Ở_ĐÂY**/edit`
3. Vào **Share** → paste email của service account (trong file JSON, trường `client_email`)
4. Cấp quyền **Editor**

## Bước 4 — Cấu hình config.py

Mở `config.py` và điền:
- `PSI_API_KEY` — API key từ bước 1
- `SHEET_ID` — Sheet ID từ bước 3
- `URLS` — danh sách URL cần kiểm tra
- `SCHEDULE_TIME` — giờ chạy hàng ngày (mặc định `"08:00"`)

## Bước 5 — Cài thư viện và chạy

```bash
# Cài thư viện
pip install -r requirements.txt

# Chạy 1 lần (kiểm tra thử)
python psi_checker.py --once

# Chạy scheduler liên tục (định kỳ theo lịch trong config.py)
python psi_checker.py
```

## Cấu trúc cột trong Google Sheet

| Cột | Nội dung |
|-----|----------|
| Timestamp | Thời điểm kiểm tra |
| URL | URL được kiểm tra |
| Strategy | MOBILE hoặc DESKTOP |
| Performance Score | Điểm 0–100 |
| FCP (ms) | First Contentful Paint |
| LCP (ms) | Largest Contentful Paint |
| CLS | Cumulative Layout Shift |
| TBT (ms) | Total Blocking Time |
| INP (ms) | Interaction to Next Paint |
| TTFB (ms) | Time to First Byte |
| Speed Index (ms) | Speed Index |
| *Category columns | Nhãn Fast/Moderate/Slow cho từng chỉ số |
