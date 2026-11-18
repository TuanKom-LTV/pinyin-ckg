"""Cầu nối EA (PinYinBridge.ex5) — khách giao dịch trên MT5 của chính họ qua buồng lái web.

EA trong MT5 của khách gọi POST /ea/sync mỗi giây (văn bản thuần, mỗi dòng 1 bản ghi, ngăn bằng |):
  gửi lên: K|mã · V|phiên bản · A|tài khoản · S|giá vàng · I|khung|ema9|cao10|thấp10 · P|lệnh mở · O|lệnh chờ · R|id|1/0|kết quả
  nhận về: G|lá chắn · C|id|lệnh|... · X|lý do từ chối
Buồng lái của khách gọi /ckg/api/ea/q/* (cùng dạng dữ liệu với buồng lái của chủ /ckg/api/q/*).
Mật khẩu MT5 của khách không bao giờ đi qua PinYin.
"""

import itertools
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.core import users

router = APIRouter(tags=["PinYin · cầu nối EA khách"])
EA_FILE = Path(__file__).resolve().parent.parent.parent / "ea" / "PinYinBridge.ex5"
EA_LATEST = "1.6"          # phiên bản EA mới nhất trên máy chủ
ONLINE_SEC = 15         # EA gọi mỗi giây; mạng chập chờn vài lần vẫn tính online
WAIT_SEC = 10
GUARD_DEF = {"be_on": False, "be_at": 1.5, "be_lock": 0.25, "tr_on": False, "tr_at": 2.2, "tr_step": 0.6, "half": True,
             "manual": False, "auto_sltp": True, "sl": 2.5, "tp": 5.0}
_lock = threading.RLock()
_st: dict[str, dict] = {}          # mã khách → trạng thái EA
_ids = itertools.count(int(time.time()) % 100000 * 10)


def _s(uid):
    return _st.setdefault(uid, {"last": 0, "acc": None, "sym": None, "ind": {}, "pos": [], "pend": [],
                                "queue": [], "sent": set(), "res": {}, "ver": None})


def status_of(uid):
    s = _st.get(uid)
    if not s or not s["acc"]:
        return None
    a = s["acc"]
    return {"on": time.time() - s["last"] < ONLINE_SEC, "login": a["login"], "server": a["server"], "demo": a["demo"],
            "ver": s["ver"], "old": _old(s["ver"])}


def _old(v):
    try:
        return tuple(int(x) for x in (v or "0").split(".")) < tuple(int(x) for x in EA_LATEST.split("."))
    except Exception:
        return True


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


# ---------- EA gọi vào ----------

@router.post("/ea/sync", include_in_schema=False)
async def sync(request: Request):
    body = (await request.body()).decode("utf-8", "ignore")
    lines = [l.split("|") for l in body.splitlines() if l]
    key = next((l[1] for l in lines if l[0] == "K" and len(l) > 1), "")
    from app.services import copytrade
    cf = copytrade.by_key(key)
    if cf:                                    # tài khoản vào lệnh của chủ (copy lệnh bot): lá chắn EA tắt, bot tự quản
        uid, u = copytrade.uid(cf), {"ea_guard": {"be_on": False, "tr_on": False, "manual": False, "auto_sltp": False}}
    else:
        email, u, uid = users.key_uid(key.strip())
        if not u:
            return PlainTextResponse("X|Mã kết nối không đúng. Lấy lại mã trên trang pinyin.id.vn.\n")
        if users.status_of(u) != "ok" or not u.get("gd"):
            return PlainTextResponse("X|Tài khoản PinYin hết hạn hoặc chưa được cấp quyền giao dịch. Liên hệ Telegram @TuanKom.\n")
    with _lock:
        s = _s(uid)
        al = next((l for l in lines if l[0] == "A" and len(l) >= 3), None)
        cur = s.get("acc")
        if al and cur and (al[1], al[2]) != (cur["login"], cur["server"]) and time.time() - s["last"] < ONLINE_SEC:
            # 1 mã = 1 MT5: MT5 khác đang dùng mã này → từ chối, không để lệnh vào nhầm tài khoản
            s["clash"] = {"login": al[1], "server": al[2], "t": time.time()}
            return PlainTextResponse(f"X|Mã kết nối này đang dùng cho MT5 #{cur['login']} ({cur['server']}). "
                                     "Mỗi mã chỉ gắn 1 tài khoản MT5. Tắt EA bên kia (chờ 15 giây) hoặc dùng mã khác.\n")
        s["last"], pos, pend, ind = time.time(), [], [], {}
        for l in lines:
            t = l[0]
            if t == "V" and len(l) > 1:
                s["ver"] = l[1]
            elif t == "A" and len(l) >= 12:
                s["acc"] = {"login": l[1], "server": l[2], "company": l[3], "currency": l[4], "balance": _f(l[5]),
                            "equity": _f(l[6]), "margin_free": _f(l[7]), "leverage": l[8], "demo": l[9] == "1",
                            "algo": l[10] == "1", "name": l[11]}
            elif t == "S" and len(l) >= 10:
                s["sym"] = {"name": l[1], "bid": _f(l[2]), "ask": _f(l[3]), "digits": int(_f(l[4], 2)), "point": _f(l[5]),
                            "stops": _f(l[6]), "vmin": _f(l[7], .01), "vstep": _f(l[8], .01), "unit": _f(l[9], 1)}
            elif t == "I" and len(l) >= 5:
                ind[l[1]] = {"ema9": _f(l[2]) or None, "hi10": _f(l[3]) or None, "lo10": _f(l[4]) or None}
            elif t == "P" and len(l) >= 9:
                pos.append({"ticket": int(_f(l[1])), "t": l[2], "lot": _f(l[3]), "e": _f(l[4]), "sl": _f(l[5]),
                            "tp": _f(l[6]), "profit": _f(l[7]), "half": l[8] == "1", "bot": len(l) > 9 and l[9] == "1"})
            elif t == "O" and len(l) >= 7:
                pend.append({"ticket": int(_f(l[1])), "t": l[2], "lot": _f(l[3]), "e": _f(l[4]), "sl": _f(l[5]), "tp": _f(l[6])})
            elif t == "R" and len(l) >= 4:
                s["res"][l[1]] = {"ok": l[2] == "1", "msg": "|".join(l[3:]), "t": time.time()}
            elif t == "N" and len(l) > 1:                       # EA tự báo (sàn chốt TP1, dời hòa vốn)
                copytrade.note(uid, "|".join(l[1:]))
            elif t == "R" and len(l) == 3 and l[2][:2] in ("1/", "0/"):      # EA 1.0: "R|id|1/thông báo"
                s["res"][l[1]] = {"ok": l[2][0] == "1", "msg": l[2][2:], "t": time.time()}
        s["pos"], s["pend"], s["ind"] = pos, pend, ind
        g = {**GUARD_DEF, **(u.get("ea_guard") or {})}
        out = ["G|%d|%s|%s|%d|%s|%s|%d|%d|%d|%s|%s" % (g["be_on"], g["be_at"], g["be_lock"], g["tr_on"], g["tr_at"], g["tr_step"], g["half"],
                                                  g["manual"], g["auto_sltp"], g["sl"], g["tp"])]
        pl = copytrade.plan_for(uid)
        if pl:
            out.append(pl)
        for c in s["queue"]:
            out.append("C|" + c["id"] + "|" + c["line"])
            s["sent"].add(c["id"])
        s["queue"] = []
        for k in [k for k, v in s["res"].items() if time.time() - v["t"] > 120]:   # dọn kết quả cũ
            s["res"].pop(k, None)
    return PlainTextResponse("\n".join(out) + "\n")


@router.get("/ea/download", include_in_schema=False)
def download():
    if not EA_FILE.exists():
        return JSONResponse({"ok": False, "loi": "Chưa có file EA trên máy chủ."}, status_code=404)
    return FileResponse(EA_FILE, filename="PinYinBridge.ex5", media_type="application/octet-stream")


# ---------- Buồng lái của khách ----------

def _me(request: Request):
    """Khách + tài khoản MT5 đang chọn trên trang (header X-Acc: 1/2/3)."""
    uid = getattr(request.state, "user", None)
    if not uid or not users.can_trade(uid):
        return None
    n = (request.headers.get("x-acc") or "1").strip()
    if n == "1":
        return uid
    return next((a["uid"] for a in users.accounts(uid) if str(a["n"]) == n), None)   # tài khoản đã xóa: từ chối


def _deny():
    return JSONResponse({"ok": False, "loi": "Tài khoản chưa được cấp quyền giao dịch. Liên hệ Telegram @TuanKom."}, status_code=403)


def _online(s):
    return s["acc"] and time.time() - s["last"] < ONLINE_SEC


def _send(uid, line, wait=None):
    """Đưa lệnh vào hàng chờ cho EA, đợi EA báo kết quả."""
    cid = str(next(_ids))
    with _lock:
        s = _s(uid)
        if not _online(s):
            return {"ok": False, "loi": "EA trên MT5 của bạn đang offline. Mở MT5 và kiểm tra EA PinYinBridge."}
        s["queue"].append({"id": cid, "line": line})
    end = time.time() + (WAIT_SEC if wait is None else max(1.0, min(WAIT_SEC, wait)))
    while time.time() < end:
        time.sleep(0.2)
        with _lock:
            r = s["res"].pop(cid, None)
        if r:
            return {"ok": True, "msg": r["msg"]} if r["ok"] else {"ok": False, "loi": r["msg"]}
    with _lock:
        if cid not in s["sent"]:                       # EA chưa nhận: hủy để không vào lệnh trễ
            s["queue"] = [c for c in s["queue"] if c["id"] != cid]
            return {"ok": False, "loi": "EA không phản hồi, lệnh đã hủy. Kiểm tra MT5 của bạn."}
    return {"ok": False, "loi": "Lệnh đã gửi tới MT5 nhưng chưa có kết quả. Kiểm tra lại trên MT5 trước khi bấm lại."}


@router.get("/ckg/api/ea/info")
def info(request: Request):
    uid = _me(request)
    if not uid:
        return _deny()
    email, u = users.by_id(uid)
    from app.services import cbot
    cur = users.split_uid(uid)[1]
    accs = []
    for a in users.accounts(uid):
        st = status_of(a["uid"])
        accs.append({"n": a["n"], "name": a["name"], "ea": st, "online": bool(st and st["on"]),
                     "bot": bool(users.can_bot(a["uid"]) and cbot.get(a["uid"])["cfg"].get("on"))})
    key = next((a["key"] for a in users.accounts(uid) if a["n"] == cur), None)
    return {"ok": True, "key": key, "url": "https://pinyin.id.vn", "ea": status_of(uid), "ver": _s(uid)["ver"],
            "cur": cur, "accs": accs, "max": users.MAX_ACC}


class AccIn(BaseModel):
    act: str
    n: int = 0
    name: str = ""


@router.post("/ckg/api/ea/acc")
def acc_set(x: AccIn, request: Request):
    """Khách thêm / đổi tên / đổi mã / xóa tài khoản MT5 (tối đa users.MAX_ACC)."""
    uid = getattr(request.state, "user", None)
    if not uid or not users.can_trade(uid):
        return _deny()
    ok, msg, n = users.acc_act(uid, x.act, x.n, x.name)
    if ok and x.act in ("del", "key") and n:
        sub = uid if n == 1 else f"{uid}~{n}"
        from app.services import cbot, copytrade
        if x.act == "del":
            try:
                c = cbot.get(sub)
                c["cfg"]["on"] = False
                cbot.save(sub)
            except Exception:
                pass
            copytrade.EXTRA.pop("cb-" + sub, None)
        with _lock:
            _st.pop(sub, None)                      # EA cũ (mã cũ) phải kết nối lại
    return {"ok": ok, "msg": msg, "n": n} if ok else {"ok": False, "loi": msg}


@router.get("/ckg/api/ea/q/tick")
def tick(request: Request, sym: str = "XAUUSD", tf: str = "M1"):
    uid = _me(request)
    if not uid:
        return _deny()
    s = _s(uid)
    if not _online(s):
        return {"ok": False, "offline": True, "loi": "EA offline", "ea": status_of(uid)}
    email, u = users.by_id(uid)
    y, a, ind = s["sym"] or {}, s["acc"], s["ind"].get(tf) or {}
    return {"ok": True, "sym": "XAUUSD", "bid": y.get("bid"), "ask": y.get("ask"), "digits": y.get("digits", 2),
            "unit": y.get("unit", 1.0), "unit_name": "$", "stops": y.get("stops"), "vmin": y.get("vmin"), "vstep": y.get("vstep"),
            "ema9": ind.get("ema9"), "hi10": ind.get("hi10"), "lo10": ind.get("lo10"), "tf": tf, "broker_sym": y.get("name"),
            "ea_ver": s["ver"], "ea_old": _old(s["ver"]), "ea_latest": EA_LATEST,
            "guard": {**GUARD_DEF, **(u.get("ea_guard") or {})},
            "acc": {"login": a["login"], "server": a["server"], "currency": a["currency"], "equity": a["equity"],
                    "demo": a["demo"], "algo": a["algo"]}}


@router.get("/ckg/api/ea/q/book")
def book(request: Request, sym: str = "XAUUSD"):
    uid = _me(request)
    if not uid:
        return _deny()
    s = _s(uid)
    return {"ok": True, "pos": s["pos"], "pend": s["pend"]}


class QOrder(BaseModel):
    sym: str = "XAUUSD"
    side: str
    lot: float
    mode: str = "now"
    sl: float = 0
    tp: float = 0
    off: float = 0.2
    tf: str = "M1"


@router.post("/ckg/api/ea/q/order")
def order(o: QOrder, request: Request):
    uid = _me(request)
    if not uid:
        return _deny()
    side = o.side.upper()
    if side not in ("BUY", "SELL") or o.mode not in ("now", "ema9", "zone") or o.tf not in ("M1", "M5", "M15"):
        return {"ok": False, "loi": "Lệnh không hợp lệ."}
    if not (0.01 <= o.lot <= 50) or o.sl < 0 or o.tp < 0 or not (0 <= o.off <= 100):
        return {"ok": False, "loi": "Lot / SL / TP không hợp lệ."}
    return _send(uid, f"ORDER|{side}|{o.lot:.2f}|{o.mode}|{o.sl:.2f}|{o.tp:.2f}|{o.off:.2f}|{o.tf}")


class QSym(BaseModel):
    sym: str = "XAUUSD"
    ticket: int | None = None


def _act(name):
    def f(x: QSym, request: Request):
        uid = _me(request)
        if not uid:
            return _deny()
        return _send(uid, f"{name}|{int(x.ticket or 0)}")
    return f


router.post("/ckg/api/ea/q/closeall")(_act("CLOSE"))
router.post("/ckg/api/ea/q/half")(_act("HALF"))
router.post("/ckg/api/ea/q/cancel")(_act("CANCEL"))
router.post("/ckg/api/ea/q/be")(_act("BE"))


class QGuard(BaseModel):
    sym: str = "XAUUSD"
    be_on: bool | None = None
    be_at: float | None = None
    be_lock: float | None = None
    tr_on: bool | None = None
    tr_at: float | None = None
    tr_step: float | None = None
    half: bool | None = None
    manual: bool | None = None
    auto_sltp: bool | None = None
    sl: float | None = None
    tp: float | None = None


@router.post("/ckg/api/ea/q/guard")
def guard(x: QGuard, request: Request):
    uid = _me(request)
    if not uid:
        return _deny()
    email, u = users.by_id(uid)
    g = {**GUARD_DEF, **(u.get("ea_guard") or {})}
    for k, v in x.model_dump(exclude={"sym"}).items():
        if v is not None:
            if isinstance(v, float) and not (0 <= v <= 200):
                return {"ok": False, "loi": f"{k} không hợp lệ."}
            g[k] = v
    if g["be_lock"] >= g["be_at"]:
        return {"ok": False, "loi": "Mức khóa hòa vốn phải nhỏ hơn mức kích hoạt."}
    users._set(email, ea_guard=g)
    return {"ok": True, "guard": g}


# ---------- Bot tự động của khách (chủ duyệt) ----------

def _bot_me(request: Request):
    uid = _me(request)
    if not uid or not users.can_bot(uid):
        return None
    return uid


def _bot_deny():
    return JSONResponse({"ok": False, "loi": "Tài khoản chưa được duyệt dùng bot. Liên hệ Telegram @TuanKom."}, status_code=403)


@router.get("/ckg/api/ea/bot")
def cbot_status(request: Request):
    uid = _bot_me(request)
    if not uid:
        return _bot_deny()
    from app.services import cbot
    return cbot.status(uid)


@router.post("/ckg/api/ea/bot")
async def cbot_set(request: Request):
    uid = _bot_me(request)
    if not uid:
        return _bot_deny()
    from app.services import cbot
    try:
        x = await request.json()
    except Exception:
        return {"ok": False, "loi": "Dữ liệu không hợp lệ."}
    if not isinstance(x, dict):
        return {"ok": False, "loi": "Dữ liệu không hợp lệ."}
    import asyncio
    return await asyncio.to_thread(cbot.set_cfg, uid, x)

