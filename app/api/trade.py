"""Giao dịch MT5 thật từ trang chính PinYin (lệnh do người dùng xác nhận).

Dùng chung lớp bảo vệ với PinYin CKG Lab (app/api/ckg_trade.py):
  - Chỉ tài khoản DEMO, trừ khi đặt PINYIN_ALLOW_REAL=1 khi khởi động FastAPI.
  - Lot tối đa mỗi lệnh: PINYIN_MAX_LOT (mặc định 1.0).
  - Chỉ quản lý lệnh có MAGIC riêng của trang chính; lệnh vào tay hoặc lệnh CKG không bị đụng tới.
"""

from datetime import datetime, timezone

import MetaTrader5 as mt5
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.ckg_trade import ALLOW_REAL, MAX_LOT, DEVIATION, _account_guard, _filling, _norm_lot, _send
from app.services.mt5_service import mt5_service


MAGIC = 260116
COMMENT = "PinYin AI"
SYMBOLS = {"XAUUSD", "EURUSD", "GBPUSD", "GBPJPY"}

router = APIRouter(prefix="/api/v1/trade", tags=["Giao dịch MT5"])


class OrderIn(BaseModel):
    symbol: str
    side: str                 # BUY | SELL
    lot: float
    sl: float | None = None   # giá cắt lỗ (tùy chọn)
    tp: float | None = None   # giá chốt lời (tùy chọn)
    nguon: str = "tay"        # "tay" | "ai" - để ghi chú lệnh


class CloseIn(BaseModel):
    ticket: int


def _ok(**kw):
    return {"ok": True, **kw}


def _err(msg):
    return {"ok": False, "loi": msg}


def _symbol(sym):
    sym = sym.upper().strip()
    if sym not in SYMBOLS:
        raise ValueError(f"Mã {sym} chưa được hỗ trợ.")
    if not mt5.symbol_select(sym, True):
        raise ValueError(f"Broker không có mã {sym}.")
    return sym


def _my_positions():
    return [p for p in (mt5.positions_get() or []) if p.magic == MAGIC]


def _check_stops(info, buy, price, sl, tp):
    """SL/TP phải nằm đúng phía và đủ xa theo stops_level của broker."""
    min_dist = (info.trade_stops_level or 0) * info.point
    if sl:
        if buy and sl >= price - min_dist:
            raise ValueError(f"Cắt lỗ của lệnh BUY phải thấp hơn giá {price} (tối thiểu {min_dist:g}).")
        if not buy and sl <= price + min_dist:
            raise ValueError(f"Cắt lỗ của lệnh SELL phải cao hơn giá {price} (tối thiểu {min_dist:g}).")
    if tp:
        if buy and tp <= price + min_dist:
            raise ValueError(f"Chốt lời của lệnh BUY phải cao hơn giá {price}.")
        if not buy and tp >= price - min_dist:
            raise ValueError(f"Chốt lời của lệnh SELL phải thấp hơn giá {price}.")


@router.get("/account")
def account():
    try:
        with mt5_service.session():
            a, block = _account_guard()
            if a is None:
                return _err(block)
            positions = [{
                "ticket": p.ticket,
                "symbol": p.symbol,
                "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "lot": p.volume,
                "gia_vao": p.price_open,
                "gia_hien_tai": p.price_current,
                "sl": p.sl or None,
                "tp": p.tp or None,
                "lai_lo": round(p.profit + p.swap, 2),
                "ghi_chu": p.comment,
                "thoi_gian": datetime.fromtimestamp(p.time, timezone.utc).isoformat(),
            } for p in _my_positions()]
            return _ok(
                demo=a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
                cho_phep_tien_that=ALLOW_REAL,
                lot_toi_da=MAX_LOT,
                may_chu=a.server,
                tien_te=a.currency,
                so_du=a.balance,
                von=a.equity,
                ky_quy_tu_do=a.margin_free,
                duoc_giao_dich=block is None,
                ly_do_chan=block,
                lenh_dang_mo=positions,
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
            sym = _symbol(o.symbol)
            info = mt5.symbol_info(sym)
            lot = _norm_lot(info, o.lot)
            tick = mt5.symbol_info_tick(sym)
            if not tick or tick.bid <= 0:
                return _err(f"Chưa có giá {sym} (thị trường có thể đang đóng).")
            buy = side == "BUY"
            price = tick.ask if buy else tick.bid
            sl = round(o.sl, info.digits) if o.sl else 0.0
            tp = round(o.tp, info.digits) if o.tp else 0.0
            _check_stops(info, buy, price, sl, tp)
            res = _send({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": sym,
                "volume": lot,
                "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
                "price": price,
                "sl": sl,
                "tp": tp,
                "deviation": DEVIATION,
                "magic": MAGIC,
                "comment": f"{COMMENT} {'AI' if o.nguon == 'ai' else 'tay'}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling(info),
            })
            return _ok(ticket=res.order, gia=res.price, lot=res.volume, symbol=sym, side=side)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


@router.post("/close")
def close(c: CloseIn):
    try:
        with mt5_service.session():
            _, block = _account_guard()
            if block:
                return _err(block)
            pos = next((p for p in _my_positions() if p.ticket == c.ticket), None)
            if pos is None:
                return _err("Không tìm thấy lệnh PinYin với ticket này.")
            info = mt5.symbol_info(pos.symbol)
            tick = mt5.symbol_info_tick(pos.symbol)
            buy = pos.type == mt5.POSITION_TYPE_BUY
            res = _send({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "position": pos.ticket,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if buy else tick.ask,
                "deviation": DEVIATION,
                "magic": MAGIC,
                "comment": COMMENT,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling(info),
            })
            return _ok(ticket=pos.ticket, gia=res.price)
    except Exception as e:
        return _err(str(e))
