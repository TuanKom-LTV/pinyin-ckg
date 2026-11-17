"""PinYin CKG Lab: phục vụ trang /ckg/ và dữ liệu MT5 thật (chỉ đọc).

Trang CKG Lab gọi hai đường dẫn tương đối:
  - api/status            -> máy chủ có hoạt động và MT5 có kết nối không
  - api/data?sym=&since=  -> nến M5 + M1 của tuần hiện tại (tuần bắt đầu T2 04:00 giờ VN)

Mọi mốc thời gian trả về là giây UTC thật (đã trừ lệch múi giờ máy chủ broker).
Không gửi lệnh giao dịch nào sang MT5.
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, RedirectResponse

from app.services.mt5_service import mt5_service


BASE_DIR = Path(__file__).resolve().parent.parent.parent
CKG_PAGE = BASE_DIR / "frontend" / "ckg" / "index.html"
OFFSET_FILE = BASE_DIR / "data" / "mt5_server_offset.txt"

CKG_SYMBOLS = {"GBPUSD", "EURUSD", "AUDUSD", "GBPJPY", "EURJPY", "XAUUSD"}
VN = timezone(timedelta(hours=7))
NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

router = APIRouter(tags=["PinYin CKG Lab"])


# ---------- Lệch giờ máy chủ broker so với UTC ----------

def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _ny_close_offset(utc_now):
    """Quy ước phổ biến của broker: giờ máy chủ = giờ New York + 7h
    (UTC+3 khi Mỹ dùng giờ mùa hè, UTC+2 khi không)."""
    y = utc_now.year
    start = _nth_sunday(y, 3, 2).replace(hour=7, tzinfo=timezone.utc)   # 2h sáng NY
    end = _nth_sunday(y, 11, 1).replace(hour=6, tzinfo=timezone.utc)
    return 3 * 3600 if start <= utc_now < end else 2 * 3600


def _cached_offset():
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return None


def server_offset():
    """Trả về (lệch giây, nguồn). Đo từ tick mới nếu có, không thì dùng giá trị đã lưu
    hoặc quy ước New York + 7h."""
    now = time.time()
    for sym in ("EURUSD", "GBPUSD", "XAUUSD"):
        tick = mt5.symbol_info_tick(sym)
        if not tick or tick.time <= 0:
            continue
        diff = tick.time - now
        rounded = round(diff / 1800) * 1800
        if abs(diff - rounded) <= 120 and abs(rounded) <= 14 * 3600:
            if _cached_offset() != rounded:
                try:
                    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
                    OFFSET_FILE.write_text(str(rounded))
                except Exception:
                    pass
            return rounded, "do_tu_tick"

    utc_now = datetime.now(timezone.utc)
    rule = _ny_close_offset(utc_now)
    cached = _cached_offset()
    # Giá trị đã lưu chỉ dùng nếu cùng mùa (lệch đúng 2h hoặc 3h như quy ước) hoặc broker khác quy ước.
    if cached is not None and cached not in (2 * 3600, 3 * 3600):
        return cached, "da_luu"
    return rule, "quy_uoc_new_york"


# ---------- Mốc tuần giống hệt trang (T2 04:00 giờ VN) ----------

def week_start_utc():
    vn_now = datetime.now(VN)
    monday = (vn_now - timedelta(days=vn_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((monday + timedelta(hours=4)).timestamp())


def _rows(rates, off, since):
    out = []
    if rates is None:
        return out
    for r in rates:
        t = int(r["time"]) - off
        if t >= since:
            out.append([
                t,
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
            ])
    return out


def _last_closed(timeframe, sym, off, length_sec):
    """Nến D1/W1 đã đóng gần nhất theo nến của broker."""
    rates = mt5.copy_rates_from_pos(sym, timeframe, 0, 2)
    if rates is None or len(rates) == 0:
        return None
    server_now = time.time() + off
    last = rates[-1]
    if int(last["time"]) + length_sec > server_now and len(rates) > 1:
        last = rates[-2]
    return last


CHART_TF = {
    "M1": (mt5.TIMEFRAME_M1, 7500), "M3": (mt5.TIMEFRAME_M3, 2600), "M5": (mt5.TIMEFRAME_M5, 1600),
    "M15": (mt5.TIMEFRAME_M15, 600), "M30": (mt5.TIMEFRAME_M30, 320), "H1": (mt5.TIMEFRAME_H1, 240),
    "H4": (mt5.TIMEFRAME_H4, 200), "D": (mt5.TIMEFRAME_D1, 250), "W": (mt5.TIMEFRAME_W1, 150),
}


# ---------- Đường dẫn ----------

@router.get("/ckg", include_in_schema=False)
def ckg_redirect():
    return RedirectResponse("/ckg/")


@router.get("/ckg/", include_in_schema=False)
def ckg_page():
    return FileResponse(CKG_PAGE, headers=NO_CACHE)


@router.get("/ckg/api/status")
def ckg_status():
    try:
        with mt5_service.session():
            info = mt5.terminal_info()
            off, src = server_offset()
            return {
                "ok": True,
                "mt5": bool(info and info.connected),
                "lech_gio_may_chu_giay": off,
                "nguon_lech_gio": src,
                "now": int(time.time() * 1000),
            }
    except Exception as e:
        # Máy chủ vẫn sống, trang vẫn bật chế độ LIVE và báo chờ dữ liệu.
        return {"ok": True, "mt5": False, "loi": str(e), "now": int(time.time() * 1000)}


def _mid(sym):
    if not mt5.symbol_select(sym, True):
        return None
    t = mt5.symbol_info_tick(sym)
    return (t.bid + t.ask) / 2 if t and t.bid > 0 and t.ask > 0 else None


@router.get("/ckg/api/symbols")
def ckg_symbols():
    """Bảng dịch tên cặp tiền: tên chuẩn PinYin → tên thật trên sàn đang đăng nhập."""
    from app.services.symbols import mapping
    try:
        with mt5_service.session():
            a = mt5.account_info()
            return {"ok": True, "server": a.server if a else None, "map": mapping()}
    except Exception as e:
        return {"ok": False, "loi": str(e)}


@router.get("/ckg/api/rates")
def ckg_rates(ccy: str = "USD"):
    """Tỷ giá thật để tính giá trị pip (cặp JPY) và quy đổi vốn tài khoản sang USD."""
    ccy = ccy.upper().strip()[:3]
    try:
        with mt5_service.session():
            usdjpy = _mid("USDJPY")
            to_usd = 1.0
            if ccy != "USD":
                direct, inverse = _mid(ccy + "USD"), _mid("USD" + ccy)
                to_usd = direct if direct else (1 / inverse if inverse else None)
            return {"ok": True, "usdjpy": usdjpy, "ccy": ccy, "to_usd": to_usd}
    except Exception as e:
        return {"ok": False, "loi": str(e)}


@router.get("/ckg/api/hapro")
def ckg_hapro(sym: str = "XAUUSD", tf: str = "M5", candles: str = "ha"):
    """Nhãn BUY/SELL của chiến lược PinYin HA Pro trên 1500 nến đã đóng gần nhất (để vẽ lên biểu đồ)."""
    from app.services.ha_pro import analyze
    sym, tf = sym.upper().strip(), tf.upper().strip()
    if sym not in CKG_SYMBOLS or tf not in CHART_TF:
        return {"ok": False, "loi": "Mã hoặc khung chưa hỗ trợ."}
    try:
        with mt5_service.session():
            if not mt5.symbol_select(sym, True):
                return {"ok": False, "loi": f"Broker không có mã {sym}."}
            off, _ = server_offset()
            r = mt5.copy_rates_from_pos(sym, CHART_TF[tf][0], 1, 1500)
        if r is None or len(r) < 200:
            return {"ok": True, "sig": []}
        bars = [(int(x["time"]) - off, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                 float(x["tick_volume"]), int(x["time"]) // 86400) for x in r]
        sig = analyze(bars, {"candles": "std" if candles == "std" else "ha"})
        return {"ok": True, "sym": sym, "tf": tf, "candles": candles,
                "sig": [{"t": s["t"], "side": s["side"], "kind": s["kind"], "e": round(s["e"], 5), "sl": round(s["sl"], 5),
                         "tp": [round(float(x), 5) for x in s["tp"]]} for s in sig]}
    except Exception as e:
        return {"ok": False, "loi": str(e)}


@router.get("/ckg/api/chart")
def ckg_chart(sym: str = "GBPUSD", tf: str = "M5", since: int = Query(default=0, ge=0)):
    """Nến biểu đồ: [[giây UTC, o, h, l, c], ...]. since > 0: chỉ trả nến từ mốc đó (cập nhật nhanh)."""
    sym, tf = sym.upper().strip(), tf.upper().strip()
    if sym not in CKG_SYMBOLS or tf not in CHART_TF:
        return {"ok": False, "loi": "Mã hoặc khung chưa hỗ trợ."}
    code, n = CHART_TF[tf]
    try:
        with mt5_service.session():
            if not mt5.symbol_select(sym, True):
                return {"ok": False, "loi": f"Broker không có mã {sym}."}
            off, _ = server_offset()
            if since:
                frm = datetime.fromtimestamp(since + off, timezone.utc)
                to = datetime.fromtimestamp(time.time() + off + 86400 * 8, timezone.utc)
                rates = mt5.copy_rates_range(sym, code, frm, to)
            else:
                rates = mt5.copy_rates_from_pos(sym, code, 0, n)
            rows = _rows(rates, off, 0)
            return {"ok": True, "sym": sym, "tf": tf, "rows": rows}
    except Exception as e:
        return {"ok": False, "loi": str(e)}


@router.get("/ckg/api/data")
def ckg_data(sym: str = "GBPUSD", since: int = Query(default=0, ge=0)):
    sym = sym.upper().strip()
    if sym not in CKG_SYMBOLS:
        return {"ok": False, "loi": f"Mã {sym} chưa được hỗ trợ."}

    try:
        with mt5_service.session():
            if not mt5.symbol_select(sym, True):
                return {"ok": False, "loi": f"Broker không có mã {sym}."}

            off, off_src = server_offset()
            ws = week_start_utc()
            # Từ T2 00:00 tới lúc thị trường mở cửa, tuần mới chưa có nến:
            # tiếp tục dùng tuần trước để trang không rơi về dữ liệu giả lập.
            probe = mt5.copy_rates_range(
                sym, mt5.TIMEFRAME_M5,
                datetime.fromtimestamp(ws + off, timezone.utc),
                datetime.fromtimestamp(time.time() + off + 3600, timezone.utc),
            )
            if probe is None or len(probe) == 0:
                ws -= 7 * 86400
            start = max(ws, since)
            # copy_rates_range nhận mốc theo giờ máy chủ broker.
            frm = datetime.fromtimestamp(start + off, timezone.utc)
            to = datetime.fromtimestamp(time.time() + off + 3600, timezone.utc)

            m5 = _rows(mt5.copy_rates_range(sym, mt5.TIMEFRAME_M5, frm, to), off, start)
            m1 = _rows(mt5.copy_rates_range(sym, mt5.TIMEFRAME_M1, frm, to), off, start)

            if not m5 and not since:
                return {"ok": False, "loi": f"MT5 chưa có nến {sym} trong tuần này."}

            tick = mt5.symbol_info_tick(sym)
            if tick and tick.time > 0:
                upd = int(tick.time_msc) - off * 1000
            elif m5:
                upd = (m5[-1][0] + 300) * 1000
            else:
                upd = 0

            js = {
                "ok": True,
                "sym": sym,
                "ws": ws,
                "m5": m5,
                "m1": m1,
                "upd": upd,
                "now": int(time.time() * 1000),
                "lech_gio_may_chu_giay": off,
                "nguon_lech_gio": off_src,
            }
            if tick and tick.bid > 0:
                js["ba"] = [tick.bid, tick.ask]

            d1 = _last_closed(mt5.TIMEFRAME_D1, sym, off, 86400)
            w1 = _last_closed(mt5.TIMEFRAME_W1, sym, off, 7 * 86400)
            if d1 is not None:
                js["d1"] = [float(d1["open"]), float(d1["high"]),
                            float(d1["low"]), float(d1["close"])]
                # Ngày của nến D1 theo lịch broker (nến D1 broker mở lúc 00:00 giờ máy chủ)
                js["d1t"] = datetime.fromtimestamp(int(d1["time"]), timezone.utc).strftime("%Y-%m-%d")
            if w1 is not None:
                # Trang so sánh w1[1] với w1[0] để xác định nến W xanh/đỏ.
                js["w1"] = [float(w1["open"]), float(w1["close"])]
                js["w1t"] = datetime.fromtimestamp(int(w1["time"]), timezone.utc).strftime("%Y-%m-%d")

            return js

    except Exception as e:
        return {"ok": False, "loi": str(e)}
