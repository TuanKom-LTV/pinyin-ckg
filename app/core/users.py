"""Tài khoản khách (chỉ xem): tự đăng ký → chủ duyệt + đặt thời hạn → đăng nhập bằng tên + mật khẩu riêng.

Lưu ở data/users.json (riêng từng máy, không lên GitHub). Mật khẩu băm PBKDF2-SHA256 + muối, không lưu bản gốc.
Trạng thái: cho (chờ duyệt) · ok (đang dùng) · khoa (bị khóa). Hết hạn (het_han < bây giờ) thì không đăng nhập được.
"""

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core import mailer

ROOT = Path(__file__).resolve().parents[2]
USERS_FILE = ROOT / "data" / "users.json"
VN = timezone(timedelta(hours=7))
EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
USER_RE = re.compile(r"^[0-9a-f]{16}$")      # mã khách trong cookie (email có dấu chấm nên không đưa thẳng vào cookie)
_lock = threading.RLock()
_cache = {"mtime": None, "data": {}}
_reg_ip: dict[str, list[float]] = {}


# ---------- Lưu trữ ----------

def load():
    with _lock:
        try:
            m = USERS_FILE.stat().st_mtime
        except FileNotFoundError:
            return {}
        if _cache["mtime"] != m:
            try:
                _cache["data"] = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            except Exception:
                _cache["data"] = {}
            _cache["mtime"] = m
            # bản cũ (khóa = tên đăng nhập): chuyển sang khóa = email, thêm mã id
            if any("@" not in k or "id" not in u for k, u in _cache["data"].items()):
                new = {}
                for k, u in _cache["data"].items():
                    key = (u.get("email") or k).lower()
                    if "@" in key and key not in new:
                        new[key] = {**u, "email": key, "id": u.get("id") or secrets.token_hex(8)}
                save(new)
                return load()
        return _cache["data"]


def by_id(uid):
    """Mã khách → (email, bản ghi) hoặc (None, None)."""
    for k, u in load().items():
        if u.get("id") == uid:
            return k, u
    return None, None


def save(data):
    with _lock:
        USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(USERS_FILE)
        _cache["mtime"] = None


def _hash(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def _check_pw(pw, stored):
    try:
        salt, h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(_hash(pw, salt).split("$")[1], h)


def fmt_date(ts):
    return datetime.fromtimestamp(ts, VN).strftime("%d/%m/%Y") if ts else "-"


def status_of(u):
    """Trạng thái thực tế: cho / ok / khoa / het (hết hạn)."""
    if u["tt"] == "ok" and u.get("het_han") and u["het_han"] < time.time():
        return "het"
    return u["tt"]


# ---------- Đăng ký / đăng nhập ----------

def register(f, ip):
    """f: dict form. Trả (ok, thông báo)."""
    now = time.time()
    _reg_ip[ip] = [t for t in _reg_ip.get(ip, []) if now - t < 3600]
    if len(_reg_ip[ip]) >= 3:
        return False, "Bạn đã đăng ký nhiều lần, thử lại sau 1 giờ."
    name = f.get("name", "").strip()[:60]
    phone = re.sub(r"[^\d+]", "", f.get("phone", ""))[:15]
    tg = f.get("tg", "").strip().lstrip("@")[:40]
    email = f.get("email", "").strip().lower()[:80]
    pw, pw2 = f.get("pw", ""), f.get("pw2", "")
    if len(name) < 2:
        return False, "Nhập họ tên."
    if not re.fullmatch(r"\+?\d{9,14}", phone):
        return False, "Số điện thoại Zalo không hợp lệ."
    if not EMAIL_RE.match(email):
        return False, "Email không hợp lệ."
    if tg and not re.fullmatch(r"[A-Za-z0-9_]{4,40}", tg):
        return False, "Telegram không hợp lệ (vd: @tennguoidung), hoặc để trống."
    if len(pw) < 6:
        return False, "Mật khẩu tối thiểu 6 ký tự."
    if pw != pw2:
        return False, "Hai lần nhập mật khẩu không khớp."
    ok, err = mailer.domain_ok(email)          # tên miền email có thật, không phải email rác
    if not ok:
        return False, err
    verify = mailer.configured()               # có cấu hình gửi thư → bắt xác nhận email
    tok = secrets.token_urlsafe(24)
    with _lock:
        data = dict(load())
        if email in data and data[email]["tt"] != "xac_minh":
            return False, "Email này đã được đăng ký."
        data[email] = {"ten": name, "sdt": phone, "email": email, "tg": tg, "mk": _hash(pw),
                       "tt": "xac_minh" if verify else "cho", "xm": tok if verify else None,
                       "xm_het": int(now + 86400) if verify else None,
                       "id": secrets.token_hex(8), "tao": int(now), "duyet": None, "het_han": None,
                       "dang_nhap": None, "ip": ip}
        save(data)
    _reg_ip[ip].append(now)
    if verify:
        sent, e = _send_verify(email, name, tok)
        if sent:
            return True, (f"Đã gửi thư xác nhận tới {email}. Mở hộp thư (xem cả mục Spam / Quảng cáo) và bấm "
                          "\"Xác nhận email\" để hoàn tất đăng ký. Link có hiệu lực 24 giờ.")
        _set(email, tt="cho", xm=None)         # gửi thư lỗi: không làm khách kẹt, chuyển thẳng sang chờ duyệt
        _notify(f"⚠️ Không gửi được thư xác nhận tới {email}: {e}")
    _notify_new(email)
    return True, "Đăng ký thành công! Tài khoản đang chờ duyệt, bạn sẽ đăng nhập được sau khi được duyệt."


def _base():
    return os.getenv("PUBLIC_URL", "https://pinyin.id.vn").rstrip("/")


def _send_verify(email, name, tok):
    link = f"{_base()}/xac-minh?t={tok}"
    return mailer.send(email, "Xác nhận email đăng ký PinYin CKG", mailer.verify_html(html.escape(name), link))


def _set(email, **kw):
    with _lock:
        data = dict(load())
        if email in data:
            data[email] = {**data[email], **kw}
            save(data)


def _notify_new(email):
    u = load().get(email) or {}
    _notify(f"👤 Khách mới đăng ký chờ duyệt:\n{u.get('ten')} · Zalo {u.get('sdt')} · {email}"
            + (f" · Telegram @{u['tg']}" if u.get("tg") else "") + f"\nDuyệt tại: {_base()}/quan-ly")


def verify_token(tok):
    """Khách bấm link trong thư. Trả (ok, thông báo)."""
    if not tok or len(tok) < 20:
        return False, "Link xác nhận không hợp lệ."
    for email, u in load().items():
        if u.get("xm") and hmac.compare_digest(u["xm"], tok):
            if u["tt"] != "xac_minh":
                return True, "Email đã được xác nhận trước đó. Tài khoản đang chờ duyệt hoặc đã dùng được."
            if (u.get("xm_het") or 0) < time.time():
                return False, "Link đã hết hạn. Vào trang gửi lại thư xác nhận."
            _set(email, tt="cho", xm=None, xm_het=None, xac_minh=int(time.time()))
            _notify_new(email)
            return True, f"Đã xác nhận email {email}! Tài khoản đang chờ duyệt, bạn sẽ đăng nhập được ngay khi được duyệt."
    return False, "Link xác nhận không đúng hoặc đã dùng. Vào trang gửi lại thư xác nhận."


def resend(email, ip):
    now = time.time()
    _reg_ip[ip] = [t for t in _reg_ip.get(ip, []) if now - t < 3600]
    if len(_reg_ip[ip]) >= 4:
        return False, "Gửi quá nhiều lần, thử lại sau 1 giờ."
    email = email.strip().lower()
    u = load().get(email)
    _reg_ip[ip].append(now)
    if not u or u["tt"] != "xac_minh":
        # không tiết lộ email nào đã đăng ký
        return True, f"Nếu {email} đang chờ xác nhận, thư mới đã được gửi. Kiểm tra hộp thư và mục Spam."
    tok = secrets.token_urlsafe(24)
    _set(email, xm=tok, xm_het=int(now + 86400))
    sent, e = _send_verify(email, u["ten"], tok)
    return (True, f"Đã gửi lại thư xác nhận tới {email}. Kiểm tra hộp thư và mục Spam.") if sent else (False, "Chưa gửi được thư, thử lại sau ít phút.")


def login(user, pw):
    """user = email. Trả (mã khách | None, lỗi)."""
    user = user.strip().lower()
    u = load().get(user)
    if not u or not _check_pw(pw, u["mk"]):
        return None, "Sai email hoặc mật khẩu."
    st = status_of(u)
    if st == "xac_minh":
        return None, "Bạn chưa xác nhận email. Mở thư PinYin trong hộp thư (xem cả mục Spam) và bấm \"Xác nhận email\"."
    if st == "cho":
        return None, "Tài khoản đang chờ duyệt. Liên hệ Telegram @TuanKom để được duyệt nhanh."
    if st == "khoa":
        return None, "Tài khoản đã bị khóa. Liên hệ Telegram @TuanKom."
    if st == "het":
        return None, f"Tài khoản hết hạn ngày {fmt_date(u['het_han'])}. Liên hệ Telegram @TuanKom để gia hạn."
    with _lock:
        data = dict(load())
        data[user] = {**data[user], "dang_nhap": int(time.time())}
        save(data)
    return data[user]["id"], None


def session_key(uid):
    """Một phần khóa ký cookie: đổi mật khẩu → phiên cũ tự hết."""
    u = by_id(uid)[1]
    return (u["mk"][-16:] if u else None)


def can_trade(uid):
    """Khách được chủ cấp quyền giao dịch qua EA và tài khoản còn hạn."""
    u = by_id(uid)[1]
    return bool(u) and status_of(u) == "ok" and bool(u.get("gd"))


def by_key(key):
    """Mã kết nối EA → (email, bản ghi)."""
    if not key or len(key) < 16:
        return None, None
    for k, u in load().items():
        if u.get("ea_key") and hmac.compare_digest(u["ea_key"], key):
            return k, u
    return None, None


def active(uid):
    u = by_id(uid)[1]
    return bool(u) and status_of(u) == "ok"


# ---------- Quản lý (chủ) ----------

def admin_list():
    out = []
    for k, u in load().items():
        out.append({"user": k, "ten": u["ten"], "sdt": u["sdt"], "email": u.get("email", ""), "tg": u["tg"], "tt": status_of(u),
                    "tao": fmt_date(u["tao"]), "het_han": fmt_date(u.get("het_han")),
                    "con": (max(0, int((u["het_han"] - time.time()) // 86400)) if u.get("het_han") else None),
                    "dang_nhap": datetime.fromtimestamp(u["dang_nhap"], VN).strftime("%d/%m %H:%M") if u.get("dang_nhap") else "-",
                    "gd": bool(u.get("gd")), "id": u.get("id"),
                    "_tao": u["tao"]})
    order = {"cho": 0, "xac_minh": 1, "ok": 2, "het": 3, "khoa": 4}
    out.sort(key=lambda x: (order.get(x["tt"], 9), -x["_tao"]))
    return out


def admin_act(user, act, days=30):
    with _lock:
        data = dict(load())
        u = data.get(user)
        if not u:
            return False, "Không có tài khoản này."
        now = time.time()
        u = dict(u)
        if act == "duyet":
            u.update(tt="ok", duyet=int(now), het_han=int(now + days * 86400))
            msg = f"Đã duyệt {user}, hạn đến {fmt_date(u['het_han'])}."
        elif act == "gia_han":
            base = max(now, u.get("het_han") or now)
            u.update(tt="ok", het_han=int(base + days * 86400))
            msg = f"Đã gia hạn {user} thêm {days} ngày, hạn đến {fmt_date(u['het_han'])}."
        elif act == "khoa":
            u["tt"] = "khoa"
            msg = f"Đã khóa {user}."
        elif act == "mo":
            u["tt"] = "ok"
            if not u.get("het_han") or u["het_han"] < now:
                u["het_han"] = int(now + days * 86400)
            msg = f"Đã mở khóa {user}, hạn đến {fmt_date(u['het_han'])}."
        elif act == "gd_on":
            u["gd"] = True
            u["ea_key"] = u.get("ea_key") or ("PY-" + secrets.token_urlsafe(18))
            msg = f"Đã cho {user} giao dịch qua EA."
        elif act == "gd_off":
            u["gd"] = False
            msg = f"Đã tắt quyền giao dịch của {user}."
        elif act == "key_moi":
            u["ea_key"] = "PY-" + secrets.token_urlsafe(18)
            msg = f"Đã cấp mã kết nối EA mới cho {user} (mã cũ hết dùng)."
        elif act == "xoa":
            data.pop(user)
            save(data)
            return True, f"Đã xóa {user}."
        else:
            return False, "Thao tác không hợp lệ."
        data[user] = u
        save(data)
        return True, msg


def _notify(text):
    try:
        from app.api.ckg_auto import tg_send
        tg_send(text)
    except Exception:
        pass


# ---------- Giao diện ----------

STYLE = """:root{color-scheme:dark;--bg:#0b0e14;--card:#0d1320;--tx:#e8ecf3;--mut:#8b95a7;--bd:#1c2a3d;--acc:#2563eb;--no:#ff5c5c;--ok:#2ecc71;--wn:#f5c542;--h2:#22d3ee}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}
.box{width:100%;max-width:380px;margin:6vh auto;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:22px}
h1{display:flex;align-items:center;gap:10px;font-size:22px;font-weight:800;margin:0 0 4px}h1 svg{width:38px;height:38px;flex:none}
p{margin:0 0 14px;color:var(--mut);font-size:13px}label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px}
input,select{width:100%;font:inherit;padding:10px;border:1px solid var(--bd);border-radius:9px;background:var(--bg);color:var(--tx)}
button,.btn{display:inline-block;text-align:center;font:inherit;font-weight:700;padding:10px 14px;border:0;border-radius:9px;background:var(--acc);color:#fff;cursor:pointer;text-decoration:none}
.full{width:100%;margin-top:14px}.e{color:var(--no);font-size:13px;margin-top:10px}.ok{color:var(--ok)}.no{color:var(--no)}.wn{color:var(--wn)}
a{color:var(--h2)}.alt{text-align:center;margin-top:14px;font-size:13px;color:var(--mut)}"""


def page(title, body, wide=False, logo=""):
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><meta name="robots" content="noindex,nofollow"><style>{STYLE}{'.box{max-width:1180px}' if wide else ''}</style></head>
<body>{body}</body></html>"""


def register_page(logo, msg="", ok=False, f=None):
    f = f or {}
    v = lambda k: html.escape(f.get(k, ""))
    if ok:
        inner = f'<h1>{logo}<span>PinYin</span></h1><p class="ok" style="font-size:15px">✅ {html.escape(msg)}</p><a class="btn full" href="/login">Về trang đăng nhập</a>'
    else:
        inner = f"""<h1>{logo}<span>PinYin</span></h1><p>Đăng ký tài khoản xem phân tích. Tài khoản cần được duyệt trước khi dùng.</p>
<form method="post" action="/dang-ky" autocomplete="on">
<label>Họ tên</label><input name="name" value="{v('name')}" required maxlength="60">
<label>Số điện thoại Zalo <span style="color:var(--no)">*</span></label><input name="phone" value="{v('phone')}" required inputmode="tel" maxlength="15">
<label>Email <span style="color:var(--no)">*</span> <span style="color:var(--mut)">(dùng để đăng nhập)</span></label><input type="email" name="email" value="{v('email')}" required maxlength="80" autocomplete="email">
<label>Telegram (không bắt buộc)</label><input name="tg" value="{v('tg')}" placeholder="@tennguoidung" maxlength="41">
<label>Mật khẩu</label><input type="password" name="pw" required minlength="6" autocomplete="new-password">
<label>Nhập lại mật khẩu</label><input type="password" name="pw2" required minlength="6" autocomplete="new-password">
<button class="full">Đăng ký</button>{f'<div class="e">{html.escape(msg)}</div>' if msg else ''}</form>
<div class="alt">Đã có tài khoản? <a href="/login">Đăng nhập</a></div>"""
    return page("PinYin · Đăng ký", f'<div class="box">{inner}</div>')


def msg_page(logo, title, msg, ok=True, extra=""):
    color = "ok" if ok else "no"
    inner = (f'<h1>{logo}<span>PinYin</span></h1><p style="font-size:15px" class="{color}">{"✅" if ok else "⚠️"} {html.escape(msg)}</p>'
             f'{extra}<a class="btn full" href="/login">Về trang đăng nhập</a>')
    return page("PinYin · " + title, f'<div class="box">{inner}</div>')


def resend_page(logo, msg="", ok=False):
    inner = f"""<h1>{logo}<span>PinYin</span></h1><p>Chưa nhận được thư xác nhận? Nhập email đã đăng ký để gửi lại.</p>
<form method="post" action="/gui-lai-xac-minh"><label>Email</label><input type="email" name="email" required maxlength="80">
<button class="full">Gửi lại thư xác nhận</button>{f'<div class="{"ok" if ok else "e"}" style="margin-top:10px;font-size:13px">{html.escape(msg)}</div>' if msg else ''}</form>
<div class="alt"><a href="/login">Đăng nhập</a> · <a href="/dang-ky">Đăng ký</a></div>"""
    return page("PinYin · Gửi lại thư xác nhận", f'<div class="box">{inner}</div>')


ADMIN_JS = r"""
const $=i=>document.getElementById(i);
const TT={xac_minh:['📧 Chưa xác nhận email','m'],cho:['⏳ Chờ duyệt','wn'],ok:['✅ Đang dùng','ok'],het:['⌛ Hết hạn','no'],khoa:['⛔ Bị khóa','no']};
async function load(){const r=await fetch('/quan-ly/api/list',{cache:'no-store'});const j=await r.json();if(!j.ok){$('tb').innerHTML='<tr><td>'+j.loi+'</td></tr>';return}
 const L=j.users;$('sum').textContent=`${L.length} tài khoản · ${L.filter(x=>x.tt=='cho').length} chờ duyệt · ${L.filter(x=>x.tt=='ok').length} đang dùng`;
 $('tb').innerHTML=L.length?L.map(u=>{const t=TT[u.tt]||[u.tt,''],e=s=>String(s).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  const d=`<select id="d_${u.user}"><option value="7">7 ngày</option><option value="30" selected>30 ngày</option><option value="90">90 ngày</option><option value="365">365 ngày</option></select>`;
  let a=(u.tt=='cho'||u.tt=='xac_minh')?`${d}<button onclick="act('${u.user}','duyet')">Duyệt</button><button class="red" onclick="act('${u.user}','xoa')">Từ chối</button>`:
   u.tt=='khoa'?`${d}<button onclick="act('${u.user}','mo')">Mở khóa</button><button class="red" onclick="act('${u.user}','xoa')">Xóa</button>`:
   `${d}<button onclick="act('${u.user}','gia_han')">Gia hạn</button><button class="red" onclick="act('${u.user}','khoa')">Khóa</button>`;
  return `<tr><td><b>${e(u.ten)}</b><br><a class="m" href="mailto:${e(u.user)}">${e(u.user)}</a></td><td><a href="https://zalo.me/${e(u.sdt)}" target="_blank" rel="noopener">Zalo ${e(u.sdt)}</a>${u.tg?`<br><a href="https://t.me/${e(u.tg)}" target="_blank" rel="noopener">@${e(u.tg)}</a>`:''}</td>
   <td class="${t[1]}"><b>${t[0]}</b></td><td>${u.het_han}${u.con!=null&&u.tt=='ok'?'<br><span class="m">còn '+u.con+' ngày</span>':''}</td><td class="m">ĐK ${u.tao}<br>Vào ${u.dang_nhap}</td><td class="act">${u.gd?`<b class="ok">✅ Được giao dịch</b><br><span class="m">${u.ea?(u.ea.on?'🟢 EA online · #'+e(u.ea.login)+' · '+e(u.ea.server)+(u.ea.demo?' (demo)':' (THẬT)')+' · EA '+e(u.ea.ver||'?')+(u.ea.old?' <b class="wn">(cũ)</b>':''):'⚪ EA offline'+(u.ea.login?' · #'+e(u.ea.login):'')):'⚪ chưa nối EA'}</span><br><button class="red" onclick="act('${u.user}','gd_off')">Tắt giao dịch</button>`:(u.tt=='ok'?`<button onclick="act('${u.user}','gd_on')">Cho giao dịch</button>`:'<span class="m">-</span>')}</td><td class="act">${a}</td></tr>`}).join(''):'<tr><td colspan="6" class="m">Chưa có khách nào đăng ký. Gửi link https://pinyin.id.vn/dang-ky cho khách.</td></tr>'}
async function act(user,a){const days=+($('d_'+user)||{}).value||30;
 if(a=='xoa'&&!confirm('Xóa tài khoản '+user+'?'))return;if(a=='khoa'&&!confirm('Khóa tài khoản '+user+'?'))return;
 const r=await fetch('/quan-ly/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user,act:a,days})});const j=await r.json();
 $('msg').innerHTML=j.ok?'<span class="ok">✅ '+j.msg+'</span>':'<span class="no">⚠️ '+j.loi+'</span>';load()}
function copyLink(){navigator.clipboard.writeText('https://pinyin.id.vn/dang-ky').then(()=>{$('msg').innerHTML='<span class="ok">✅ Đã chép link đăng ký</span>'})}
async function mailLoad(){const j=await (await fetch('/quan-ly/api/mail',{cache:'no-store'})).json();
 $('mailbox').innerHTML=j.on?`📧 <b class="ok">Xác nhận email: ĐANG BẬT</b> · gửi từ ${j.from.replace(/</g,'&lt;')} · khách phải bấm link trong thư mới vào danh sách chờ duyệt. <a href="#" onclick="$('mk').style.display='flex';return false">Đổi khóa</a>`+mailForm(1):
  `📧 <b class="wn">Xác nhận email: CHƯA BẬT</b> · khách đăng ký vào thẳng danh sách chờ duyệt (chỉ lọc tên miền email). Dán khóa API Resend để bật:`+mailForm(0)}
function mailForm(h){return `<div id="mk" style="display:${h?'none':'flex'};gap:8px;margin-top:8px;flex-wrap:wrap"><input id="mkey" placeholder="Khóa API Resend (re_...)" style="flex:2;min-width:220px"><input id="mto" type="email" placeholder="Email để nhận thư thử" style="flex:1;min-width:180px"><button onclick="mailSave()">Lưu &amp; gửi thử</button></div>`}
async function mailSave(){const key=$('mkey').value.trim(),to=$('mto').value.trim();if(!key||!to){alert('Nhập khóa API và email nhận thư thử.');return}
 $('msg').textContent='Đang gửi thư thử...';const j=await (await fetch('/quan-ly/api/mail',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,to})})).json();
 $('msg').innerHTML=j.ok?'<span class="ok">✅ Đã gửi thư thử tới '+to+' và bật xác nhận email.</span>':'<span class="no">⚠️ '+String(j.loi).replace(/</g,'&lt;')+'</span>';mailLoad()}
load();mailLoad();setInterval(load,20000);
"""


def admin_page(logo):
    body = f"""<div class="box"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
<h1>{logo}<span>Quản lý khách hàng</span></h1><div style="display:flex;gap:8px;flex-wrap:wrap"><button onclick="copyLink()">📋 Chép link đăng ký</button><a class="btn" style="background:#1c2a3d" href="/ckg/">← Về trang phân tích</a></div></div>
<div id="mailbox" style="border:1px solid var(--bd);border-radius:10px;padding:10px 12px;margin:10px 0;font-size:13px"></div><p id="sum" style="margin-top:6px"></p><div id="msg" style="min-height:22px;font-size:14px"></div>
<div style="overflow-x:auto"><table><thead><tr><th>Khách</th><th>Liên hệ</th><th>Trạng thái</th><th>Hạn dùng</th><th>Hoạt động</th><th>Giao dịch EA</th><th></th></tr></thead><tbody id="tb"><tr><td>Đang tải…</td></tr></tbody></table></div>
<p style="margin-top:14px">Khách đăng ký tại <b>https://pinyin.id.vn/dang-ky</b> → chờ duyệt ở đây (Telegram báo khi có người đăng ký). Khách chỉ có quyền XEM: không vào lệnh, không thấy tài khoản MT5, không thấy bot.</p></div>
<style>table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:9px 8px;border-top:1px solid var(--bd);vertical-align:middle}}th{{color:var(--h2);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.m{{color:var(--mut);font-size:12px}}.act{{white-space:nowrap}}.act select{{width:auto;padding:5px;margin-right:6px}}.act button{{padding:6px 10px;margin-right:4px;font-size:12px}}.red{{background:transparent;border:1px solid var(--no);color:var(--no)}}
@media(max-width:700px){{thead{{display:none}}tr{{display:block;border-top:1px solid var(--bd);padding:8px 0}}td{{display:block;border:0;padding:3px 0}}}}</style>
<script>{ADMIN_JS}</script>"""
    return page("PinYin · Quản lý khách hàng", body, wide=True)
