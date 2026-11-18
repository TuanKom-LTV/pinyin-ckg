"""Đăng nhập bằng mật khẩu khi truy cập PinYin từ xa (qua Cloudflare Tunnel).

- Mở trực tiếp trên máy này (127.0.0.1, không qua tunnel): vào thẳng, không hỏi mật khẩu.
- Mọi request đi qua tunnel (có header Cf-Connecting-IP / X-Forwarded-For) phải đăng nhập.
- Mật khẩu: PINYIN_PASSWORD trong file .env. Đổi mật khẩu thì mọi phiên cũ tự hết hạn.
- Sai mật khẩu 5 lần trong 15 phút từ một IP thì khóa IP đó 15 phút.
"""

import hashlib
import hmac
import html
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

from dotenv import load_dotenv
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core import users


ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ENV_FILE)

COOKIE = "pinyin_auth"
SESSION_DAYS = 30
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
PUBLIC_PATHS = {"/login", "/logout", "/dang-ky", "/xac-minh", "/gui-lai-xac-minh", "/ea/sync", "/static/images/favicon.svg", "/health", "/mien-tru-trach-nhiem"}

_fails: dict[str, list[float]] = {}

# Logo chữ K có thân nến (giống biểu tượng tab của PinYin CKG)
LOGO_K = ('<svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#131824"/>'
          '<line x1="24" y1="10" x2="24" y2="54" stroke="#2ecc71" stroke-width="3" stroke-linecap="round"/>'
          '<rect x="18" y="17" width="12" height="30" rx="2" fill="#2ecc71"/>'
          '<path d="M31 32 46 16M31 32 46 48" stroke="#e8ecf3" stroke-width="7" stroke-linecap="round" fill="none"/></svg>')
FAVICON = "data:image/svg+xml," + quote(LOGO_K.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" ', 1))


# ---------- Hai quyền: "chu" (toàn quyền) và "xem" (chỉ xem phân tích) ----------
ROLES = ("chu", "xem")

# Người xem không được gọi các đường dẫn này (vào lệnh, xem số dư/lệnh/lịch sử tài khoản)
VIEW_BLOCK_PREFIXES = ("/api/v1/trade", "/ckg/api/account", "/ckg/api/history",
                       "/ckg/api/order", "/ckg/api/close", "/ckg/api/reverse",
                       "/docs", "/redoc", "/openapi.json",
                       "/ckg/api/q", "/ckg/api/auto", "/quan-ly")   # buồng lái nhanh, bot, quản lý khách
AI_PATH = "/api/v1/ai/analyze"


def _password(role: str = "chu"):
    return os.getenv("PINYIN_PASSWORD" if role == "chu" else "PINYIN_VIEW_PASSWORD", "")


def _secret(role: str):
    # Đổi mật khẩu của quyền nào thì chỉ các phiên của quyền đó hết hạn
    if role.startswith("u-"):
        return os.getenv("PINYIN_SECRET", "") + "|" + role + "|" + (users.session_key(role[2:]) or secrets.token_hex(8))
    return os.getenv("PINYIN_SECRET", "") + "|" + role + "|" + _password(role)


def _sign(exp: int, role: str) -> str:
    mac = hmac.new(_secret(role).encode(), f"{exp}.{role}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{role}.{mac}"


def _who(token: str | None):
    """Cookie hợp lệ → (quyền, tên khách | None); không hợp lệ → (None, None).
    Khách (u-<tên>) luôn có quyền "xem"; bị khóa / hết hạn / đổi mật khẩu thì phiên mất hiệu lực ngay."""
    if not token or token.count(".") != 2:
        return None, None
    exp_s, role, _ = token.split(".")
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return None, None
    if role.startswith("u-"):
        user = role[2:]
        if not users.USER_RE.match(user) or not users.active(user):
            return None, None
        return ("xem", user) if hmac.compare_digest(token, _sign(int(exp_s), role)) else (None, None)
    if role not in ROLES or not _password(role):
        return None, None
    return (role, None) if hmac.compare_digest(token, _sign(int(exp_s), role)) else (None, None)


def _role(token: str | None) -> str | None:
    return _who(token)[0]


def viewer_blocked(path: str, method: str) -> bool:
    if path.startswith(VIEW_BLOCK_PREFIXES):
        return True
    if path in (AI_PATH, "/ckg/api/ai"):
        # Gọi AI tốn phí API: người xem chỉ đọc bản phân tích gần nhất (GET /ckg/api/ai/last)
        return os.getenv("PINYIN_VIEW_AI") != "1"
    # Mọi request ghi/thay đổi khác cũng bị chặn với người xem
    return method not in ("GET", "HEAD", "OPTIONS")


def is_remote(request: Request) -> bool:
    h = request.headers
    if h.get("cf-connecting-ip") or h.get("x-forwarded-for") or h.get("cf-ray"):
        return True
    host = request.client.host if request.client else ""
    return host not in ("127.0.0.1", "::1", "localhost")


def _ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "?"))


def _locked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < LOCK_SECONDS]
    return len(_fails[ip]) >= MAX_FAILS


def _safe_next(n: str) -> str:
    return n if n.startswith("/") and not n.startswith("//") else "/"


def _login_page(msg: str = "", nxt: str = "/") -> HTMLResponse:
    body = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PinYin · Đăng nhập</title>
<meta name="robots" content="noindex,nofollow"><link rel="icon" type="image/svg+xml" href="{FAVICON}">
<style>
:root{{color-scheme:dark;--bg:#0b0e14;--card:#0d1320;--tx:#e8ecf3;--mut:#8b95a7;--bd:#1c2a3d;--acc:#2563eb;--no:#ff5c5c}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}}
form{{width:100%;max-width:340px;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:22px}}
h1{{font-size:19px;margin:0 0 4px}}h1.lg{{display:flex;align-items:center;gap:10px;font-size:22px;font-weight:800}}h1.lg svg{{width:38px;height:38px;flex:none}}p{{margin:0 0 16px;color:var(--mut);font-size:13px}}
input{{width:100%;font:inherit;padding:10px;border:1px solid var(--bd);border-radius:9px;background:var(--bg);color:var(--tx)}}
button{{width:100%;margin-top:12px;font:inherit;font-weight:700;padding:10px;border:0;border-radius:9px;background:var(--acc);color:#fff;cursor:pointer}}
.e{{color:var(--no);font-size:13px;margin-top:10px}}
.alt{{text-align:center;margin-top:10px;font-size:13px;color:var(--mut)}}.alt a{{color:#22d3ee}}
.tg{{display:flex;justify-content:center;align-items:center;gap:6px;margin-top:14px;color:#229ED9;font-size:13px;font-weight:600;text-decoration:none}}
</style></head><body>
<form method="post" action="/login"><h1 class="lg">{LOGO_K}<span>PinYin</span></h1><p>Đăng nhập để xem phân tích.</p>
<input type="hidden" name="next" value="{html.escape(nxt)}">
<input type="email" name="user" placeholder="Email" autocomplete="username" autofocus style="margin-bottom:10px">
<input type="password" name="pw" placeholder="Mật khẩu" autocomplete="current-password" required>
<button>Đăng nhập</button>{f'<div class="e">{html.escape(msg)}</div>' if msg else ''}<a class="tg" href="https://t.me/TuanKom" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M21.4 4.2 2.9 11.3c-1.3.5-1.2 1.2-.2 1.5l4.7 1.5 1.8 5.6c.2.6.4.8.9.8.4 0 .6-.2.9-.4l2.3-2.2 4.7 3.5c.9.5 1.5.2 1.7-.8l3.1-14.5c.3-1.3-.5-1.8-1.4-1.1zM8.6 14l9-5.7c.4-.3.8-.1.5.2l-7.4 6.7-.3 3.2z"/></svg>Hỗ trợ: Telegram @TuanKom</a><div class="alt">Chưa có tài khoản? <a href="/dang-ky">Đăng ký</a> · <a href="/gui-lai-xac-minh">Chưa nhận thư xác nhận?</a><br><a href="/mien-tru-trach-nhiem">⚠️ Miễn trừ trách nhiệm về rủi ro</a></div></form></body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


async def auth_middleware(request: Request, call_next):
    path = request.url.path

    if not is_remote(request):
        request.state.role, request.state.user = "chu", None
        return await call_next(request)

    if path == "/login":
        nxt = _safe_next(request.query_params.get("next", "/"))
        if request.method != "POST":
            return _login_page(nxt=nxt)
        ip = _ip(request)
        if _locked(ip):
            return _login_page("Sai quá nhiều lần. Thử lại sau 15 phút.", nxt)
        form = parse_qs((await request.body()).decode("utf-8", "ignore"))
        pw = (form.get("pw") or [""])[0]
        uname = (form.get("user") or [""])[0].strip()
        nxt = _safe_next((form.get("next") or ["/"])[0])
        if uname:
            user, err = users.login(uname, pw)
            if not user:
                if err.startswith("Sai"):
                    _fails.setdefault(ip, []).append(time.time())
                return _login_page(err, nxt)
            _fails.pop(ip, None)
            resp = RedirectResponse(nxt if nxt != "/" else "/ckg/", status_code=303)
            resp.set_cookie(COOKIE, _sign(int(time.time()) + SESSION_DAYS * 86400, "u-" + user),
                            max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="lax", path="/")
            return resp
        if not _password("chu"):
            return _login_page("Máy chủ chưa đặt PINYIN_PASSWORD trong .env.", nxt)
        role = None
        for r in ROLES:
            if _password(r) and hmac.compare_digest(pw.encode(), _password(r).encode()):
                role = r
        if not role:
            _fails.setdefault(ip, []).append(time.time())
            return _login_page("Sai mật khẩu.", nxt)
        _fails.pop(ip, None)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE, _sign(int(time.time()) + SESSION_DAYS * 86400, role),
                        max_age=SESSION_DAYS * 86400, httponly=True, secure=True,
                        samesite="lax", path="/")
        return resp

    if path == "/logout":
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    role, cust = _who(request.cookies.get(COOKIE))
    request.state.user = cust
    # khách được cấp quyền giao dịch: dùng buồng lái EA của chính mình
    ea_ok = bool(cust) and path.startswith("/ckg/api/ea/") and users.can_trade(cust)
    # trang công khai (đăng ký, xác nhận email...) luôn dùng được, kể cả khi đang đăng nhập tài khoản xem
    if role == "xem" and not ea_ok and path not in PUBLIC_PATHS and viewer_blocked(path, request.method):
        return JSONResponse(
            {"ok": False, "xem": True,
             "loi": ("Tài khoản chỉ xem: chỉ đọc được bản phân tích AI gần nhất, không yêu cầu phân tích mới."
                     if path in (AI_PATH, "/ckg/api/ai") else
                     "Tài khoản chỉ xem: không có quyền vào lệnh hoặc xem tài khoản MT5.")},
            status_code=403)
    if path in PUBLIC_PATHS or role:
        request.state.role = role
        resp = await call_next(request)
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        if role:
            resp.headers["X-PinYin-Role"] = role
        return resp

    accept = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accept:
        return RedirectResponse("/login?next=" + quote(path), status_code=303)
    return JSONResponse({"ok": False, "loi": "Chưa đăng nhập."}, status_code=401)


def ensure_env():
    """Tạo mật khẩu và khóa bí mật ngẫu nhiên trong .env nếu chưa có. Trả về mật khẩu mới (nếu vừa tạo)."""
    created = None
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    keys = {l.split("=", 1)[0].strip() for l in lines if "=" in l and not l.lstrip().startswith("#")}
    if "PINYIN_PASSWORD" not in keys:
        created = secrets.token_urlsafe(12)
        lines.append(f"PINYIN_PASSWORD={created}")
    if "PINYIN_SECRET" not in keys:
        lines.append(f"PINYIN_SECRET={secrets.token_hex(32)}")
    if "PINYIN_VIEW_PASSWORD" not in keys:
        lines.append("# Mật khẩu CHỈ XEM để đưa cho người khác (không vào được lệnh, không thấy tài khoản)")
        lines.append(f"PINYIN_VIEW_PASSWORD={secrets.token_urlsafe(9)}")
    if created or not {"PINYIN_SECRET", "PINYIN_VIEW_PASSWORD"} <= keys:
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return created
