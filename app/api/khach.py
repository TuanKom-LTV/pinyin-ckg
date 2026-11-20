"""Đăng ký khách (/dang-ky), quản lý khách (/quan-ly, chỉ chủ) và thông tin phiên (/api/me)."""

from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import os

from app.core import mailer, users
from app.core.auth import LOGO_K, _ip

router = APIRouter(tags=["PinYin · khách hàng"])
NOSTORE = {"Cache-Control": "no-store"}


def _owner(request: Request):
    return getattr(request.state, "role", None) == "chu"


@router.get("/dang-ky", include_in_schema=False)
def reg_form():
    return HTMLResponse(users.register_page(LOGO_K), headers=NOSTORE)


@router.post("/dang-ky", include_in_schema=False)
async def reg_submit(request: Request):
    f = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "ignore")).items()}
    ok, msg = users.register(f, _ip(request))
    return HTMLResponse(users.register_page(LOGO_K, msg, ok, None if ok else f), headers=NOSTORE)


@router.get("/mien-tru-trach-nhiem", include_in_schema=False)
def disclaimer():
    return HTMLResponse(users.disclaimer_page(LOGO_K))


@router.get("/xac-minh", include_in_schema=False)
def verify(t: str = ""):
    ok, msg = users.verify_token(t)
    extra = "" if ok else '<a class="btn full" style="background:#1c2a3d" href="/gui-lai-xac-minh">Gửi lại thư xác nhận</a>'
    return HTMLResponse(users.msg_page(LOGO_K, "Xác nhận email", msg, ok, extra), headers=NOSTORE)


@router.get("/gui-lai-xac-minh", include_in_schema=False)
def resend_form():
    return HTMLResponse(users.resend_page(LOGO_K), headers=NOSTORE)


@router.post("/gui-lai-xac-minh", include_in_schema=False)
async def resend_submit(request: Request):
    f = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "ignore")).items()}
    ok, msg = users.resend(f.get("email", ""), _ip(request))
    return HTMLResponse(users.resend_page(LOGO_K, msg, ok), headers=NOSTORE)


@router.get("/quen-mat-khau", include_in_schema=False)
def forgot_form():
    return HTMLResponse(users.forgot_page(LOGO_K), headers=NOSTORE)


@router.post("/quen-mat-khau", include_in_schema=False)
async def forgot_submit(request: Request):
    f = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "ignore")).items()}
    ok, msg = users.forgot(f.get("email", ""), _ip(request))
    return HTMLResponse(users.forgot_page(LOGO_K, msg, ok), headers=NOSTORE)


@router.get("/quen-tai-khoan", include_in_schema=False)
def find_form():
    return HTMLResponse(users.find_page(LOGO_K), headers=NOSTORE)


@router.post("/quen-tai-khoan", include_in_schema=False)
async def find_submit(request: Request):
    f = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "ignore")).items()}
    ok, msg, hints = users.find_account(f.get("q", ""), _ip(request))
    return HTMLResponse(users.find_page(LOGO_K, msg, ok, hints), headers=NOSTORE)


@router.get("/dat-lai-mat-khau", include_in_schema=False)
def reset_form(t: str = ""):
    ok, x = users.reset_check(t)
    if not ok:
        return HTMLResponse(users.msg_page(LOGO_K, "Đặt lại mật khẩu", x, False,
                                           '<a class="btn full" style="background:#1c2a3d" href="/quen-mat-khau">Gửi lại yêu cầu</a>'), headers=NOSTORE)
    return HTMLResponse(users.reset_page(LOGO_K, t, email=x), headers={**NOSTORE, "Referrer-Policy": "no-referrer"})


@router.post("/dat-lai-mat-khau", include_in_schema=False)
async def reset_submit(request: Request):
    f = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "ignore")).items()}
    t = f.get("t", "")
    ok, msg = users.reset_pw(t, f.get("pw", ""), f.get("pw2", ""))
    if ok:
        return HTMLResponse(users.msg_page(LOGO_K, "Đặt lại mật khẩu", msg, True), headers=NOSTORE)
    good, email = users.reset_check(t)
    if not good:
        return HTMLResponse(users.msg_page(LOGO_K, "Đặt lại mật khẩu", email, False,
                                           '<a class="btn full" style="background:#1c2a3d" href="/quen-mat-khau">Gửi lại yêu cầu</a>'), headers=NOSTORE)
    return HTMLResponse(users.reset_page(LOGO_K, t, msg, email), headers={**NOSTORE, "Referrer-Policy": "no-referrer"})


@router.get("/api/me")
def me(request: Request):
    role = getattr(request.state, "role", None) or "chu"
    u = getattr(request.state, "user", None)
    out = {"ok": True, "role": role, "user": u}
    if u:
        email, d = users.by_id(u)
        d = d or {}
        out.update(user=email, ten=d.get("ten"), het_han=users.fmt_date(d.get("het_han")), gd=users.can_trade(u), bot=users.can_bot(u))
    elif role == "chu":
        out["cho_duyet"] = sum(1 for x in users.load().values() if x["tt"] == "cho")
    return out


@router.get("/quan-ly", include_in_schema=False)
def admin(request: Request):
    if not _owner(request):
        return HTMLResponse("Không có quyền.", status_code=403)
    return HTMLResponse(users.admin_page(LOGO_K), headers=NOSTORE)


@router.get("/quan-ly/api/list")
def admin_list(request: Request):
    if not _owner(request):
        return JSONResponse({"ok": False, "loi": "Không có quyền."}, status_code=403)
    from app.api.ea import status_of
    L = users.admin_list()
    from app.services import cbot
    for x in L:
        uid = x.pop("id", None)
        x["ea"] = status_of(uid) if x.get("gd") else None
        x["cbot"] = cbot.brief(uid) if uid and x.get("bot") else None
    return {"ok": True, "users": L}


@router.get("/quan-ly/api/mail")
def mail_status(request: Request):
    if not _owner(request):
        return JSONResponse({"ok": False, "loi": "Không có quyền."}, status_code=403)
    return {"ok": True, "on": mailer.configured(), "from": os.getenv("MAIL_FROM", mailer.FROM_DEFAULT)}


class MailIn(BaseModel):
    key: str
    to: str


@router.post("/quan-ly/api/mail")
def mail_set(x: MailIn, request: Request):
    """Gửi thư thử bằng khóa mới; gửi được mới lưu khóa vào .env (bật xác nhận email)."""
    if not _owner(request):
        return JSONResponse({"ok": False, "loi": "Không có quyền."}, status_code=403)
    key, to = x.key.strip(), x.to.strip()
    if not key.startswith("re_") or not users.EMAIL_RE.match(to.lower()):
        return {"ok": False, "loi": "Khóa API Resend bắt đầu bằng re_ và email nhận thư phải hợp lệ."}
    ok, err = mailer.send(to, "PinYin CKG · thư thử", "<p>✅ Gửi email từ PinYin CKG hoạt động. Xác nhận email khi khách đăng ký đã được bật.</p>", key=key)
    if not ok:
        return {"ok": False, "loi": f"Không gửi được thư thử, chưa lưu khóa. {err}"}
    from app.api.ckg_auto import _set_env
    _set_env("RESEND_API_KEY", key)
    return {"ok": True}


@router.get("/quan-ly/api/ai")
def ai_status(request: Request):
    if not _owner(request):
        return JSONResponse({"ok": False, "loi": "Không có quyền."}, status_code=403)
    from app.api import ckg_ai
    last = max(ckg_ai._last.values(), key=lambda r: r["ts"], default=None)
    return {"ok": True, "groq": bool(os.getenv("GROQ_API_KEY", "").strip()), "gemini": bool(os.getenv("GEMINI_API_KEY", "").strip()),
            "last": {"model": last["model"], "gio": last["gio"], "sym": last["sym"]} if last else None,
            "auto": os.getenv("PINYIN_AI_AUTO", "1") != "0", "auto_log": ckg_ai._auto["log"][-6:]}


class AiKeyIn(BaseModel):
    key: str


@router.post("/quan-ly/api/ai")
def ai_set(x: AiKeyIn, request: Request):
    """Gọi thử key Groq; chạy được mới lưu vào .env."""
    if not _owner(request):
        return JSONResponse({"ok": False, "loi": "Không có quyền."}, status_code=403)
    key = x.key.strip()
    if not key.startswith("gsk_"):
        return {"ok": False, "loi": "Key Groq bắt đầu bằng gsk_ (lấy ở console.groq.com → API Keys)."}
    from app.api import ckg_ai
    ok, info = ckg_ai.groq_test(key)
    if not ok:
        return {"ok": False, "loi": f"Gọi thử Groq không được, chưa lưu key. {info}"}
    from app.api.ckg_auto import _set_env
    _set_env("GROQ_API_KEY", key)
    return {"ok": True, "model": info}


class ActIn(BaseModel):
    user: str
    act: str
    days: int = 30


@router.post("/quan-ly/api/act")
def admin_act(x: ActIn, request: Request):
    if not _owner(request):
        return JSONResponse({"ok": False, "loi": "Không có quyền."}, status_code=403)
    if not 1 <= x.days <= 3650:
        return {"ok": False, "loi": "Số ngày không hợp lệ."}
    ok, msg = users.admin_act(x.user, x.act, x.days)
    return {"ok": ok, "msg": msg} if ok else {"ok": False, "loi": msg}


@router.get("/quan-ly/api/tai")
def admin_load(request: Request):
    if not _owner(request):
        return JSONResponse({"ok": False, "loi": "Không có quyền."}, status_code=403)
    from app.services import health
    return health.owner()


@router.post("/quan-ly/api/backup")
def admin_backup(request: Request):
    if not _owner(request):
        return JSONResponse({"ok": False, "loi": "Không có quyền."}, status_code=403)
    from app.services import health
    ok, msg = health.backup_now()
    return {"ok": ok, "msg": msg} if ok else {"ok": False, "loi": msg}

