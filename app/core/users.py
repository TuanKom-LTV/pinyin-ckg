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


DEF_ACC = 1            # mặc định mỗi khách 1 tài khoản MT5, muốn thêm phải xin (chủ duyệt, có phí) (mỗi tài khoản 1 mã kết nối + 1 bot riêng)
HARD_ACC = 10          # chủ mở thêm (có phí) tối đa tới mức này


def acc_limit_of(u):
    return max(1, min(HARD_ACC, int(u.get("max_acc") or DEF_ACC)))


def acc_limit(uid):
    u = by_id(uid)[1]
    return acc_limit_of(u) if u else DEF_ACC


def split_uid(uid):
    """'abc~2' → ('abc', 2) (tài khoản MT5 thứ 2 của khách abc); 'abc' → ('abc', 1)."""
    if isinstance(uid, str) and "~" in uid:
        b, n = uid.rsplit("~", 1)
        if n.isdigit():
            return b, int(n)
    return uid, 1


def _accs(u):
    out = [{"n": 1, "uid": u["id"], "key": u.get("ea_key"), "name": (u.get("acc_names") or {}).get("1") or "Tài khoản 1"}]
    for n, x in sorted((u.get("ea_keys") or {}).items(), key=lambda kv: int(kv[0])):
        out.append({"n": int(n), "uid": f"{u['id']}~{n}", "key": x["key"], "name": x.get("name") or f"Tài khoản {n}"})
    return out


def accounts(uid):
    """Các tài khoản MT5 của khách: [{n, uid, key, name}]."""
    u = by_id(uid)[1]
    return _accs(u) if u else []


def key_uid(key):
    """Mã kết nối EA → (email, bản ghi, uid của đúng tài khoản MT5 đó)."""
    if not key or len(key) < 16:
        return None, None, None
    for k, u in load().items():
        if u.get("ea_key") and hmac.compare_digest(u["ea_key"], key):
            return k, u, u["id"]
        for n, x in (u.get("ea_keys") or {}).items():
            if x.get("key") and hmac.compare_digest(x["key"], key):
                return k, u, f"{u['id']}~{n}"
    return None, None, None


def acc_act(uid, act, n=0, name=""):
    """Khách tự thêm / đổi tên / đổi mã / xóa tài khoản MT5 của mình. Trả (ok, thông báo, n)."""
    base = split_uid(uid)[0]
    with _lock:
        data = dict(load())
        email = next((k for k, x in data.items() if x.get("id") == base), None)
        if not email:
            return False, "Không có tài khoản.", 0
        u = dict(data[email])
        keys = {k: dict(v) for k, v in (u.get("ea_keys") or {}).items()}
        names = dict(u.get("acc_names") or {})
        name = (name or "").strip()[:30]
        lim = acc_limit_of(u)
        if act == "req":
            if time.time() - (u.get("acc_req") or 0) < 3600:
                return True, "Đã gửi yêu cầu rồi, chủ PinYin sẽ liên hệ bạn. Cần gấp: Telegram @TuanKom.", 0
            u["acc_req"] = int(time.time())
            data[email] = u
            save(data)
            try:
                from app.api.ckg_auto import tg_send
                tg_send(f"📩 Khách {u.get('ten') or email} ({email}, Zalo {u.get('sdt') or '-'}) xin THÊM tài khoản MT5 "
                        f"(đang {1 + len(keys)}/{lim}). Nếu khách nhắn Telegram thì mở thêm tại https://pinyin.id.vn/quan-ly (nút ＋ MT5).")
            except Exception:
                pass
            return True, "Đã báo chủ PinYin. Nhắn Telegram @TuanKom để được báo phí và mở thêm tài khoản.", 0
        if act == "add":
            if 1 + len(keys) >= lim:
                return False, (f"Gói của bạn đang dùng tối đa {lim} tài khoản MT5. Để mở thêm tài khoản (có phí), "
                               "bấm Xin thêm tài khoản để nhắn Telegram @TuanKom."), 0
            n = next(i for i in range(2, HARD_ACC + 1) if str(i) not in keys)
            keys[str(n)] = {"key": "PY-" + secrets.token_urlsafe(18), "name": name or f"Tài khoản {n}"}
            msg = f"Đã thêm {keys[str(n)]['name']}. Gắn EA vào MT5 đó với mã kết nối mới."
        elif act == "name":
            if not name:
                return False, "Nhập tên.", 0
            if n == 1:
                names["1"] = name
            elif str(n) in keys:
                keys[str(n)]["name"] = name
            else:
                return False, "Không có tài khoản này.", 0
            msg = "Đã đổi tên."
        elif act == "key":
            if n == 1:
                u["ea_key"] = "PY-" + secrets.token_urlsafe(18)
            elif str(n) in keys:
                keys[str(n)]["key"] = "PY-" + secrets.token_urlsafe(18)
            else:
                return False, "Không có tài khoản này.", 0
            msg = "Đã đổi mã kết nối. EA đang gắn phải dán mã mới."
        elif act == "del":
            if n == 1 or str(n) not in keys:
                return False, "Không xóa được tài khoản chính.", 0
            keys.pop(str(n))
            msg = "Đã xóa tài khoản. Lệnh đang mở trên MT5 đó giữ nguyên (SL/TP vẫn chạy)."
        else:
            return False, "Thao tác không hợp lệ.", 0
        u["ea_keys"], u["acc_names"] = keys, names
        data[email] = u
        save(data)
        return True, msg, n


def by_id(uid):
    """Mã khách → (email, bản ghi) hoặc (None, None). Nhận cả mã tài khoản MT5 phụ (abc~2)."""
    uid = split_uid(uid)[0]
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


# ---------- Quên mật khẩu ----------

_fp_ip: dict[str, list[float]] = {}
_fp_mail: dict[str, list[float]] = {}
RESET_SEC = 3600                       # link đặt lại mật khẩu dùng được 1 giờ


def _rate(bucket, key, limit, now):
    bucket[key] = [t for t in bucket.get(key, []) if now - t < 3600]
    if len(bucket[key]) >= limit:
        return False
    bucket[key].append(now)
    return True


def forgot(email, ip):
    """Khách quên mật khẩu: gửi link đặt lại tới email. Không tiết lộ email nào đã đăng ký. Trả (ok, thông báo)."""
    now = time.time()
    email = (email or "").strip().lower()[:80]
    if not EMAIL_RE.match(email):
        return False, "Email không hợp lệ."
    if not mailer.configured():
        return False, "Máy chủ chưa bật gửi email. Nhắn Telegram @TuanKom hoặc Zalo để được cấp mật khẩu mới."
    if not _rate(_fp_ip, ip, 5, now):
        return False, "Gửi quá nhiều lần, thử lại sau 1 giờ."
    generic = (f"Nếu {email} đã đăng ký, thư đặt lại mật khẩu đã được gửi. Mở hộp thư (xem cả mục Spam / Quảng cáo) "
               "và bấm \"Đặt lại mật khẩu\". Link có hiệu lực 1 giờ.")
    u = load().get(email)
    if not u or not _rate(_fp_mail, email, 3, now):
        return True, generic
    tok = secrets.token_urlsafe(24)
    _set(email, rs=hashlib.sha256(tok.encode()).hexdigest(), rs_het=int(now + RESET_SEC))
    link = f"{_base()}/dat-lai-mat-khau?t={tok}"
    sent, e = mailer.send(email, "Đặt lại mật khẩu PinYin CKG", mailer.reset_html(html.escape(u.get("ten") or ""), link))
    if not sent:
        _notify(f"⚠️ Không gửi được thư đặt lại mật khẩu tới {email}: {e}")
        return False, "Chưa gửi được thư, thử lại sau ít phút hoặc nhắn Telegram @TuanKom."
    return True, generic


def _by_reset(tok):
    if not tok or len(tok) < 20:
        return None, None
    h = hashlib.sha256(tok.encode()).hexdigest()
    for email, u in load().items():
        if u.get("rs") and hmac.compare_digest(u["rs"], h):
            return email, u
    return None, None


def reset_check(tok):
    """Link còn dùng được không. Trả (ok, thông báo lỗi)."""
    email, u = _by_reset(tok)
    if not u:
        return False, "Link đặt lại mật khẩu không đúng hoặc đã dùng. Gửi lại yêu cầu mới."
    if (u.get("rs_het") or 0) < time.time():
        return False, "Link đã hết hạn (quá 1 giờ). Gửi lại yêu cầu mới."
    return True, email


def reset_pw(tok, pw, pw2):
    ok, email = reset_check(tok)
    if not ok:
        return False, email
    if len(pw) < 6:
        return False, "Mật khẩu tối thiểu 6 ký tự."
    if pw != pw2:
        return False, "Hai lần nhập mật khẩu không khớp."
    u = load().get(email) or {}
    kw = {"mk": _hash(pw), "rs": None, "rs_het": None, "doi_mk": int(time.time())}
    if u.get("tt") == "xac_minh":                 # bấm được link trong thư = email là thật
        kw.update(tt="cho", xm=None, xm_het=None, xac_minh=int(time.time()))
    _set(email, **kw)                              # đổi mật khẩu → mọi phiên đăng nhập cũ tự hết
    _notify(f"🔑 Khách {email} vừa tự đặt lại mật khẩu qua email.")
    if kw.get("tt") == "cho":
        _notify_new(email)
    return True, "Đã đặt mật khẩu mới. Đăng nhập lại bằng email và mật khẩu mới."


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


def can_bot(uid):
    """Khách được chủ duyệt dùng bot tự động (cần thêm quyền giao dịch qua EA)."""
    u = by_id(uid)[1]
    return bool(u) and status_of(u) == "ok" and bool(u.get("gd")) and bool(u.get("bot"))


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
                    "gd": bool(u.get("gd")), "bot": bool(u.get("bot")), "id": u.get("id"),
                    "nacc": 1 + len(u.get("ea_keys") or {}), "max_acc": acc_limit_of(u),
                    "acc_req": bool(u.get("acc_req")) and (u.get("acc_req") or 0) > (u.get("acc_ok") or 0),
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
            u["bot"] = False
            msg = f"Đã tắt quyền giao dịch (và bot) của {user}."
        elif act == "bot_on":
            if not u.get("gd"):
                return False, "Cần cho giao dịch trước khi cho dùng bot."
            u["bot"] = True
            msg = f"Đã cho {user} dùng bot tự động."
        elif act in ("acc_up", "acc_down"):
            lim, n = acc_limit_of(u), 1 + len(u.get("ea_keys") or {})
            if act == "acc_up":
                if lim >= HARD_ACC:
                    return False, f"Tối đa {HARD_ACC} tài khoản MT5."
                u["max_acc"], u["acc_ok"] = lim + 1, int(now)
            else:
                if lim - 1 < max(1, n):
                    return False, f"Khách đang dùng {n} tài khoản MT5, phải xóa bớt trước khi giảm."
                u["max_acc"] = lim - 1
            msg = f"{user}: được dùng {u['max_acc']} tài khoản MT5."
        elif act == "bot_off":
            u["bot"] = False
            msg = f"Đã tắt bot tự động của {user} (bot dừng, lệnh đang mở giữ SL trên MT5)."
        elif act == "mk_moi":
            tmp = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(8))
            u.update(mk=_hash(tmp), rs=None, rs_het=None, doi_mk=int(now))
            msg = f"Mật khẩu mới của {user}: {tmp} — gửi cho khách qua Zalo / Telegram (phiên đăng nhập cũ đã hết). Khách nên vào Quên mật khẩu để tự đổi sau."
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
<div style="font-size:12px;color:var(--mut);margin-top:10px">Bấm Đăng ký là bạn đã đọc và chấp nhận <a href="/mien-tru-trach-nhiem" target="_blank" style="color:#22d3ee">Tuyên bố miễn trừ trách nhiệm về rủi ro</a>.</div>
<button class="full">Đăng ký</button>{f'<div class="e">{html.escape(msg)}</div>' if msg else ''}</form>
<div class="alt">Đã có tài khoản? <a href="/login">Đăng nhập</a></div>"""
    return page("PinYin · Đăng ký", f'<div class="box">{inner}</div>')


def msg_page(logo, title, msg, ok=True, extra=""):
    color = "ok" if ok else "no"
    inner = (f'<h1>{logo}<span>PinYin</span></h1><p style="font-size:15px" class="{color}">{"✅" if ok else "⚠️"} {html.escape(msg)}</p>'
             f'{extra}<a class="btn full" href="/login">Về trang đăng nhập</a>')
    return page("PinYin · " + title, f'<div class="box">{inner}</div>')


def forgot_page(logo, msg="", ok=False):
    inner = f"""<h1>{logo}<span>PinYin</span></h1><p>Quên mật khẩu? Nhập email đã đăng ký, PinYin gửi link để bạn đặt mật khẩu mới.</p>
<form method="post" action="/quen-mat-khau"><label>Email</label><input type="email" name="email" required maxlength="80" autocomplete="email">
<button class="full">Gửi link đặt lại mật khẩu</button>{f'<div class="{"ok" if ok else "e"}" style="margin-top:10px;font-size:13px">{html.escape(msg)}</div>' if msg else ''}</form>
<div class="alt">Không nhận được thư? Nhắn <a href="https://t.me/TuanKom" target="_blank" rel="noopener">Telegram @TuanKom</a> để được cấp mật khẩu mới.<br><a href="/login">Đăng nhập</a> · <a href="/dang-ky">Đăng ký</a></div>"""
    return page("PinYin · Quên mật khẩu", f'<div class="box">{inner}</div>')


def reset_page(logo, tok, msg="", email=""):
    inner = f"""<h1>{logo}<span>PinYin</span></h1><p>Đặt mật khẩu mới cho <b>{html.escape(email)}</b>.</p>
<form method="post" action="/dat-lai-mat-khau"><input type="hidden" name="t" value="{html.escape(tok)}">
<input type="email" name="user" value="{html.escape(email)}" autocomplete="username" hidden>
<label>Mật khẩu mới</label><input type="password" name="pw" required minlength="6" autocomplete="new-password" autofocus>
<label>Nhập lại mật khẩu mới</label><input type="password" name="pw2" required minlength="6" autocomplete="new-password">
<button class="full">Lưu mật khẩu mới</button>{f'<div class="e">{html.escape(msg)}</div>' if msg else ''}</form>
<div class="alt"><a href="/login">Đăng nhập</a></div>"""
    return page("PinYin · Đặt lại mật khẩu", f'<div class="box">{inner}</div>')


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
  a+=`<br><button onclick="act('${u.user}','mk_moi')" title="Khách quên mật khẩu, không nhận được thư: cấp mật khẩu tạm rồi gửi khách qua Zalo / Telegram">🔑 Cấp MK mới</button>`;
  return `<tr><td><b>${e(u.ten)}</b><br><a class="m" href="mailto:${e(u.user)}">${e(u.user)}</a></td><td><a href="https://zalo.me/${e(u.sdt)}" target="_blank" rel="noopener">Zalo ${e(u.sdt)}</a>${u.tg?`<br><a href="https://t.me/${e(u.tg)}" target="_blank" rel="noopener">@${e(u.tg)}</a>`:''}</td>
   <td class="${t[1]}"><b>${t[0]}</b></td><td>${u.het_han}${u.con!=null&&u.tt=='ok'?'<br><span class="m">còn '+u.con+' ngày</span>':''}</td><td class="m">ĐK ${u.tao}<br>Vào ${u.dang_nhap}</td><td class="act">${u.gd?`<b class="ok">✅ Được giao dịch</b><br><span class="m">${u.ea?(u.ea.on?'🟢 EA online · #'+e(u.ea.login)+' · '+e(u.ea.server)+(u.ea.demo?' (demo)':' (THẬT)')+' · EA '+e(u.ea.ver||'?')+(u.ea.old?' <b class="wn">(cũ)</b>':''):'⚪ EA offline'+(u.ea.login?' · #'+e(u.ea.login):'')):'⚪ chưa nối EA'}</span><br>${u.bot?`<b class="ok">🤖 Được dùng bot</b> <span class="m">${u.cbot?(u.cbot.on?'· đang BẬT '+e(u.cbot.tf)+(u.cbot.camp?' · '+e(u.cbot.camp):''):'· đang tắt'):''}</span><br><button class="red" onclick="act('${u.user}','bot_off')">Tắt bot</button> `:`<button onclick="act('${u.user}','bot_on')">🤖 Cho dùng bot</button> `}<br><span class="m">🖥 MT5: <b>${u.nacc}/${u.max_acc}</b> tài khoản</span> <button onclick="act('${u.user}','acc_up')" title="Mở thêm 1 tài khoản MT5 (có phí)">＋ MT5</button><button class="red" onclick="act('${u.user}','acc_down')" title="Giảm 1 tài khoản">－</button>${u.acc_req?' <b class="wn">📩 khách xin thêm</b>':''}<br><button class="red" onclick="act('${u.user}','gd_off')">Tắt giao dịch</button>`:(u.tt=='ok'?`<button onclick="act('${u.user}','gd_on')">Cho giao dịch</button>`:'<span class="m">-</span>')}</td><td class="act">${a}</td></tr>`}).join(''):'<tr><td colspan="6" class="m">Chưa có khách nào đăng ký. Gửi link https://pinyin.id.vn/dang-ky cho khách.</td></tr>'}
async function act(user,a){const days=+($('d_'+user)||{}).value||30;
 if(a=='xoa'&&!confirm('Xóa tài khoản '+user+'?'))return;if(a=='khoa'&&!confirm('Khóa tài khoản '+user+'?'))return;if(a=='mk_moi'&&!confirm('Cấp mật khẩu mới cho '+user+'? Mật khẩu cũ hết dùng, khách bị đăng xuất.'))return;
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
async function tai(){let j;try{j=await (await fetch('/quan-ly/api/tai',{cache:'no-store'})).json()}catch(e){return}if(!j||!j.ok)return;
 const e=x=>String(x??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),L=j.loops||{},lp=(k,n)=>L[k]==null?'':`${n} <b class="${L[k]>30?'no':L[k]>8?'wn':'ok'}">${L[k]<10?'●':L[k]+'s'}</b>`;
 const up=s=>s<3600?Math.floor(s/60)+' phút':s<86400?Math.floor(s/3600)+' giờ':Math.floor(s/86400)+' ngày';
 const q=(j.targets||[]).filter(t=>t.on||t.queue).map(t=>`${e(t.name)} <span class="m">(${t.kind})</span> ${t.online?'🟢':'⚪'}${t.queue?` <b class="wn">⏳${t.queue} lệnh chờ ${t.oldest}s</b>`:''}`).join(' · ')||'<span class="m">không có</span>';
 const b=j.backup||{};
 $('loadbox').innerHTML=`<b>🖥 Tình trạng máy chủ</b> · chạy ${up(j.uptime_s)} · MT5 ${j.mt5.ok?'<b class="ok">kết nối sàn</b>'+(j.mt5.ping_ms?' ('+j.mt5.ping_ms+' ms)':''):'<b class="no">MẤT KẾT NỐI '+up(j.mt5.since_s)+'</b> '+e(j.mt5.err)}
 · vòng chạy: ${[lp('bot','bot'),lp('cbot','bot khách'),lp('copy','copy'),lp('guard','lá chắn')].filter(Boolean).join(' · ')}
 · EA online <b>${j.ea_online}</b>/${j.ea_total} · đệm API ${j.cache.rate}%<br>Tài khoản nhận lệnh: ${q}
 <br>💾 Sao lưu: ${b.last?'lần cuối <b>'+e(b.last)+'</b> ('+e(b.file)+', '+b.size_kb+' KB)':'<b class="wn">chưa có</b>'} → ${(b.dirs||[]).map(e).join(' · ')}${b.err?' <b class="no">'+e(b.err)+'</b>':''}
 <button onclick="bk()" style="margin-left:6px;padding:4px 10px;font-size:12px">Sao lưu ngay</button> · UptimeRobot: <code>https://pinyin.id.vn/health</code>`}
async function bk(){const r=await (await fetch('/quan-ly/api/backup',{method:'POST'})).json();alert(r.ok?r.msg:'Lỗi: '+(r.loi||''));tai()}
load();mailLoad();tai();setInterval(load,20000);setInterval(tai,5000);
"""


def admin_page(logo):
    body = f"""<div class="box"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
<h1>{logo}<span>Quản lý khách hàng</span></h1><div style="display:flex;gap:8px;flex-wrap:wrap"><button onclick="copyLink()">📋 Chép link đăng ký</button><a class="btn" style="background:#1c2a3d" href="/ckg/">← Về trang phân tích</a></div></div>
<div id="mailbox" style="border:1px solid var(--bd);border-radius:10px;padding:10px 12px;margin:10px 0;font-size:13px"></div>
<div id="loadbox" style="border:1px solid var(--bd);border-radius:10px;padding:10px 12px;margin:10px 0;font-size:13px">Đang tải tình trạng máy chủ…</div><p id="sum" style="margin-top:6px"></p><div id="msg" style="min-height:22px;font-size:14px"></div>
<div style="overflow-x:auto"><table><thead><tr><th>Khách</th><th>Liên hệ</th><th>Trạng thái</th><th>Hạn dùng</th><th>Hoạt động</th><th>Giao dịch EA</th><th></th></tr></thead><tbody id="tb"><tr><td>Đang tải…</td></tr></tbody></table></div>
<p style="margin-top:14px">Khách đăng ký tại <b>https://pinyin.id.vn/dang-ky</b> → chờ duyệt ở đây (Telegram báo khi có người đăng ký). Khách chỉ có quyền XEM: không vào lệnh, không thấy tài khoản MT5, không thấy bot.</p></div>
<style>table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:9px 8px;border-top:1px solid var(--bd);vertical-align:middle}}th{{color:var(--h2);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.m{{color:var(--mut);font-size:12px}}.act{{white-space:nowrap}}.act select{{width:auto;padding:5px;margin-right:6px}}.act button{{padding:6px 10px;margin-right:4px;font-size:12px}}.red{{background:transparent;border:1px solid var(--no);color:var(--no)}}
@media(max-width:700px){{thead{{display:none}}tr{{display:block;border-top:1px solid var(--bd);padding:8px 0}}td{{display:block;border:0;padding:3px 0}}}}</style>
<script>{ADMIN_JS}</script>"""
    return page("PinYin · Quản lý khách hàng", body, wide=True)

DISCLAIMER_URL = "/mien-tru-trach-nhiem"
DISCLAIMER = [
    (None, "Giao dịch ngoại hối và CFD ký quỹ tiềm ẩn rủi ro cao và có thể không phù hợp với tất cả các nhà đầu tư. "
           "Đòn bẩy có thể gây bất lợi cũng như có lợi cho bạn, và bạn có thể mất nhiều hơn số tiền gửi ban đầu với một số nhà cung cấp. "
           "Nội dung trên PinYin CKG chỉ dành cho mục đích giáo dục, tham khảo và hỗ trợ, không phải là lời khuyên đầu tư."),
    ("Nguy cơ mất mát cao", "Giao dịch ngoại hối và hợp đồng chênh lệch (CFD) là những công cụ phức tạp được giao dịch ký quỹ. "
           "Một tỷ lệ lớn các tài khoản nhà đầu tư cá nhân thua lỗ khi giao dịch CFD. Bạn nên cân nhắc xem mình có hiểu cách thức hoạt động "
           "của các sản phẩm này hay không và liệu mình có đủ khả năng chấp nhận rủi ro cao khi mất tiền hay không."),
    ("Đây không phải là lời khuyên đầu tư", "Tất cả nội dung trên PinYin CKG — bao gồm các bài viết, công cụ tính toán, dữ liệu thị trường "
           "và đánh giá về các nhà môi giới — chỉ được cung cấp cho mục đích giáo dục và thông tin chung. Nội dung này không xem xét mục tiêu, "
           "tình hình tài chính hoặc nhu cầu của bạn, và không phải là lời khuyên mua hoặc bán bất kỳ công cụ tài chính nào. "
           "Hãy cân nhắc tìm kiếm lời khuyên từ một cố vấn tài chính độc lập, có giấy phép phù hợp."),
    ("Độ chính xác của thông tin", "Chúng tôi nỗ lực hết sức để giữ cho nội dung của mình chính xác và cập nhật, nhưng chúng tôi không đảm bảo "
           "rằng nội dung đó đầy đủ, chính xác hoặc hiện hành. Kết quả tính toán chỉ là ước tính, không bao gồm các chi phí như chênh lệch giá, "
           "hoa hồng, phí hoán đổi và trượt giá. Dữ liệu thị trường được cung cấp bởi bên thứ ba và có thể bị chậm trễ."),
    ("Thành tích trong quá khứ", "Kết quả trong quá khứ không phải là chỉ báo đáng tin cậy về kết quả trong tương lai. Mọi ví dụ chỉ mang tính minh họa."),
    ("Các nhà môi giới bên thứ ba", "Các nhà môi giới được đề cập trên trang web này là các công ty độc lập. Hãy kiểm tra xem bất kỳ nhà môi giới nào "
           "có được cơ quan quản lý tại quốc gia của bạn cấp phép hay không, và đọc kỹ các điều khoản và điều kiện rủi ro của họ trước khi mở tài khoản. "
           "Các sản phẩm và đòn bẩy dành cho bạn phụ thuộc vào quốc gia cư trú của bạn."),
]


def disclaimer_page(logo):
    parts = "".join((f'<h2 style="font-size:16px;color:var(--h2);margin:18px 0 6px">{html.escape(t)}</h2>' if t else "")
                    + f'<p style="margin:0 0 8px;color:var(--tx)">{html.escape(p)}</p>' for t, p in DISCLAIMER)
    inner = f"""<h1>{logo}<span>PinYin CKG</span></h1>
<div style="font-size:20px;font-weight:800;margin:10px 0 2px">⚠️ Tuyên bố miễn trừ trách nhiệm về rủi ro</div>
<div style="font-size:12px;color:var(--mut);margin-bottom:12px">Cập nhật lần cuối ngày 2 tháng 10 năm 2026</div>
{parts}<p style="margin-top:18px;font-weight:700">PinYin CKG</p>
<div class="alt"><a href="/">Về trang chính</a> · <a href="/login">Đăng nhập</a> · <a href="/dang-ky">Đăng ký</a></div>"""
    return page("PinYin · Miễn trừ trách nhiệm", f'<div class="box" style="max-width:760px">{inner}</div>')
