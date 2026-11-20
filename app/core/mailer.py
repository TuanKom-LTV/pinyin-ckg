"""Gửi email (xác nhận đăng ký) qua Resend API, người gửi no-reply@pinyin.id.vn. Khóa API: RESEND_API_KEY trong .env.
Kèm kiểm tra tên miền email có thật (bản ghi MX qua DNS-over-HTTPS) và chặn email rác tạm thời."""

import http.client
import json
import os
import socket
import ssl
import urllib.parse

FROM_DEFAULT = "PinYin CKG <no-reply@pinyin.id.vn>"
DISPOSABLE = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com", "yopmail.com",
    "temp-mail.org", "tempmail.com", "tempmailo.com", "tempmail.net", "trashmail.com", "getnada.com", "maildrop.cc",
    "dispostable.com", "fakeinbox.com", "mohmal.com", "emailondeck.com", "minuteinbox.com", "throwawaymail.com",
    "mailnesia.com", "mintemail.com", "spamgourmet.com", "tempinbox.com", "moakt.com", "emailfake.com", "tmail.ws",
    "10mail.org", "burnermail.io", "inboxkitten.com", "mail.tm", "mailpoof.com", "tempr.email", "byom.de",
}


class _V4Conn(http.client.HTTPSConnection):
    """Ép IPv4 (máy nhà IPv6 hỏng làm Python treo)."""
    def connect(self):
        ip = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        sock = socket.create_connection((ip, self.port), self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)


def configured():
    return bool(os.getenv("RESEND_API_KEY", "").strip())


def send(to, subject, html, key=None):
    """Trả (ok, lỗi)."""
    key = (key or os.getenv("RESEND_API_KEY", "")).strip()
    if not key:
        return False, "Chưa cấu hình gửi email (RESEND_API_KEY)."
    body = json.dumps({"from": os.getenv("MAIL_FROM", FROM_DEFAULT), "to": [to], "subject": subject, "html": html})
    conn = _V4Conn("api.resend.com", timeout=20)
    try:
        conn.request("POST", "/emails", body, {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                               "User-Agent": "PinYin-CKG/1.0"})
        r = conn.getresponse()
        data = r.read().decode("utf-8", "ignore")
        if r.status in (200, 201, 202):
            return True, None
        try:
            msg = json.loads(data).get("message") or data
        except Exception:
            msg = data
        return False, f"Resend {r.status}: {msg[:200]}"
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()


def domain_ok(email):
    """Tên miền email có nhận thư không (có bản ghi MX hoặc A). Lỗi mạng thì cho qua."""
    dom = email.rsplit("@", 1)[-1].lower()
    if dom in DISPOSABLE:
        return False, "Không nhận email tạm thời / email rác. Dùng email thật của bạn."
    try:
        for typ in ("MX", "A"):
            conn = _V4Conn("cloudflare-dns.com", timeout=8)
            conn.request("GET", "/dns-query?" + urllib.parse.urlencode({"name": dom, "type": typ}),
                         headers={"Accept": "application/dns-json"})
            j = json.loads(conn.getresponse().read().decode())
            conn.close()
            if j.get("Status") == 3:            # NXDOMAIN: tên miền không tồn tại
                return False, f"Tên miền email \"{dom}\" không tồn tại. Kiểm tra lại (vd: gmail.com)."
            if j.get("Answer"):
                return True, None
        return False, f"Tên miền email \"{dom}\" không nhận thư. Kiểm tra lại địa chỉ email."
    except Exception:
        return True, None


def verify_html(name, link):
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:520px;margin:auto;background:#0d1320;color:#e8ecf3;border-radius:14px;padding:26px">
<h2 style="margin:0 0 6px;color:#22d3ee">PinYin CKG</h2><p style="color:#8b95a7;margin:0 0 18px;font-size:13px">Hệ thống phân tích CKG đa khung &amp; cố vấn AI thực chiến</p>
<p>Chào {name},</p><p>Bạn vừa đăng ký tài khoản xem phân tích PinYin CKG. Bấm nút dưới đây để xác nhận email:</p>
<p style="text-align:center;margin:24px 0"><a href="{link}" style="background:#2563eb;color:#fff;text-decoration:none;font-weight:700;padding:12px 26px;border-radius:9px;display:inline-block">Xác nhận email</a></p>
<p style="font-size:13px;color:#8b95a7">Link có hiệu lực 24 giờ. Sau khi xác nhận, tài khoản sẽ được duyệt và bạn đăng nhập bằng email này.<br>
Nếu bạn không đăng ký, hãy bỏ qua thư này.</p><p style="font-size:12px;color:#8b95a7;word-break:break-all">Link: {link}</p></div>"""


def reset_html(name, link):
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:520px;margin:auto;background:#0d1320;color:#e8ecf3;border-radius:14px;padding:26px">
<h2 style="margin:0 0 6px;color:#22d3ee">PinYin CKG</h2><p style="color:#8b95a7;margin:0 0 18px;font-size:13px">Hệ thống phân tích CKG đa khung &amp; cố vấn AI thực chiến</p>
<p>Chào {name},</p><p>Bạn (hoặc ai đó) vừa yêu cầu đặt lại mật khẩu PinYin CKG cho email này. Bấm nút dưới đây để đặt mật khẩu mới:</p>
<p style="text-align:center;margin:24px 0"><a href="{link}" style="background:#2563eb;color:#fff;text-decoration:none;font-weight:700;padding:12px 26px;border-radius:9px;display:inline-block">Đặt lại mật khẩu</a></p>
<p style="font-size:13px;color:#8b95a7">Link có hiệu lực 1 giờ và chỉ dùng được 1 lần.<br>
Nếu bạn không yêu cầu, hãy bỏ qua thư này, mật khẩu cũ vẫn giữ nguyên.</p><p style="font-size:12px;color:#8b95a7;word-break:break-all">Link: {link}</p></div>"""


def remind_html(name, email, link):
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:520px;margin:auto;background:#0d1320;color:#e8ecf3;border-radius:14px;padding:26px">
<h2 style="margin:0 0 6px;color:#22d3ee">PinYin CKG</h2><p style="color:#8b95a7;margin:0 0 18px;font-size:13px">Hệ thống phân tích CKG đa khung &amp; cố vấn AI thực chiến</p>
<p>Chào {name},</p><p>Bạn (hoặc ai đó) vừa tìm lại email đăng nhập PinYin CKG. Tài khoản của bạn đăng nhập bằng email:</p>
<p style="text-align:center;font-size:18px;font-weight:800;margin:18px 0">{email}</p>
<p style="text-align:center;margin:24px 0"><a href="{link}" style="background:#2563eb;color:#fff;text-decoration:none;font-weight:700;padding:12px 26px;border-radius:9px;display:inline-block">Đăng nhập</a></p>
<p style="font-size:13px;color:#8b95a7">Quên mật khẩu thì bấm "Quên mật khẩu?" ở trang đăng nhập.<br>Nếu bạn không yêu cầu, hãy bỏ qua thư này.</p></div>"""
