"""PinYin CKG: bot tự vào lệnh XAUUSD theo tín hiệu EMA9 × EMA21 (giống hệt bảng phân tích vàng).

Chạy trên máy chủ (luồng nền), không cần mở trang.
  - Tín hiệu: nến vừa ĐÓNG có EMA9 cắt EMA21. BUY khi cắt lên, SELL khi cắt xuống.
  - SL: đáy (BUY) / đỉnh (SELL) của 4 nến gần nhất kể cả nến tín hiệu. TP: giá vào ± Bước TP × 1/2/3.
  - Có tín hiệu ngược chiều: chốt lệnh bot đang mở rồi vào lệnh mới.
  - Lot theo bảng vốn (vốn quy ra USD): 100$→0.01, 600$→0.02, 1000$→0.03; số lệnh bot tối đa 3/5 theo bậc.
  - Dùng chung chốt an toàn của buồng lái: chỉ tài khoản DEMO (trừ khi PINYIN_ALLOW_REAL=1), cần bật Algo Trading.
  - Lệnh bot có MAGIC riêng 260117, không đụng lệnh vào tay hay lệnh buồng lái.
  - Báo Telegram khi bật/tắt, vào lệnh, đóng lệnh (kể cả chạm SL/TP), lỗi.
Cài đặt lưu ở data/auto_xau.json (riêng từng máy, mặc định TẮT).
"""

import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.ckg import server_offset
from app.api.ckg_trade import _account_guard, _filling, _norm_lot, _send, DEVIATION, MAX_LOT
from app.services.mt5_service import mt5_service


SYM = "XAUUSD"
MAGIC = 260117
COMMENT = "PinYin AUTO"
TFS = {"M1": (mt5.TIMEFRAME_M1, 60), "M5": (mt5.TIMEFRAME_M5, 300)}
TIERS = [(100, 3, .01), (300, 5, .01), (600, 3, .02), (900, 5, .02), (1000, 3, .03), (1200, 5, .03)]
VN = timezone(timedelta(hours=7))
ROOT = Path(__file__).resolve().parent.parent.parent
CFG_FILE = ROOT / "data" / "auto_xau.json"
LOG_FILE = ROOT / "data" / "auto_xau_log.jsonl"
ENV_FILE = ROOT / ".env"
LOOP_SEC = 2

router = APIRouter(prefix="/ckg/api", tags=["PinYin CKG · bot tự động"])

DEFAULT = {"on": False, "tf": "M1", "step": 2.0, "tp": 1,
           # lá chắn cho bot: SL/TP theo tín hiệu hoặc cố định $, hòa vốn tự động, trailing + chốt 50%
           "sl_mode": "4n", "sl_usd": 3.0, "tp_mode": "step", "tp_usd": 5.0,
           "be_on": False, "be_at": 1.5, "be_lock": 0.25, "tr_on": False, "tr_at": 2.2, "tr_step": 0.6, "half": True,
           "lot": None}   # lot cố định; None = theo bảng vốn
_lock = threading.RLock()
_log = deque(maxlen=60)
_state = {"last_bar": 0, "check": 0, "msg": "Chưa chạy", "sig": None, "lot": None, "known": {}}


# ---------- Cài đặt + nhật ký ----------

def _cfg():
    try:
        c = {**DEFAULT, **json.loads(CFG_FILE.read_text(encoding="utf-8"))}
    except Exception:
        c = dict(DEFAULT)
    return c


def _save(c):
    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def _now_vn():
    return datetime.now(VN).strftime("%d/%m %H:%M:%S")


def log(msg, kind="info", tele=True):
    e = {"t": _now_vn(), "k": kind, "m": msg}
    _log.appendleft(e)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except Exception:
        pass
    if tele:
        icon = {"open": "🟢", "close": "🔵", "err": "⚠️", "info": "ℹ️"}.get(kind, "ℹ️")
        tg_send(f"{icon} PinYin AUTO · {msg}")


def _load_log():
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-60:]
        for l in lines:
            _log.appendleft(json.loads(l))
    except Exception:
        pass


# ---------- Telegram (ép IPv4: máy này IPv6 hỏng làm Python treo) ----------

class _V4Conn(http.client.HTTPSConnection):
    def connect(self):
        ip = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        sock = socket.create_connection((ip, self.port), self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)


def _env(name):
    return (os.getenv(name) or "").strip()


def _tg_api(method, params=None, token=None):
    token = token or _env("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    conn = _V4Conn("api.telegram.org", timeout=15)
    try:
        body = urllib.parse.urlencode(params or {})
        conn.request("POST", f"/bot{token}/{method}", body, {"Content-Type": "application/x-www-form-urlencoded"})
        return json.loads(conn.getresponse().read().decode("utf-8"))
    finally:
        conn.close()


def tg_send(text):
    if not (_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID")):
        return

    def run():
        try:
            _tg_api("sendMessage", {"chat_id": _env("TELEGRAM_CHAT_ID"), "text": text})
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def _set_env(name, value):
    """Ghi/sửa 1 dòng trong .env và cập nhật biến môi trường đang chạy."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if not l.startswith(name + "=")] + [f"{name}={value}"]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value


# ---------- Tín hiệu (giống hàm gsig trên trang) ----------

def _ema(a, n):
    k = 2 / (n + 1)
    e = a[0]
    out = []
    for x in a:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def signal_at(rows, i, step):
    """rows: [(t,o,h,l,c), ...]. Trả tín hiệu ở nến i (nến đã đóng) hoặc None."""
    if i < 15:
        return None
    cl = [r[4] for r in rows]
    E, E2 = _ema(cl, 9), _ema(cl, 21)
    t = 0
    if E[i - 1] <= E2[i - 1] and E[i] > E2[i]:
        t = 1
    if E[i - 1] >= E2[i - 1] and E[i] < E2[i]:
        t = -1
    if not t:
        return None
    w = rows[i - 3:i + 1]
    sl = min(r[3] for r in w) if t > 0 else max(r[2] for r in w)
    return {"t": t, "side": "BUY" if t > 0 else "SELL", "e": cl[i], "sl": sl,
            "tp": [cl[i] + t * step * n for n in (1, 2, 3)], "bar": int(rows[i][0])}


# ---------- Lệnh ----------

def _to_usd(ccy):
    if ccy == "USD":
        return 1.0
    for s, inv in ((ccy + "USD", False), ("USD" + ccy, True)):
        if mt5.symbol_select(s, True):
            tk = mt5.symbol_info_tick(s)
            if tk and tk.bid > 0:
                m = (tk.bid + tk.ask) / 2
                return 1 / m if inv else m
    return None


def _tier(a):
    r = _to_usd(a.currency)
    cap = a.equity * r if r else None
    b = None
    for x in TIERS:
        if cap is not None and cap >= x[0]:
            b = x
    return cap, b


def _bot_positions():
    return [p for p in (mt5.positions_get(symbol=SYM) or []) if p.magic == MAGIC]


def _close_pos(p, why):
    info = mt5.symbol_info(SYM)
    tick = mt5.symbol_info_tick(SYM)
    buy = p.type == mt5.POSITION_TYPE_BUY
    res = _send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYM, "position": p.ticket, "volume": p.volume,
        "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
        "price": tick.bid if buy else tick.ask, "deviation": DEVIATION,
        "magic": MAGIC, "comment": COMMENT, "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info),
    })
    _state["known"].pop(p.ticket, None)
    log(f"Đóng #{p.ticket} {'BUY' if buy else 'SELL'} {p.volume} lot @ {res.price} ({why}), lãi/lỗ {p.profit + p.swap:.2f}", "close")


def _open(sg, lot, tp_n, c=None):
    info = mt5.symbol_info(SYM)
    lot = _norm_lot(info, lot)
    tick = mt5.symbol_info_tick(SYM)
    if not tick or tick.bid <= 0:
        raise RuntimeError("Chưa có giá XAUUSD (thị trường có thể đang đóng).")
    buy = sg["t"] > 0
    price = tick.ask if buy else tick.bid
    min_dist = (info.trade_stops_level + 1) * info.point
    c = c or DEFAULT
    sl = price - sg["t"] * c["sl_usd"] if c.get("sl_mode") == "usd" else sg["sl"]
    tp = price + sg["t"] * c["tp_usd"] if c.get("tp_mode") == "usd" else sg["tp"][tp_n - 1]
    # Giá đã chạy qua SL/TP trong lúc chờ thì bỏ tín hiệu (không vào lệnh sai)
    if (buy and (price - sl < min_dist or tp - price < min_dist)) or (not buy and (sl - price < min_dist or price - tp < min_dist)):
        raise RuntimeError(f"Giá hiện {price} đã quá gần/vượt SL {sl:.2f} hoặc TP {tp:.2f}, bỏ tín hiệu.")
    res = _send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYM, "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": price, "deviation": DEVIATION,
        "magic": MAGIC, "comment": COMMENT, "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info),
        "sl": round(sl, info.digits), "tp": round(tp, info.digits),
    })
    _state["known"][res.order] = sg["side"]
    log(f"Vào {sg['side']} {res.volume} lot @ {res.price} · SL {sl:.2f} · TP{tp_n} {tp:.2f}", "open")


def _check_closed(off):
    """Lệnh bot biến mất (chạm SL/TP hoặc đóng tay trên MT5) → báo kết quả."""
    live = {p.ticket for p in _bot_positions()}
    for tk in [t for t in _state["known"] if t not in live]:
        side = _state["known"].pop(tk)
        deals = mt5.history_deals_get(position=tk) or []
        out = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
        pnl = sum(d.profit + d.swap + d.commission for d in deals)
        why = {mt5.DEAL_REASON_SL: "chạm SL", mt5.DEAL_REASON_TP: "chạm TP"}.get(out[-1].reason, "đóng") if out else "đóng"
        px = out[-1].price if out else "?"
        log(f"#{tk} {side} {why} @ {px}, lãi/lỗ {pnl:.2f}", "close")


def _step():
    c = _cfg()
    tf_code, tf_sec = TFS.get(c["tf"], TFS["M1"])
    with mt5_service.session():
        off, _ = server_offset()
        _check_closed(off)
        if not mt5.symbol_select(SYM, True):
            _state["msg"] = "Broker không có XAUUSD"
            return
        r = mt5.copy_rates_from_pos(SYM, tf_code, 0, 600)
        if r is None or len(r) < 30:
            _state["msg"] = "Chưa đủ nến"
            return
        rows = [(int(x["time"]) - off, x["open"], x["high"], x["low"], x["close"]) for x in r]
        i = len(rows) - 2                       # nến vừa đóng (nến cuối đang chạy)
        bar = rows[i][0]
        if not _state["last_bar"]:              # vừa bật: không vào theo tín hiệu cũ
            _state["last_bar"] = bar
        a = mt5.account_info()
        cap, b = _tier(a) if a else (None, None)
        _state["lot"] = b[2] if b else None
        _state["msg"] = f"Đang theo dõi {c['tf']} · nến đóng {datetime.fromtimestamp(bar, VN):%H:%M}"
        if bar <= _state["last_bar"]:
            return
        _state["last_bar"] = bar
        sg = signal_at(rows, i, float(c["step"]))
        if not sg:
            return
        _state["sig"] = {**sg, "at": _now_vn()}
        log(f"Tín hiệu {sg['side']} {c['tf']} @ {sg['e']:.2f} (SL {sg['sl']:.2f})", "info", tele=False)
        a, block = _account_guard()
        if block:
            log(f"Không vào lệnh: {block}", "err")
            return
        fixed = c.get("lot")
        if not b and not fixed:
            log(f"Không vào lệnh: vốn {cap or 0:.0f}$ dưới mức 100$ của bảng vốn.", "err")
            return
        lot, maxn = (fixed, b[1] if b else 1) if fixed else (b[2], b[1])
        try:
            mine = _bot_positions()
            for p in mine:                       # tín hiệu ngược chiều: chốt lệnh cũ
                if (p.type == mt5.POSITION_TYPE_BUY) != (sg["t"] > 0):
                    _close_pos(p, "tín hiệu ngược chiều")
            mine = _bot_positions()
            if len(mine) >= maxn:
                log(f"Không vào thêm: bot đã có {len(mine)}/{maxn} lệnh theo bảng vốn.", "err")
                return
            _open(sg, lot, int(c["tp"]), c)
        except Exception as e:                   # lỗi theo từng tín hiệu: luôn báo
            log(f"Bỏ tín hiệu {sg['side']}: {e}", "err")


def _loop():
    _load_log()
    err_at = 0
    while True:
        try:
            if _cfg()["on"]:
                with _lock:
                    _step()
                    _state["check"] = time.time()
            else:
                _state["msg"] = "Đang tắt"
                _state["last_bar"] = 0
                if _state["known"]:              # bot tắt nhưng lệnh cũ còn: vẫn báo khi chạm SL/TP
                    with _lock, mt5_service.session():
                        _check_closed(0)
        except Exception as e:
            _state["msg"] = f"Lỗi: {e}"
            if time.time() - err_at > 600:      # báo lỗi tối đa 10 phút/lần
                err_at = time.time()
                log(f"Lỗi: {e}", "err")
        time.sleep(LOOP_SEC)


_started = False


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="pinyin-auto").start()
        try:                                    # nhớ lại các lệnh bot đang mở để báo khi chúng đóng
            with mt5_service.session():
                for p in _bot_positions():
                    _state["known"][p.ticket] = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        except Exception:
            pass


# ---------- API ----------

class AutoIn(BaseModel):
    on: bool | None = None
    tf: str | None = None
    step: float | None = None
    tp: int | None = None
    sl_mode: str | None = None
    sl_usd: float | None = None
    tp_mode: str | None = None
    tp_usd: float | None = None
    be_on: bool | None = None
    be_at: float | None = None
    be_lock: float | None = None
    tr_on: bool | None = None
    tr_at: float | None = None
    tr_step: float | None = None
    half: bool | None = None
    lot: float | None = None
    lot_auto: bool | None = None


class TgIn(BaseModel):
    token: str


@router.get("/auto")
def auto_status():
    c = _cfg()
    pos = []
    try:
        with mt5_service.session():
            a = mt5.account_info()
            if a and _state["lot"] is None:
                b = _tier(a)[1]
                _state["lot"] = b[2] if b else None
            pos = [{"ticket": p.ticket, "t": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL", "lot": p.volume,
                    "e": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit + p.swap} for p in _bot_positions()]
    except Exception:
        pass
    return {"ok": True, "cfg": c, "msg": _state["msg"], "check": _state["check"], "lot": _state["lot"],
            "sig": _state["sig"], "pos": pos, "log": list(_log)[:30],
            "tg": bool(_env("TELEGRAM_BOT_TOKEN")), "tg_chat": bool(_env("TELEGRAM_CHAT_ID"))}


@router.post("/auto")
def auto_set(x: AutoIn):
    c = _cfg()
    was = c["on"]
    if x.tf is not None:
        if x.tf not in TFS:
            return {"ok": False, "loi": "Khung chỉ M1 hoặc M5."}
        c["tf"] = x.tf
    if x.step is not None:
        if not (0.2 <= x.step <= 100):
            return {"ok": False, "loi": "Bước TP từ 0.2 đến 100 $."}
        c["step"] = round(x.step, 2)
    if x.tp is not None:
        if x.tp not in (1, 2, 3):
            return {"ok": False, "loi": "TP chỉ 1, 2 hoặc 3."}
        c["tp"] = x.tp
    if x.sl_mode is not None:
        if x.sl_mode not in ("4n", "usd"):
            return {"ok": False, "loi": "Kiểu SL không hợp lệ."}
        c["sl_mode"] = x.sl_mode
    if x.tp_mode is not None:
        if x.tp_mode not in ("step", "usd"):
            return {"ok": False, "loi": "Kiểu TP không hợp lệ."}
        c["tp_mode"] = x.tp_mode
    for k in ("sl_usd", "tp_usd", "be_at", "be_lock", "tr_at", "tr_step"):
        v = getattr(x, k)
        if v is not None:
            if not (0.05 <= v <= 200):
                return {"ok": False, "loi": f"Giá trị {k} phải từ 0.05 đến 200 $."}
            c[k] = round(v, 2)
    for k in ("be_on", "tr_on", "half"):
        v = getattr(x, k)
        if v is not None:
            c[k] = v
    if c["be_lock"] >= c["be_at"]:
        return {"ok": False, "loi": "Mức khóa hòa vốn phải nhỏ hơn mức kích hoạt."}
    if x.lot_auto:
        c["lot"] = None
    elif x.lot is not None:
        if not (0.01 <= x.lot <= MAX_LOT):
            return {"ok": False, "loi": f"Lot từ 0.01 đến {MAX_LOT}."}
        c["lot"] = round(x.lot, 2)
    if x.on is not None:
        c["on"] = x.on
    _save(c)
    with _lock:
        _state["last_bar"] = 0                  # đổi cài đặt / bật lại: chỉ vào theo tín hiệu MỚI
    if c["on"] != was:
        log(("BẬT" if c["on"] else "TẮT") + f" bot · {c['tf']} · Bước TP {c['step']}$ · chốt ở TP{c['tp']}")
    elif c["on"]:
        log(f"Đổi cài đặt: {c['tf']} · Bước TP {c['step']}$ · chốt ở TP{c['tp']}", tele=False)
    return {"ok": True, "cfg": c}


@router.post("/auto/telegram")
def auto_telegram(x: TgIn):
    """Lưu token bot Telegram, rồi tìm chat của người đã nhắn /start cho bot."""
    tok = x.token.strip()
    try:
        me = _tg_api("getMe", token=tok)
        if not me or not me.get("ok"):
            return {"ok": False, "loi": "Token không đúng."}
        up = _tg_api("getUpdates", token=tok) or {}
        chats = [u["message"]["chat"] for u in up.get("result", []) if "message" in u]
        if not chats:
            return {"ok": False, "loi": f"Mở Telegram, nhắn /start cho bot @{me['result']['username']} rồi bấm Kết nối lại."}
        _set_env("TELEGRAM_BOT_TOKEN", tok)
        _set_env("TELEGRAM_CHAT_ID", str(chats[-1]["id"]))
        _tg_api("sendMessage", {"chat_id": chats[-1]["id"], "text": "✅ PinYin AUTO đã kết nối. Bot sẽ báo mọi lệnh vào/đóng ở đây."})
        return {"ok": True, "bot": me["result"]["username"]}
    except Exception as e:
        return {"ok": False, "loi": str(e)}
