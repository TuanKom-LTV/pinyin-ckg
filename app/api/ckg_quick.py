"""PinYin CKG: buồng lái nhanh (điều khiển MT5 từ xa).

Đơn vị khoảng cách: XAUUSD tính bằng giá ($), cặp FX tính bằng pip.
  - 3 chế độ vào lệnh: NGAY (lệnh thị trường), EMA9 (lệnh chờ ở EMA9 ± khoảng cách), HỘI TỤ (lệnh chờ ở cản Cao/Thấp 10 nến).
  - Chốt 50% khối lượng, đóng toàn bộ, dời SL hòa vốn ngay, hủy lệnh chờ.
  - 2 lá chắn chạy nền trên máy chủ (đóng trang vẫn chạy):
      Hòa vốn tự động (BE): lãi đạt X → dời SL về giá vào + khóa Y.
      Trailing: lãi đạt X → chốt 50% (1 lần) rồi kéo SL theo giá, cách Z.
Chỉ đụng lệnh có MAGIC buồng lái CKG (260115). Lệnh bot tự động và lệnh vào tay không bị đụng.
"""

import json
import threading
import time
from pathlib import Path

import MetaTrader5 as mt5
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.ckg import CKG_SYMBOLS, server_offset
from app.api.ckg_trade import MAGIC, COMMENT, DEVIATION, _account_guard, _filling, _norm_lot, _send, _sym, allow_real
from app.services.mt5_service import mt5_service
from app.services.apicache import ttl


router = APIRouter(prefix="/ckg/api/q", tags=["PinYin CKG · buồng lái nhanh"])
ROOT = Path(__file__).resolve().parent.parent.parent
GUARD_FILE = ROOT / "data" / "guard.json"
TFS = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}
GOLD_DEF = {"be_on": False, "be_at": 1.5, "be_lock": 0.25, "tr_on": False, "tr_at": 2.2, "tr_step": 0.6, "half": True,
            "manual": False, "auto_sltp": True, "sl": 2.5, "tp": 5.0}   # manual: áp lá chắn cho lệnh vào tay (MT5 điện thoại...)
FX_DEF = {"be_on": False, "be_at": 15, "be_lock": 2, "tr_on": False, "tr_at": 22, "tr_step": 6, "half": True,
          "manual": False, "auto_sltp": True, "sl": 20.0, "tp": 30.0}
_lock = threading.RLock()
_half_done = set()          # ticket đã chốt 50% bởi trailing (chỉ 1 lần)


def _unit(info):
    """1 đơn vị khoảng cách: vàng = 1$ giá, FX = 1 pip (10 point)."""
    return 1.0 if info.name.startswith("XAU") else info.point * 10


def _guards():
    try:
        return json.loads(GUARD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _guard(sym):
    g = _guards().get(sym)
    base = GOLD_DEF if sym.startswith("XAU") else FX_DEF
    return {**base, **(g or {})}


def _ema(a, n):
    k = 2 / (n + 1)
    e = a[0]
    for x in a[1:]:
        e = x * k + e * (1 - k)
    return e


def _levels(sym, tf):
    """EMA9 (nến hiện tại) và Cao/Thấp 10 nến đã đóng của khung tf."""
    r = mt5.copy_rates_from_pos(sym, TFS.get(tf, mt5.TIMEFRAME_M1), 0, 300)
    if r is None or len(r) < 20:
        return None, None, None
    closes = [float(x["close"]) for x in r]
    last10 = r[-11:-1]
    return _ema(closes, 9), float(max(x["high"] for x in last10)), float(min(x["low"] for x in last10))


def _ok(**kw):
    return {"ok": True, **kw}


def _err(msg):
    return {"ok": False, "loi": msg}


def _notify(msg):
    try:
        from app.api.ckg_auto import tg_send
        tg_send("🛡 Buồng lái · " + msg)
    except Exception:
        pass


# ---------- Giá + mức tham chiếu (trang hỏi mỗi giây) ----------

@router.get("/tick")
@ttl(0.5)
def tick(sym: str = "XAUUSD", tf: str = "M1"):
    try:
        with mt5_service.session():
            sym = _sym(sym)
            info = mt5.symbol_info(sym)
            tk = mt5.symbol_info_tick(sym)
            e9, hi, lo = _levels(sym, tf)
            a = mt5.account_info()
            return _ok(sym=sym, bid=tk.bid if tk else None, ask=tk.ask if tk else None, digits=info.digits,
                       unit=_unit(info), unit_name="$" if sym.startswith("XAU") else "pip",
                       stops=(info.trade_stops_level + 1) * info.point, vmin=info.volume_min, vstep=info.volume_step,
                       ema9=e9, hi10=hi, lo10=lo, tf=tf, guard=_guard(sym),
                       acc={"login": a.login, "server": a.server, "currency": a.currency, "equity": a.equity,
                            "demo": a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO, "real_ok": allow_real()} if a else None)
    except Exception as e:
        return _err(str(e))


@router.get("/book")
@ttl(1)
def book(sym: str = "XAUUSD"):
    """Lệnh đang mở + lệnh chờ của buồng lái trên mã này."""
    try:
        with mt5_service.session():
            sym = _sym(sym)
            off, _ = server_offset()
            pos = [{"ticket": p.ticket, "t": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL", "lot": p.volume,
                    "e": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit + p.swap,
                    "half": p.ticket in _half_done}
                   for p in (mt5.positions_get(symbol=sym) or []) if p.magic == MAGIC]
            names = {mt5.ORDER_TYPE_BUY_LIMIT: "BUY LIMIT", mt5.ORDER_TYPE_SELL_LIMIT: "SELL LIMIT",
                     mt5.ORDER_TYPE_BUY_STOP: "BUY STOP", mt5.ORDER_TYPE_SELL_STOP: "SELL STOP"}
            pend = [{"ticket": o.ticket, "t": names.get(o.type, str(o.type)), "lot": o.volume_current,
                     "e": o.price_open, "sl": o.sl, "tp": o.tp}
                    for o in (mt5.orders_get(symbol=sym) or []) if o.magic == MAGIC]
            return _ok(pos=pos, pend=pend)
    except Exception as e:
        return _err(str(e))


# ---------- Vào lệnh ----------

class QOrder(BaseModel):
    sym: str
    side: str            # BUY | SELL
    lot: float
    mode: str = "now"    # now | ema9 | zone
    sl: float = 0        # khoảng cách SL (đơn vị: $ vàng / pip FX), 0 = không đặt
    tp: float = 0
    off: float = 0.2     # EMA9: giá vào cách EMA9 bao nhiêu (cùng đơn vị)
    tf: str = "M1"


@router.post("/order")
def q_order(o: QOrder):
    try:
        with mt5_service.session():
            a, block = _account_guard()
            if block:
                return _err(block)
            sym = _sym(o.sym)
            info = mt5.symbol_info(sym)
            lot = _norm_lot(info, o.lot)
            tk = mt5.symbol_info_tick(sym)
            if not tk or tk.bid <= 0:
                return _err(f"Chưa có giá {sym} (thị trường có thể đang đóng).")
            buy = o.side.upper() == "BUY"
            u, d = _unit(info), info.digits
            mind = (info.trade_stops_level + 1) * info.point
            mkt = tk.ask if buy else tk.bid

            if o.mode == "now":
                entry, otype, action = mkt, (mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL), mt5.TRADE_ACTION_DEAL
            else:
                e9, hi, lo = _levels(sym, o.tf)
                if e9 is None:
                    return _err("Chưa đủ nến để tính mức vào.")
                if o.mode == "ema9":
                    entry = e9 + o.off * u if buy else e9 - o.off * u
                elif o.mode == "zone":
                    entry = lo if buy else hi
                else:
                    return _err("Chế độ không hợp lệ.")
                entry = round(entry, d)
                if abs(entry - mkt) < mind:
                    return _err(f"Giá vào {entry} quá sát giá hiện tại {mkt}, dùng chế độ NGAY.")
                if o.mode == "zone" and ((buy and entry > mkt) or (not buy and entry < mkt)):
                    return _err(f"Giá đã vượt cản {'Thấp' if buy else 'Cao'} 10 nến ({entry}), không đặt lệnh chờ.")
                below = entry < mkt
                otype = (mt5.ORDER_TYPE_BUY_LIMIT if below else mt5.ORDER_TYPE_BUY_STOP) if buy else \
                        (mt5.ORDER_TYPE_SELL_STOP if below else mt5.ORDER_TYPE_SELL_LIMIT)
                action = mt5.TRADE_ACTION_PENDING

            req = {"action": action, "symbol": sym, "volume": lot, "type": otype, "price": round(entry, d),
                   "deviation": DEVIATION, "magic": MAGIC, "comment": COMMENT, "type_time": mt5.ORDER_TIME_GTC,
                   "type_filling": _filling(info) if action == mt5.TRADE_ACTION_DEAL else mt5.ORDER_FILLING_RETURN}
            if o.sl > 0:
                dist = max(o.sl * u, mind)
                req["sl"] = round(entry - dist if buy else entry + dist, d)
            if o.tp > 0:
                dist = max(o.tp * u, mind)
                req["tp"] = round(entry + dist if buy else entry - dist, d)
            res = _send(req)
            kind = {mt5.TRADE_ACTION_DEAL: "Đã vào"}.get(action, "Đã đặt lệnh chờ")
            return _ok(ticket=res.order, gia=res.price or req["price"], lot=res.volume or lot,
                       sl=req.get("sl"), tp=req.get("tp"), msg=f"{kind} {o.side.upper()} {lot} lot @ {res.price or req['price']}")
    except Exception as e:
        return _err(str(e))


# ---------- Quản lý lệnh ----------

class QSym(BaseModel):
    sym: str
    ticket: int | None = None


def _positions(sym, ticket=None):
    return [p for p in (mt5.positions_get(symbol=sym) or []) if p.magic == MAGIC and (not ticket or p.ticket == ticket)]


def _close_vol(p, vol):
    info = mt5.symbol_info(p.symbol)
    tk = mt5.symbol_info_tick(p.symbol)
    buy = p.type == mt5.POSITION_TYPE_BUY
    return _send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "position": p.ticket, "volume": vol,
                  "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "price": tk.bid if buy else tk.ask,
                  "deviation": DEVIATION, "magic": MAGIC, "comment": COMMENT, "type_time": mt5.ORDER_TIME_GTC,
                  "type_filling": _filling(info)})


def _half_vol(p):
    info = mt5.symbol_info(p.symbol)
    step = info.volume_step or 0.01
    half = round(round(p.volume / 2 / step) * step, 2)
    if half < info.volume_min or p.volume - half < info.volume_min:
        return None
    return half


_sltp_done = set()


def _auto_sltp(p, g):
    """Lệnh vào tay chưa có SL/TP: gắn theo khoảng cách trong ô Dừng lỗ / Chốt lời của buồng lái."""
    info, tk = mt5.symbol_info(p.symbol), mt5.symbol_info_tick(p.symbol)
    if not info or not tk or tk.bid <= 0:
        return
    u, d = _unit(info), info.digits
    mind = (info.trade_stops_level + 1) * info.point
    buy = p.type == mt5.POSITION_TYPE_BUY
    px = tk.bid if buy else tk.ask
    sl, tp = p.sl, p.tp
    if not sl and g.get("sl", 0) > 0:
        sl = round(p.price_open - g["sl"] * u if buy else p.price_open + g["sl"] * u, d)
        if (buy and px - sl < mind) or (not buy and sl - px < mind):     # giá đã chạy quá SL dự kiến: đặt sát nhất có thể
            sl = round(px - mind if buy else px + mind, d)
    if not tp and g.get("tp", 0) > 0:
        tp = round(p.price_open + g["tp"] * u if buy else p.price_open - g["tp"] * u, d)
        if (buy and tp - px < mind) or (not buy and px - tp < mind):
            tp = p.tp
    if sl == p.sl and tp == p.tp:
        return
    _send({"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": p.ticket, "sl": sl, "tp": tp})
    _notify(f"📱 Lệnh vào tay {p.symbol} #{p.ticket} {'BUY' if buy else 'SELL'} {p.volume} lot → tự gắn SL {sl or '-'} · TP {tp or '-'}")


def _set_sl(p, sl):
    return _send({"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": p.ticket,
                  "sl": round(sl, mt5.symbol_info(p.symbol).digits), "tp": p.tp, "magic": MAGIC})


@router.post("/half")
def q_half(x: QSym):
    try:
        with mt5_service.session():
            _, block = _account_guard()
            if block:
                return _err(block)
            sym = _sym(x.sym)
            done, skip = [], []
            for p in _positions(sym, x.ticket):
                h = _half_vol(p)
                if not h:
                    skip.append(f"#{p.ticket} ({p.volume} lot)")
                    continue
                _close_vol(p, h)
                done.append(f"#{p.ticket} chốt {h}/{p.volume} lot")
            if not done:
                return _err("Không chốt được 50%: " + (", ".join(skip) + " quá nhỏ để chia đôi." if skip else "không có lệnh mở."))
            return _ok(msg="; ".join(done) + (f" · bỏ qua {', '.join(skip)} (quá nhỏ)" if skip else ""))
    except Exception as e:
        return _err(str(e))


@router.post("/closeall")
def q_closeall(x: QSym):
    try:
        with mt5_service.session():
            _, block = _account_guard()
            if block:
                return _err(block)
            sym = _sym(x.sym)
            ps = _positions(sym, x.ticket)
            for p in ps:
                _close_vol(p, p.volume)
            return _ok(msg=f"Đã đóng {len(ps)} lệnh.") if ps else _err("Không có lệnh mở.")
    except Exception as e:
        return _err(str(e))


@router.post("/cancel")
def q_cancel(x: QSym):
    try:
        with mt5_service.session():
            _sym(x.sym)
            os_ = [o for o in (mt5.orders_get(symbol=x.sym.upper()) or []) if o.magic == MAGIC and (not x.ticket or o.ticket == x.ticket)]
            for o in os_:
                _send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            return _ok(msg=f"Đã hủy {len(os_)} lệnh chờ.") if os_ else _err("Không có lệnh chờ.")
    except Exception as e:
        return _err(str(e))


@router.post("/be")
def q_be(x: QSym):
    """Dời SL hòa vốn ngay (giá vào + khóa) cho các lệnh đang lãi đủ xa."""
    try:
        with mt5_service.session():
            _, block = _account_guard()
            if block:
                return _err(block)
            sym = _sym(x.sym)
            info = mt5.symbol_info(sym)
            tk = mt5.symbol_info_tick(sym)
            g, u = _guard(sym), _unit(info)
            mind = (info.trade_stops_level + 1) * info.point
            done, skip = [], []
            for p in _positions(sym, x.ticket):
                buy = p.type == mt5.POSITION_TYPE_BUY
                sl = p.price_open + g["be_lock"] * u if buy else p.price_open - g["be_lock"] * u
                px = tk.bid if buy else tk.ask
                if (buy and px - sl < mind) or (not buy and sl - px < mind):
                    skip.append(f"#{p.ticket}")
                    continue
                _set_sl(p, sl)
                done.append(f"#{p.ticket} SL → {round(sl, info.digits)}")
            if not done:
                return _err("Chưa dời được: " + (", ".join(skip) + " chưa lãi đủ để khóa hòa vốn." if skip else "không có lệnh mở."))
            return _ok(msg="; ".join(done))
    except Exception as e:
        return _err(str(e))


class QGuard(BaseModel):
    sym: str
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


@router.post("/guard")
def q_guard(x: QGuard):
    sym = x.sym.upper()
    if sym not in CKG_SYMBOLS:
        return _err("Mã chưa hỗ trợ.")
    g = _guard(sym)
    for k, v in x.model_dump(exclude={"sym"}).items():
        if v is not None:
            if isinstance(v, float) and not (0 <= v <= 1000):
                return _err(f"{k} không hợp lệ.")
            g[k] = v
    if g["be_lock"] >= g["be_at"]:
        return _err("Mức khóa hòa vốn phải nhỏ hơn mức kích hoạt.")
    allg = _guards()
    allg[sym] = g
    GUARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    GUARD_FILE.write_text(json.dumps(allg), encoding="utf-8")
    return _ok(guard=g)


# ---------- Lá chắn chạy nền ----------

BOT_MAGIC = 260117


def _bot_guard():
    try:
        from app.api.ckg_auto import _cfg
        c = _cfg()
        return {} if c.get("strategy") == "dca" else c     # DCA tự quản SL/TP theo giá trung bình, không dùng BE/trailing $
    except Exception:
        return {}


def _guard_step():
    bg = _bot_guard()
    allg = _guards()
    man = {s for s, v in allg.items() if v.get("manual")}          # mã bật lá chắn cho lệnh vào tay
    ps = [p for p in (mt5.positions_get() or [])
          if p.magic in (MAGIC, BOT_MAGIC) or (p.magic == 0 and p.symbol in man and not (p.comment or "").startswith("PinYin AUTO"))]
    live = {p.ticket for p in ps}
    _half_done.intersection_update(live)
    _sltp_done.intersection_update(live)
    for p in ps:
        g = bg if p.magic == BOT_MAGIC else _guard(p.symbol)
        if p.magic == 0 and g.get("auto_sltp") and p.ticket not in _sltp_done and (p.sl == 0 or p.tp == 0):
            _sltp_done.add(p.ticket)
            _auto_sltp(p, g)
            continue                                                 # vòng sau đọc lại lệnh với SL/TP mới
        if not (g.get("be_on") or g.get("tr_on")):
            continue
        info = mt5.symbol_info(p.symbol)
        tk = mt5.symbol_info_tick(p.symbol)
        if not info or not tk or tk.bid <= 0:
            continue
        u, d = _unit(info), info.digits
        mind = (info.trade_stops_level + 1) * info.point
        buy = p.type == mt5.POSITION_TYPE_BUY
        px = tk.bid if buy else tk.ask
        gain = (px - p.price_open) / u if buy else (p.price_open - px) / u
        better = (lambda new: new > p.sl + info.point / 2) if buy else (lambda new: p.sl == 0 or new < p.sl - info.point / 2)
        room = (lambda new: px - new >= mind) if buy else (lambda new: new - px >= mind)

        if g["tr_on"] and gain >= g["tr_at"]:
            if g["half"] and p.ticket not in _half_done:
                _half_done.add(p.ticket)
                h = _half_vol(p)
                if h:
                    _close_vol(p, h)
                    _notify(f"{'BOT ' if p.magic == BOT_MAGIC else ''}{p.symbol} #{p.ticket} lãi {gain:.2f} → chốt 50% ({h} lot)")
            new = round(px - g["tr_step"] * u if buy else px + g["tr_step"] * u, d)
            if better(new) and room(new):
                _set_sl(p, new)
            continue
        if g["be_on"] and gain >= g["be_at"]:
            new = round(p.price_open + g["be_lock"] * u if buy else p.price_open - g["be_lock"] * u, d)
            if better(new) and room(new):
                _set_sl(p, new)
                _notify(f"{'BOT ' if p.magic == BOT_MAGIC else ''}{p.symbol} #{p.ticket} lãi {gain:.2f} → dời SL hòa vốn {new}")


def _loop():
    err_at = 0
    while True:
        try:
            g = _guards()                       # chưa bật lá chắn nào: không gọi MT5
            bg = _bot_guard()
            if any(v.get("be_on") or v.get("tr_on") or v.get("manual") for v in g.values()) or bg.get("be_on") or bg.get("tr_on"):
                with _lock, mt5_service.session():
                    _guard_step()
        except Exception as e:
            if time.time() - err_at > 600:
                err_at = time.time()
                _notify(f"Lỗi lá chắn: {e}")
        try:
            from app.services.health import beat
            beat("guard")
        except Exception:
            pass
        time.sleep(2)


_started = False


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="pinyin-guard").start()


# ---------- Đổi tài khoản MT5 ngay trên trang (chỉ chủ) ----------
ACC_FILE = ROOT / "data" / "mt5_accounts.json"


def _recent():
    try:
        return json.loads(ACC_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _remember(a):
    lst = [x for x in _recent() if not (x["login"] == a.login and x["server"] == a.server)]
    lst.insert(0, {"login": a.login, "server": a.server, "name": a.name, "ccy": a.currency,
                   "demo": a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO, "t": int(time.time())})
    ACC_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACC_FILE.write_text(json.dumps(lst[:10], ensure_ascii=False), encoding="utf-8")   # KHÔNG lưu mật khẩu


class RealIn(BaseModel):
    on: bool
    pw: str = ""


@router.post("/real")
def q_real(x: RealIn):
    """Bật/tắt cho phép vào lệnh trên tài khoản THẬT. Bật cần nhập lại mật khẩu chủ."""
    import hmac
    import os
    from app.api.ckg_auto import _set_env
    if x.on:
        pw = os.getenv("PINYIN_PASSWORD", "")
        if not pw or not hmac.compare_digest(x.pw.encode(), pw.encode()):
            return _err("Sai mật khẩu chủ.")
    _set_env("PINYIN_ALLOW_REAL", "1" if x.on else "0")
    _notify("🔓 ĐÃ CHO PHÉP vào lệnh tài khoản THẬT." if x.on else "🔒 Đã CHẶN vào lệnh tài khoản THẬT (chỉ Demo).")
    return _ok(on=allow_real())


@router.get("/accounts")
def q_accounts():
    try:
        with mt5_service.session():
            a = mt5.account_info()
            cur = {"login": a.login, "server": a.server, "name": a.name} if a else None
            if a and not any(x["login"] == a.login and x["server"] == a.server for x in _recent()):
                _remember(a)
    except Exception:
        cur = None
    return _ok(cur=cur, recent=_recent())


class LoginIn(BaseModel):
    login: int
    password: str = ""
    server: str


def _fresh_account(a):
    """Sau khi đổi tài khoản (gọi trong phiên MT5): dò lại tên mã của sàn mới, bỏ đệm cũ, nạp sẵn nến vàng
    để trang tải lại là có biểu đồ ngay (không phải chờ bảng tên mã cũ hết hạn 60 giây)."""
    from app.services import apicache, symbols, copytrade
    symbols.reset()
    apicache.clear_all()
    try:
        symbols.mapping()
        if mt5.symbol_select("XAUUSD", True):
            for tf in (mt5.TIMEFRAME_M1, mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15):
                mt5.copy_rates_from_pos("XAUUSD", tf, 0, 300)
        if a:
            copytrade.set_master(a.login, a.server)
    except Exception:
        pass


@router.post("/login")
def q_login(x: LoginIn):
    server = x.server.strip()
    if x.login <= 0 or not server:
        return _err("Nhập số tài khoản và máy chủ.")
    try:
        with _lock, mt5_service.session():
            before = mt5.account_info()
            if before and before.login == x.login and before.server == server and not x.password:
                return _ok(msg="Đang dùng đúng tài khoản này rồi.")
            # để trống mật khẩu: dùng mật khẩu MT5 đã lưu (tài khoản từng đăng nhập trên máy này)
            ok = mt5.login(x.login, server=server, password=x.password) if x.password else mt5.login(x.login, server=server)
            if not ok:
                code, msg = mt5.last_error()
                back = ""
                if before:                          # thử quay lại tài khoản cũ (mật khẩu MT5 đã lưu)
                    if mt5.login(before.login, server=before.server):
                        back = f" Đã quay lại tài khoản cũ #{before.login}."
                    else:
                        back = f" ⚠️ Chưa quay lại được tài khoản cũ #{before.login}, đăng nhập lại trên trang này."
                hint = " Kiểm tra số tài khoản, mật khẩu, và tên máy chủ đúng như trong email của sàn." if code in (-6, -2, -10005) or "auth" in str(msg).lower() else ""
                _fresh_account(mt5.account_info())
                return _err(f"Đăng nhập thất bại ({code} {msg}).{hint}{back}")
            a = mt5.account_info()
            _remember(a)
            _fresh_account(a)
        # đổi tài khoản: tắt bot để không vào lệnh nhầm, xóa theo dõi lệnh của tài khoản cũ
        try:
            from app.api import ckg_auto
            ckg_auto.stop_all("đổi tài khoản MT5")
        except Exception:
            pass
        _half_done.clear()
        kind = "DEMO" if a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "THẬT"
        _notify(f"Đã đổi tài khoản MT5 → #{a.login} {a.server} ({kind}, {a.currency} {a.equity:.2f}). Bot tự động đã TẮT.")
        return _ok(msg=f"Đã đăng nhập #{a.login} · {a.server} · {kind} · vốn {a.equity:.2f} {a.currency}. Bot tự động đã được tắt, bật lại khi sẵn sàng.",
                   login=a.login, server=a.server, demo=kind == "DEMO")
    except Exception as e:
        return _err(str(e))
