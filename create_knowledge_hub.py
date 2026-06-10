"""
Tạo tab Knowledge Hub trong Google Sheet PSI — tổng hợp định nghĩa và ngưỡng các chỉ số.
Chạy 1 lần: python create_knowledge_hub.py
"""

from psi_checker import get_sheets_service, SHEET_ID

TAB = "📚 Knowledge Hub"

# ── Nội dung ──────────────────────────────────────────────────────────────────

SECTIONS = [
    # [row_type, data]
    # row_type: "title" | "header" | "row" | "blank"
    ("title",  ["🚀 PAGESPEED INSIGHTS — KNOWLEDGE HUB"]),
    ("subtitle", ["Tổng hợp định nghĩa, ngưỡng đánh giá và cách cải thiện các chỉ số hiệu suất website"]),
    ("blank",  []),

    # ── Performance Score ────────────────────────────────────────────────────
    ("section", ["📊 PERFORMANCE SCORE"]),
    ("header",  ["Chỉ số", "Định nghĩa", "Tốt 🟢", "Cần cải thiện 🟡", "Kém 🔴", "Cách cải thiện"]),
    ("row", [
        "Performance Score",
        "Điểm tổng hợp từ 0–100 do Lighthouse tính toán dựa trên các chỉ số Core Web Vitals và Lab Data. "
        "Điểm càng cao website càng nhanh và trải nghiệm người dùng càng tốt.",
        "≥ 90", "50 – 89", "< 50",
        "Tối ưu hình ảnh, giảm JavaScript không cần thiết, bật caching, dùng CDN"
    ]),
    ("blank",  []),

    # ── Core Web Vitals ──────────────────────────────────────────────────────
    ("section", ["⚡ CORE WEB VITALS (Google dùng để xếp hạng SEO)"]),
    ("header",  ["Chỉ số", "Định nghĩa", "Tốt 🟢", "Cần cải thiện 🟡", "Kém 🔴", "Cách cải thiện"]),
    ("row", [
        "LCP — Largest Contentful Paint",
        "Thời gian để phần tử lớn nhất trên màn hình (ảnh hero, heading lớn) được hiển thị hoàn toàn. "
        "Đo tốc độ tải nội dung chính mà người dùng thấy đầu tiên.",
        "≤ 2500 ms", "2501 – 4000 ms", "> 4000 ms",
        "Tối ưu ảnh (WebP, lazy load), preload font, dùng CDN, cải thiện TTFB server"
    ]),
    ("row", [
        "CLS — Cumulative Layout Shift",
        "Đo mức độ các phần tử trên trang bị dịch chuyển bất ngờ trong quá trình tải "
        "(ví dụ: nút bị đẩy xuống do ảnh load sau). Giá trị càng thấp càng ổn định.",
        "≤ 0.1", "0.101 – 0.25", "> 0.25",
        "Đặt kích thước width/height cho ảnh và video, tránh chèn nội dung động phía trên fold"
    ]),
    ("row", [
        "INP — Interaction to Next Paint",
        "Đo độ trễ từ lúc người dùng tương tác (click, tap, gõ phím) đến lúc trình duyệt phản hồi bằng cách vẽ lại màn hình. "
        "Thay thế FID từ 2024, đo toàn bộ vòng đời tương tác.",
        "≤ 200 ms", "201 – 500 ms", "> 500 ms",
        "Giảm JavaScript nặng, tách code theo route, tránh blocking main thread"
    ]),
    ("blank",  []),

    # ── Lab Metrics ─────────────────────────────────────────────────────────
    ("section", ["🔬 LAB METRICS (Lighthouse đo trong môi trường kiểm soát)"]),
    ("header",  ["Chỉ số", "Định nghĩa", "Tốt 🟢", "Cần cải thiện 🟡", "Kém 🔴", "Cách cải thiện"]),
    ("row", [
        "FCP — First Contentful Paint",
        "Thời gian từ lúc bắt đầu tải trang đến khi trình duyệt hiển thị nội dung đầu tiên "
        "(text, ảnh, canvas...). Cho biết người dùng phải chờ bao lâu để thấy có gì đó xuất hiện.",
        "≤ 1800 ms", "1801 – 3000 ms", "> 3000 ms",
        "Giảm thời gian phản hồi server, loại bỏ render-blocking CSS/JS, preload tài nguyên quan trọng"
    ]),
    ("row", [
        "TBT — Total Blocking Time",
        "Tổng thời gian main thread bị chặn (blocked) giữa FCP và TTI. "
        "Khi main thread bị chặn >50ms, người dùng cảm thấy trang bị đơ, không phản hồi.",
        "≤ 200 ms", "201 – 600 ms", "> 600 ms",
        "Chia nhỏ JavaScript thành các chunk, dùng web workers, loại bỏ thư viện nặng không cần thiết"
    ]),
    ("row", [
        "TTFB — Time to First Byte",
        "Thời gian từ lúc browser gửi request đến khi nhận byte đầu tiên từ server. "
        "Phản ánh hiệu suất server, network, và caching.",
        "≤ 800 ms", "801 – 1800 ms", "> 1800 ms",
        "Dùng CDN, tối ưu database query, bật server-side caching, nâng cấp hosting"
    ]),
    ("row", [
        "Speed Index",
        "Đo tốc độ hiển thị nội dung visual trong quá trình tải trang. "
        "Tính bằng cách phân tích video quay màn hình lúc load — càng thấp càng tốt.",
        "≤ 3400 ms", "3401 – 5800 ms", "> 5800 ms",
        "Tối ưu thứ tự tải tài nguyên, giảm CSS/JS blocking, tối ưu ảnh above-the-fold"
    ]),
    ("blank",  []),

    # ── Mobile vs Desktop ────────────────────────────────────────────────────
    ("section", ["📱 MOBILE vs 🖥️ DESKTOP"]),
    ("header",  ["", "Mobile", "Desktop", "Lưu ý"]),
    ("row", ["Điều kiện test",
             "Mạng 4G giả lập (8.7 Mbps), CPU throttle 4x",
             "Mạng cáp quang, không throttle CPU",
             "Mobile khó đạt điểm cao hơn Desktop do điều kiện khắt khe hơn"]),
    ("row", ["Tầm quan trọng SEO",
             "⭐⭐⭐⭐⭐ Google ưu tiên mobile-first indexing",
             "⭐⭐⭐",
             "Nên tối ưu mobile trước"]),
    ("row", ["Điểm chênh lệch thông thường",
             "Thường thấp hơn Desktop 10–30 điểm",
             "Cao hơn",
             "Chênh lệch >40 điểm cần xem lại responsive design"]),
    ("blank",  []),

    # ── Màu sắc ─────────────────────────────────────────────────────────────
    ("section", ["🎨 Ý NGHĨA MÀU SẮC TRONG SHEET KẾT QUẢ"]),
    ("header",  ["Màu", "Ý nghĩa", "Hành động đề xuất"]),
    ("row", ["🟢 Xanh lá", "Đạt ngưỡng tốt — trải nghiệm người dùng tốt", "Duy trì, tiếp tục theo dõi"]),
    ("row", ["🟡 Vàng",    "Cần cải thiện — người dùng có thể cảm nhận chậm", "Lên kế hoạch tối ưu trong sprint tới"]),
    ("row", ["🔴 Đỏ",      "Kém — ảnh hưởng xấu đến SEO và trải nghiệm",     "Ưu tiên fix ngay"]),
]

# ── Màu sắc cho formatting ────────────────────────────────────────────────────

DARK_BLUE   = {"red": 0.157, "green": 0.306, "blue": 0.475}   # tiêu đề chính
MID_BLUE    = {"red": 0.263, "green": 0.490, "blue": 0.698}   # section
LIGHT_BLUE  = {"red": 0.812, "green": 0.886, "blue": 0.953}   # header row
LIGHT_GRAY  = {"red": 0.953, "green": 0.953, "blue": 0.953}   # row lẻ
WHITE       = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
WHITE_TEXT  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
BLACK_TEXT  = {"red": 0.0,   "green": 0.0,   "blue": 0.0}
SUBTITLE_BG = {"red": 0.231, "green": 0.416, "blue": 0.612}


def build_cell(value, bold=False, bg=None, fg=None, font_size=10, wrap=True, align="LEFT"):
    fmt = {
        "textFormat": {
            "bold": bold,
            "fontSize": font_size,
            "foregroundColor": fg or BLACK_TEXT,
        },
        "wrapStrategy": "WRAP" if wrap else "OVERFLOW_CELL",
        "verticalAlignment": "MIDDLE",
        "horizontalAlignment": align,
    }
    if bg:
        fmt["backgroundColor"] = bg
    return {"userEnteredValue": {"stringValue": str(value)}, "userEnteredFormat": fmt}


def build_rows(sections):
    rows = []
    for row_type, data in sections:
        if row_type == "blank":
            rows.append([build_cell("")])
        elif row_type == "title":
            rows.append([build_cell(data[0], bold=True, bg=DARK_BLUE, fg=WHITE_TEXT, font_size=14)])
        elif row_type == "subtitle":
            rows.append([build_cell(data[0], bold=False, bg=SUBTITLE_BG, fg=WHITE_TEXT, font_size=10)])
        elif row_type == "section":
            rows.append([build_cell(data[0], bold=True, bg=MID_BLUE, fg=WHITE_TEXT, font_size=11)])
        elif row_type == "header":
            rows.append([build_cell(v, bold=True, bg=LIGHT_BLUE, font_size=10) for v in data])
        elif row_type == "row":
            rows.append([build_cell(v, font_size=10) for v in data])
    return rows


def create_knowledge_hub():
    svc = get_sheets_service()

    # Tạo tab nếu chưa có
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]
    sheet_id = None

    if TAB not in existing:
        resp = svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": TAB, "index": 0}}}]},
        ).execute()
        sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        print(f"Đã tạo tab '{TAB}'")
    else:
        sheet_id = next(s["properties"]["sheetId"] for s in meta["sheets"] if s["properties"]["title"] == TAB)
        print(f"Tab '{TAB}' đã tồn tại, cập nhật nội dung...")

    # Ghi dữ liệu
    rows = build_rows(SECTIONS)
    svc.spreadsheets().values().clear(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A1:Z500"
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{TAB}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[c["userEnteredValue"]["stringValue"] for c in r] for r in rows]},
    ).execute()

    # Áp dụng format
    format_requests = []
    for i, (row_type, data) in enumerate(SECTIONS):
        if row_type == "blank":
            continue
        cells = build_rows([(row_type, data)])[0]
        for j, cell in enumerate(cells):
            format_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i, "endRowIndex": i + 1,
                        "startColumnIndex": j, "endColumnIndex": j + 1,
                    },
                    "cell": {"userEnteredFormat": cell["userEnteredFormat"]},
                    "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment,horizontalAlignment)",
                }
            })

    # Merge tiêu đề + subtitle + section sang toàn bộ cột A–F
    for i, (row_type, _) in enumerate(SECTIONS):
        if row_type in ("title", "subtitle", "section"):
            format_requests.append({
                "mergeCells": {
                    "range": {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i+1, "startColumnIndex": 0, "endColumnIndex": 6},
                    "mergeType": "MERGE_ALL",
                }
            })

    # Độ rộng cột
    col_widths = [220, 380, 100, 160, 100, 300]
    for ci, w in enumerate(col_widths):
        format_requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": ci, "endIndex": ci+1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })

    # Freeze hàng đầu
    format_requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    })

    svc.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID, body={"requests": format_requests}
    ).execute()

    print("✅ Knowledge Hub đã sẵn sàng!")


if __name__ == "__main__":
    create_knowledge_hub()
