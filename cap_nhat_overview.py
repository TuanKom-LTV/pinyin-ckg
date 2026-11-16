from pathlib import Path

path = Path("frontend/index.html")
content = path.read_text(encoding="utf-8")

replacements = [
    (
        '<span class="demo-label">DỮ LIỆU MINH HỌA</span>',
        '<span class="demo-label" id="overview-data-status">ĐANG KIỂM TRA MT5</span>'
    ),
    (
        '<h2>XAUUSD</h2>\n                    <small>Vàng / Đô la Mỹ</small>',
        '<h2 id="overview-symbol">XAUUSD</h2>\n                    <small>Vàng / Đô la Mỹ</small>'
    ),
    (
        '<h2>2.650,00</h2>\n                    <small>Dữ liệu minh họa</small>',
        '<h2 id="overview-price">Đang tải...</h2>\n                    <small id="overview-price-note">Đang kiểm tra dữ liệu MT5</small>'
    ),
    (
        '<h2 class="green">Tăng</h2>\n                    <small>Minh họa, không phải tín hiệu</small>',
        '<h2 id="overview-trend">Chưa phân tích</h2>\n                    <small>Chưa có kết quả phân tích xu hướng</small>'
    ),
    (
        '<h2 class="blue">Sẵn sàng</h2>\n                    <small>Chưa kết nối mô hình AI</small>',
        '<h2 class="blue">Chưa kết nối</h2>\n                    <small>Mô hình AI chưa được tích hợp</small>'
    )
]

for old, new in replacements:
    count = content.count(old)
    if count != 1:
        raise SystemExit(
            f"DUNG LAI: Tim thay {count} lan cho doan can sua. "
            "Khong ghi file."
        )
    content = content.replace(old, new, 1)

path.write_text(content, encoding="utf-8")
print("DA CAP NHAT HTML TONG QUAN")
