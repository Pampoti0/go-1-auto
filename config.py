# ============================================================
# CẤU HÌNH — đọc từ biến môi trường (xem .env.example)
# KHÔNG hardcode secrets vào file này: repo sẽ public khi nộp bài.
# ============================================================

import os

try:  # tự nạp .env khi chạy local (không bắt buộc khi deploy)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Google PageSpeed Insights API Key
PSI_API_KEY = os.getenv("PSI_API_KEY", "")

# Google Sheets ID (lấy từ URL của sheet)
SHEET_ID = os.getenv("SHEET_ID", "")

# Tên sheet tab legacy (run mới ghi vào tab YYYY-MM)
SHEET_TAB = os.getenv("SHEET_TAB", "PSI Results")

# Service Account: HOẶC đường dẫn file JSON, HOẶC nội dung JSON
# (SERVICE_ACCOUNT_JSON tiện cho deploy — dán nguyên nội dung file vào env var)
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "")

# Danh sách URL cần kiểm tra — override bằng env URLS (phân tách bởi dấu phẩy)
_DEFAULT_URLS = [
    "https://greennode.ai/",
    "https://greennode.ai/product/gpu-instances",
    "https://greennode.ai/product/cpu-instances",
    "https://greennode.ai/product/vks",
    "https://greennode.ai/product/bare-metal-h100",
    "https://greennode.ai/product/file-storage",
    "https://greennode.ai/product/object-storage",
    "https://greennode.ai/product/block-storage",
    "https://greennode.ai/product/Load-Balancer",
    "https://greennode.ai/product/Interconnect",
    "https://greennode.ai/product/cross-connect",
    "https://greennode.ai/product/vpn-site-to-site",
    "https://greennode.ai/product/vdb-postgresql",
    "https://greennode.ai/product/vdb-mysql",
    "https://greennode.ai/product/vdb-kafka",
    "https://greennode.ai/product/vdb-redis",
    "https://greennode.ai/product/vdb-opensearch",
    "https://greennode.ai/product/vdb-mariadb",
    "https://greennode.ai/contact-us",
]
URLS = [u.strip() for u in os.getenv("URLS", "").split(",") if u.strip()] or _DEFAULT_URLS

# "mobile" / "desktop" — phân tách bởi dấu phẩy
STRATEGIES = [s.strip() for s in os.getenv("STRATEGIES", "mobile,desktop").split(",") if s.strip()]

# Lịch chạy tự động: daily | weekly | monthly
SCHEDULE_MODE = os.getenv("SCHEDULE_MODE", "monthly")
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "08:00")
SCHEDULE_WEEKDAY = os.getenv("SCHEDULE_WEEKDAY", "monday")
SCHEDULE_DAY_OF_MONTH = int(os.getenv("SCHEDULE_DAY_OF_MONTH", "4"))  # 1–28

# Số giây chờ giữa mỗi request API (tránh rate limit)
REQUEST_DELAY = int(os.getenv("REQUEST_DELAY", "2"))
