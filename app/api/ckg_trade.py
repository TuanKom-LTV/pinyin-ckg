"""PinYin CKG Lab: buồng lái gửi lệnh thật sang MT5.

An toàn:
  - Chỉ quản lý lệnh có MAGIC của PinYin (lệnh vào tay trên MT5 không bị đụng tới).
  - Mặc định chỉ cho phép tài khoản DEMO. Muốn dùng tài khoản thật phải đặt biến môi trường
    PINYIN_ALLOW_REAL=1 trước khi khởi động FastAPI.
  - Lot tối đa mỗi lệnh: PINYIN_MAX_LOT (mặc định 1.0) để chặn gõ nhầm.
"""

import os
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.ckg import CKG_SYMBOLS, server_offset, week_start_utc
from app.services.mt5_service import mt5_service


MAGIC = 260115
COMMENT = "PinYin CKG"
DEVIATION = 20
ALLOW_REAL = os.getenv("PINYIN_ALLOW_REAL") == "1"
MAX_LOT = float(os.getenv("PINYIN_MAX_LOT", "1.0"))

router = APIRouter(prefix="/ckg/api", tags=["PinYin CKG Lab · giao dịch"])


class OrderIn(BaseModel):
    sym: str
    side: str          # BUY | SELL
    lot: float
    tp_pip: float | None = None   # đặt TP trên MT5 cách giá vào N pip (None/0 = không đặt)
    sl_pip: float | None = None   # đặt SL trên MT5 cách giá vào N pip (None/0 = không đặt)


class CloseIn(BaseModel):
    sym: str
    ticket: int | None = None   # có ticket: chỉ chốt đúng lệnh đó; không có: chốt tất cả lệnh PinYin của mã


class ReverseIn(BaseModel):
    sym: str
    mult: float = 2
    tp_pip: float | None = None
    sl_pip: float | None = None


# ---------- Hỗ trợ ----------

def _account_guard():
    a = mt5.account_info()
    t = mt5.terminal_info()
    if a is None:
        return None, "Không đọc được tài khoản MT5."
    if a.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not ALLOW_REAL:
        return a, "Tài khoản MT5 là tài khoản THẬT. Máy chủ đang chặn lệnh (đặt PINYIN_ALLOW_REAL=1 để cho phép)."
    if not a.trade_allowed or not a.trade_expert:
        return a, "Tài khoản không cho phép giao dịch tự động."
    if not t or not t.trade_allowed:
        return a, "MT5 đang tắt nút Algo Trading. Bật lên rồi thử lại."
    return a, None


def _sym(sym):
    sym = sym.upper().strip()
    if sym not in CKG_SYMBOLS:
        raise ValueError(f"Mã {sym} chưa được hỗ trợ.")
    if not mt5.symbol_select(sym, True):
        raise ValueError(f"Broker không có mã {sym}.")
    return sym


def _filling(info):
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _norm_lot(info, lot):
    step = info.volume_step or 0.01
    lot = round(round(lot / step) * step, 2)
    if lot < info.volume_min:
        raise ValueError(f"Lot tối thiểu là {info.volume_min}.")
    if lot > min(info.volume_max, MAX_LOT):
        raise ValueError(f"Lot {lot} vượt giới hạn {min(info.volume_max, MAX_LOT)}.")
    return lot


def _send(req):
    res = mt5.order_send(req)
    if res is None:
        raise RuntimeError(f"MT5 không phản hồi: {mt5.last_error()}")
    if res.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
        raise RuntimeError(f"MT5 từ chối lệnh (mã {res.retcode}): {res.comment}")
    return res


def _open(sym, side, lot, tp_pip=None, sl_pip=None):
    info = mt5.symbol_info(sym)
    lot = _norm_lot(info, lot)
    tick = mt5.symbol_info_tick(sym)
    if not tick or tick.bid <= 0:
        raise RuntimeError(f"Chưa có giá {sym} (thị trường có thể đang đóng).")
    buy = side == "BUY"
    price = tick.ask if buy else tick.bid
    pip = info.point * 10                 # 1 pip = 10 point (FX 5/3 số lẻ, XAUUSD 0.1)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "deviation": DEVIATION,
        "magic": MAGIC,
        "comment": COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling(info),
    }
    min_dist = (info.trade_stops_level + 1) * info.point
    if tp_pip and tp_pip > 0:
        d = max(tp_pip * pip, min_dist)
        req["tp"] = round(price + d if buy else price - d, info.digits)
    if sl_pip and sl_pip > 0:
        d = max(sl_pip * pip, min_dist)
        req["sl"] = round(price - d if buy else price + d, info.digits)
    res = _send(req)
    return {"ticket": res.order, "gia": res.price, "lot": res.volume, "tp": req.get("tp"), "sl": req.get("sl")}


def _my_positions(sym=None):
    ps = mt5.positions_get(symbol=sym) if sym else mt5.positions_get()
    return [p for p in (ps or []) if p.magic == MAGIC]


def _close(sym, ticket=None):
    info = mt5.symbol_info(sym)
    done = []
    for p in _my_positions(sym):
        if ticket and p.ticket != ticket:
            continue
        tick = mt5.symbol_info_tick(sym)
        buy = p.type == mt5.POSITION_TYPE_BUY
        res = _send({
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "position": p.ticket,
            "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if buy else tick.ask,
            "deviation": DEVIATION,
            "magic": MAGIC,
            "comment": COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling(info),
        })
        done.append({"ticket": p.ticket, "gia": res.price, "lot": p.volume,
                     "side": "BUY" if buy else "SELL"})
    return done


def _ok(**kw):
    return {"ok": True, **kw}


def _err(msg):
    return {"ok": False, "loi": msg}


# ---------- Đường dẫn ----------

@router.get("/account")
def account():
    try:
        with mt5_service.session():
            a, block = _account_guard()
            if a is None:
                return _err(block)
            off, _ = server_offset()
            pos = [{
                "ticket": p.ticket,
                "sym": p.symbol,
                "t": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "lot": p.volume,
                "e": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit + p.swap,
                "ms": int(p.time_msc) - off * 1000,
            } for p in _my_positions()]
            # Lệnh không phải của buồng lái CKG (vào tay trên MT5, trang chính...): chỉ để xem
            other = [{
                "sym": p.symbol,
                "t": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "lot": p.volume,
                "e": p.price_open,
                "profit": p.profit + p.swap,
                "nguon": "vào tay" if p.magic == 0 else ("trang chính" if p.magic == 260116 else f"magic {p.magic}"),
            } for p in (mt5.positions_get() or []) if p.magic != MAGIC]
            return _ok(
                demo=a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
                server=a.server,
                currency=a.currency,
                balance=a.balance,
                equity=a.equity,
                margin_free=a.margin_free,
                trade=block is None,
                chan=block,
                positions=pos,
                khac=other,
            )
    except Exception as e:
        return _err(str(e))


@router.post("/order")
def order(o: OrderIn):
    side = o.side.upper()
    if side not in ("BUY", "SELL"):
        return _err("Loại lệnh phải là BUY hoặc SELL.")
    try:
        with mt5_service.session():
            _, block = _account_guard()
            if block:
                return _err(block)
            return _ok(mo=_open(_sym(o.sym), side, o.lot, o.tp_pip, o.sl_pip))
    except Exception as e:
        return _err(str(e))


@router.post("/close")
def close(c: CloseIn):
    try:
        with mt5_service.session():
            _, block = _account_guard()
            if block:
                return _err(block)
            done = _close(_sym(c.sym), c.ticket)
            if not done:
                return _err(f"Không tìm thấy lệnh #{c.ticket} của PinYin trên {c.sym} (có thể đã đóng)." if c.ticket else "Không có lệnh PinYin nào đang mở trên mã này.")
            return _ok(dong=done)
    except Exception as e:
        return _err(str(e))


@router.post("/reverse")
def reverse(r: ReverseIn):
    try:
        with mt5_service.session():
            _, block = _account_guard()
            if block:
                return _err(block)
            sym = _sym(r.sym)
            ps = _my_positions(sym)
            if not ps:
                return _err("Không có lệnh PinYin nào đang mở trên mã này.")
            side = "SELL" if ps[0].type == mt5.POSITION_TYPE_BUY else "BUY"
            lot = sum(p.volume for p in ps) * r.mult
            info = mt5.symbol_info(sym)
            lot = _norm_lot(info, lot)          # kiểm tra trước khi đóng lệnh cũ
            done = _close(sym)
            return _ok(dong=done, mo=_open(sym, side, lot, r.tp_pip, r.sl_pip))
    except Exception as e:
        return _err(str(e))


@router.get("/history")
def history():
    """Các lệnh PinYin đã đóng trong tuần (giống tab History của MT5)."""
    try:
        with mt5_service.session():
            off, _ = server_offset()
            frm = datetime.fromtimestamp(week_start_utc() + off - 86400, timezone.utc)
            to = datetime.fromtimestamp(time.time() + off + 86400, timezone.utc)
            deals = [d for d in (mt5.history_deals_get(frm, to) or []) if d.magic == MAGIC]
            ins = {d.position_id: d for d in deals if d.entry == mt5.DEAL_ENTRY_IN}
            rows = []
            for d in deals:
                if d.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT):
                    continue
                i = ins.get(d.position_id)
                rows.append({
                    "id": d.position_id,
                    "sym": d.symbol,
                    "t": ("BUY" if i.type == mt5.DEAL_TYPE_BUY else "SELL") if i
                         else ("SELL" if d.type == mt5.DEAL_TYPE_BUY else "BUY"),
                    "lot": d.volume,
                    "e": i.price if i else None,
                    "x": d.price,
                    "oms": (int(i.time_msc) - off * 1000) if i else None,
                    "cms": int(d.time_msc) - off * 1000,
                    "pf": d.profit + d.swap + d.commission + (i.commission if i else 0),
                })
            rows.sort(key=lambda r: r["cms"])
            return _ok(rows=rows)
    except Exception as e:
        return _err(str(e))
