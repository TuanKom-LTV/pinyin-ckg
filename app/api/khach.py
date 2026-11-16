"""Đăng ký khách (/dang-ky), quản lý khách (/quan-ly, chỉ chủ) và thông tin phiên (/api/me)."""

from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.core import users
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


@router.get("/api/me")
def me(request: Request):
    role = getattr(request.state, "role", None) or "chu"
    u = getattr(request.state, "user", None)
    out = {"ok": True, "role": role, "user": u}
    if u:
        email, d = users.by_id(u)
        d = d or {}
        out.update(user=email, ten=d.get("ten"), het_han=users.fmt_date(d.get("het_han")))
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
    return {"ok": True, "users": users.admin_list()}


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
