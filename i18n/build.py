"""Tạo frontend/i18n.js = từ điển (i18n/dict.json) + bộ dịch (i18n/runtime.js), đổi số phiên bản ở các thẻ <script>.

Từ điển: {"<câu tiếng Việt đã chuẩn hóa>": ["<English>", "<中文>"]}. Chuẩn hóa = gộp khoảng trắng, thay số / mã cặp bằng {0},{1}…
Thêm / sửa câu dịch: sửa i18n/dict.json rồi chạy:  python i18n/build.py
"""
import hashlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
DICT = ROOT / "i18n" / "dict.json"
RUNTIME = ROOT / "i18n" / "runtime.js"
OUT = ROOT / "frontend" / "i18n.js"
TAGGED = [ROOT / "frontend" / "ckg" / "index.html", ROOT / "app" / "core" / "users.py"]


def main():
    d = json.loads(DICT.read_text(encoding="utf-8"))
    d = {k: v for k, v in sorted(d.items()) if isinstance(v, list) and len(v) == 2}
    js = "window.PY_I18N=" + json.dumps(d, ensure_ascii=False, separators=(",", ":")) + ";\n" + RUNTIME.read_text(encoding="utf-8")
    OUT.write_text(js, encoding="utf-8", newline="\n")
    ver = hashlib.sha1(js.encode("utf-8")).hexdigest()[:10]
    for p in TAGGED:
        raw = p.read_bytes().decode("utf-8")
        new = re.sub(r"i18n\.js\?v=\w+", "i18n.js?v=" + ver, raw)
        if new != raw:
            p.write_bytes(new.encode("utf-8"))
    print(f"i18n.js: {len(d)} câu, {len(js) // 1024} KB, phiên bản {ver}")


if __name__ == "__main__":
    main()
