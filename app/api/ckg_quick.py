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
from app.api.ckg_trade import MAGIC, COMMENT, DEVIATION, _account_guard, _filling, _norm_lot, _send, _sym
from app.services.mt5_service import mt5_service


router = APIRouter(prefix="/ckg/api/q", tags=["PinYin CKG · buồng lái nhanh"])
ROOT = Path(__file__).resolve().parent.parent.parent
GUARD_FILE = ROOT / "data" / "guard.json"
TFS = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}
GOLD_DEF = {"be_on": False, "be_at": 1.5, "be_lock": 0.25, "tr_on": False, "tr_at": 2.2, "tr_step": 0.6, "half": True}
FX_DEF = {"be_on": False, "be_at": 15, "be_lock": 2, "tr_on": False, "tr_at": 22, "tr_step": 6, "half": True}
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
                            "demo": a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO} if a else None)
    except Exception as e:
        return _err(str(e))


@router.get("/book")
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
        return _cfg()
    except Exception:
        return {}


def _guard_step():
    bg = _bot_guard()
    ps = [p for p in (mt5.positions_get() or []) if p.magic in (MAGIC, BOT_MAGIC)]
    live = {p.ticket for p in ps}
    _half_done.intersection_update(live)
    for p in ps:
        g = bg if p.magic == BOT_MAGIC else _guard(p.symbol)
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
            if any(v.get("be_on") or v.get("tr_on") for v in g.values()) or bg.get("be_on") or bg.get("tr_on"):
                with _lock, mt5_service.session():
                    _guard_step()
        except Exception as e:
            if time.time() - err_at > 600:
                err_at = time.time()
                _notify(f"Lỗi lá chắn: {e}")
        time.sleep(2)


_started = False


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="pinyin-guard").start()
