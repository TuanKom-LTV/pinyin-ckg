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

Chiến lược "PinYin HA Pro DCA" (strategy = "dca"): chiến dịch tối đa N lệnh cùng chiều, SL chung, chốt TP1/TP2
từng phần theo GIÁ TRUNG BÌNH, sau TP1 dời SL về hòa vốn, phần còn lại trail EMA21; thoát khi SL mềm VWAP,
hết giờ, tín hiệu ngược (đảo chiều luôn). Trạng thái chiến dịch lưu ở data/auto_dca_state.json.
"""

import http.client
import json
import math
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
from app.services.ha_pro import analyze as analyze_ha
from app.services.ha_pro_dca import compute as dca_compute


SYM = "XAUUSD"
MAGIC = 260117
COMMENT = "PinYin AUTO"
TFS = {"M1": (mt5.TIMEFRAME_M1, 60), "M5": (mt5.TIMEFRAME_M5, 300), "M15": (mt5.TIMEFRAME_M15, 900)}
STRATS = {"ema": "EMA9 × EMA21", "ha": "PinYin HA Pro", "dca": "PinYin HA Pro DCA"}
TIERS = [(100, 3, .01), (300, 5, .01), (600, 3, .02), (900, 5, .02), (1000, 3, .03), (1200, 5, .03)]
VN = timezone(timedelta(hours=7))
ROOT = Path(__file__).resolve().parent.parent.parent
CFG_FILE = ROOT / "data" / "auto_xau.json"
LOG_FILE = ROOT / "data" / "auto_xau_log.jsonl"
DCA_FILE = ROOT / "data" / "auto_dca_state.json"
ENV_FILE = ROOT / ".env"
LOOP_SEC = 2

router = APIRouter(prefix="/ckg/api", tags=["PinYin CKG · bot tự động"])

DEFAULT = {"on": False, "tf": "M1", "step": 2.0, "tp": 1,
           # lá chắn cho bot: SL/TP theo tín hiệu hoặc cố định $, hòa vốn tự động, trailing + chốt 50%
           "sl_mode": "4n", "sl_usd": 3.0, "tp_mode": "step", "tp_usd": 5.0,
           "be_on": False, "be_at": 1.5, "be_lock": 0.25, "tr_on": False, "tr_at": 2.2, "tr_step": 0.6, "half": True,
           "lot": None,   # lot cố định; None = theo bảng vốn
           "strategy": "ema", "ha_sl_buffer": 2.0, "ha_sl_lookback": 20,   # chiến lược tín hiệu
           "ha_candles": "ha",   # HA Pro tính trên nến Heikin Ashi (như TradingView) hoặc nến Nhật
           "dca": {}}            # cài đặt chiến lược DCA (gộp với DCA_DEF)
# Cài đặt DCA mặc định = input mặc định của Pine Script. Khóa: (min, max, kiểu) để kiểm tra khi lưu.
DCA_DEF = {"legs": 3, "gap": 0.5, "min_bars": 5, "risk": 0.0, "dir": "both", "stretch": 2.0,
           "sl_mode": "min", "sl_k": 0.5, "sl_buf": 1.0, "soft": True, "soft_sig": 1.0,
           "tp1r": 1.0, "tp1p": 50, "tp2r": 2.0, "tp2p": 30, "cap": True, "be": True, "be_off": 0.0,
           "trail": True, "trail_after": 2, "time": 60, "rev": True, "candles": "std",
           "sess_on": False, "sess": "1400-2300"}   # chỉ MỞ/THÊM lệnh trong khung giờ VN (như useSession của Pine)
DCA_NUM = {"legs": (1, 5, int), "gap": (0, 10, float), "min_bars": (1, 200, int), "risk": (0, 10, float),
           "stretch": (0.5, 10, float), "sl_k": (0, 10, float), "sl_buf": (0, 100, float), "soft_sig": (0.1, 5, float),
           "tp1r": (0.1, 10, float), "tp1p": (1, 100, int), "tp2r": (0.2, 20, float), "tp2p": (0, 100, int),
           "be_off": (0, 100, float), "trail_after": (1, 2, int), "time": (0, 2000, int)}
DCA_ENUM = {"dir": ("both", "buy", "sell"), "sl_mode": ("min", "vwap", "swing"), "candles": ("ha", "std")}
DCA_BOOL = ("soft", "cap", "be", "trail", "rev", "sess_on")


def _sess_ok(s):
    """"HHMM-HHMM" → (phút đầu, phút cuối) hoặc None nếu sai dạng."""
    try:
        a, b = s.split("-")
        f = lambda x: int(x[:2]) * 60 + int(x[2:])
        assert len(a) == len(b) == 4 and 0 <= f(a) < 1440 and 0 <= f(b) <= 1440
        return f(a), f(b)
    except Exception:
        return None


def _in_sess(d, t_utc):
    """Giờ VN của nến có nằm trong khung giờ cài đặt không (khung qua nửa đêm vẫn được)."""
    if not d.get("sess_on"):
        return True
    r = _sess_ok(d.get("sess", ""))
    if not r:
        return True
    m = ((int(t_utc) + 7 * 3600) % 86400) // 60
    return r[0] <= m < r[1] if r[0] <= r[1] else (m >= r[0] or m < r[1])
_lock = threading.RLock()
_log = deque(maxlen=60)
_state = {"last_bar": 0, "check": 0, "msg": "Chưa chạy", "sig": None, "lot": None, "known": {}}


# ---------- Cài đặt + nhật ký ----------

def _cfg():
    try:
        c = {**DEFAULT, **json.loads(CFG_FILE.read_text(encoding="utf-8"))}
    except Exception:
        c = dict(DEFAULT)
    c["dca"] = {**DCA_DEF, **(c.get("dca") or {})}
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


def _close_pos(p, why, vol=None):
    info = mt5.symbol_info(SYM)
    tick = mt5.symbol_info_tick(SYM)
    buy = p.type == mt5.POSITION_TYPE_BUY
    part = vol is not None and vol < p.volume - 1e-9
    res = _send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYM, "position": p.ticket, "volume": vol if part else p.volume,
        "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
        "price": tick.bid if buy else tick.ask, "deviation": DEVIATION,
        "magic": MAGIC, "comment": COMMENT, "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info),
    })
    if part:
        log(f"Chốt {vol} / {p.volume} lot #{p.ticket} {'BUY' if buy else 'SELL'} @ {res.price} ({why})", "close")
        return res
    _state["known"].pop(p.ticket, None)
    log(f"Đóng #{p.ticket} {'BUY' if buy else 'SELL'} {p.volume} lot @ {res.price} ({why}), lãi/lỗ {p.profit + p.swap:.2f}", "close")
    return res


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


# ---------- Chiến lược DCA: trạng thái chiến dịch ----------

DCA_EMPTY = {"dir": 0, "rev": False, "sl": None, "legs": 0, "lot": None, "last_px": None, "last_t": 0, "start_t": 0,
             "tp1": False, "tp2": False, "tp1px": None, "tp2px": None, "vol_tp": 0.0, "sl_be": None, "wait_msg": False}
_dca = dict(DCA_EMPTY)


def _dca_load():
    try:
        _dca.update({**DCA_EMPTY, **json.loads(DCA_FILE.read_text(encoding="utf-8"))})
    except Exception:
        pass


def _dca_save():
    try:
        DCA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DCA_FILE.write_text(json.dumps(_dca, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _dca_reset():
    _dca.clear()
    _dca.update(DCA_EMPTY)
    _dca_save()


def _avg(ps):
    v = sum(p.volume for p in ps)
    return sum(p.price_open * p.volume for p in ps) / v if v else None


def _set_sl(p, sl):
    info = mt5.symbol_info(SYM)
    return _send({"action": mt5.TRADE_ACTION_SLTP, "symbol": SYM, "position": p.ticket,
                  "sl": round(sl, info.digits), "tp": p.tp, "magic": MAGIC})


def _close_all(why):
    for p in _bot_positions():
        try:
            _close_pos(p, why)
        except Exception as e:
            log(f"Không đóng được #{p.ticket}: {e}", "err")


def _close_part(pct, why):
    """Chốt pct% khối lượng chiến dịch (tính trên khối lượng lúc chạm TP1), chia vào các lệnh theo thứ tự."""
    info = mt5.symbol_info(SYM)
    step, vmin = info.volume_step or 0.01, info.volume_min
    want = math.floor(_dca["vol_tp"] * pct / 100 / step + 1e-9) * step
    if want < vmin - 1e-9:
        log(f"{why}: khối lượng {_dca['vol_tp']:.2f} lot quá nhỏ để chốt {pct}% (tối thiểu {vmin}), giữ nguyên lệnh.", "info")
        return
    left = round(want, 2)
    for p in sorted(_bot_positions(), key=lambda x: x.ticket):
        if left < vmin - 1e-9:
            break
        v = min(p.volume, left)
        if p.volume - v > 1e-9 and p.volume - v < vmin - 1e-9:      # phần còn lại dưới lot tối thiểu: đóng hết lệnh
            v = p.volume
        v = round(math.floor(v / step + 1e-9) * step, 2)
        if v < vmin - 1e-9:
            continue
        try:
            _close_pos(p, why, v)
            left = round(left - v, 2)
        except Exception as e:
            log(f"{why}: không chốt được #{p.ticket}: {e}", "err")


def _dca_levels(d, sg=None):
    """Tính lại TP1/TP2 theo giá trung bình thật của các lệnh và SL chung (chỉ khi chưa chốt TP1)."""
    ps = _bot_positions()
    avg = _avg(ps)
    if avg is None or _dca["sl"] is None or _dca["tp1"]:
        return
    R = _dca["dir"] * (avg - _dca["sl"])
    if R <= 0:
        return
    t1 = avg + _dca["dir"] * d["tp1r"] * R
    t2 = avg + _dca["dir"] * d["tp2r"] * R
    if d["cap"] and sg:
        if _dca["dir"] == 1:
            band = min(sg["up"], sg["upx"]) if sg["ready"] and sg["upx"] is not None else sg["up"]
        else:
            band = max(sg["lo"], sg["lox"]) if sg["ready"] and sg["lox"] is not None else sg["lo"]
        if _dca["dir"] * (band - t1) > 0 and _dca["dir"] * (band - t2) < 0:
            t2 = band
    _dca["tp1px"], _dca["tp2px"] = t1, t2


def _dca_live(c):
    """Chạy mỗi vòng (2 giây): phát hiện chiến dịch đã kết thúc, giá chạm TP1/TP2 → chốt từng phần, dời SL hòa vốn."""
    d = c["dca"]
    if not _dca["dir"]:
        return
    ps = _bot_positions()
    if not ps:
        log(f"Chiến dịch {'BUY' if _dca['dir'] > 0 else 'SELL'} DCA kết thúc ({_dca['legs']} lệnh).", "info", tele=False)
        _dca_reset()
        return
    tick = mt5.symbol_info_tick(SYM)
    if not tick or tick.bid <= 0:
        return
    dr = _dca["dir"]
    px = tick.bid if dr > 0 else tick.ask                       # giá đóng lệnh
    tp2p = max(0, min(d["tp2p"], 100 - d["tp1p"]))
    changed = False
    if not _dca["tp1"] and _dca["tp1px"] and dr * (px - _dca["tp1px"]) >= 0:
        avg0 = _avg(ps)                                         # giá TB cả chiến dịch (trước khi chốt từng phần)
        _dca["tp1"], _dca["vol_tp"], changed = True, round(sum(p.volume for p in ps), 2), True
        _close_part(d["tp1p"], "TP1")
        ps = _bot_positions()
        if d["be"] and ps:
            be = avg0 + dr * d["be_off"]
            info = mt5.symbol_info(SYM)
            mind = (info.trade_stops_level + 1) * info.point
            for p in ps:
                if dr * (px - be) >= mind and (p.sl == 0 or dr * (be - p.sl) > info.point / 2):
                    try:
                        _set_sl(p, be)
                    except Exception as e:
                        log(f"Không dời SL hòa vốn #{p.ticket}: {e}", "err")
            _dca["sl_be"] = be
            log(f"DCA: chạm TP1 → dời SL về hòa vốn {be:.2f}", "info")
        if tp2p == 0:
            _dca["tp2"] = True
    if _dca["tp1"] and not _dca["tp2"] and tp2p > 0 and _dca["tp2px"] and dr * (px - _dca["tp2px"]) >= 0 and _bot_positions():
        _dca["tp2"], changed = True, True
        _close_part(tp2p, "TP2")
    if changed:
        _dca_save()


def _dca_bar(c, bars, b, fixed):
    """Chạy khi có nến mới đóng: thoát chủ động, mở chiến dịch / thêm lệnh DCA, cập nhật TP."""
    d = c["dca"]
    sigs = dca_compute(bars, {"candles": d["candles"]})
    sg = sigs[-1] if sigs else None
    if not sg:
        _state["msg"] += " · DCA: chưa đủ nến"
        return
    t_now = bars[-1][0]
    since = lambda t0: sum(1 for x in bars if x[0] > t0)
    ps = _bot_positions()
    dr = _dca["dir"]
    if dr and not ps:                                           # SL/đóng tay giữa 2 vòng
        _dca_reset()
        dr = 0
    if not dr and ps:                                           # lệnh bot cũ (chiến lược khác) còn mở
        if not _dca["wait_msg"]:
            _dca["wait_msg"] = True
            log(f"DCA: còn {len(ps)} lệnh bot cũ đang mở, chờ chúng đóng mới bắt đầu chiến dịch.", "info", tele=False)
        return
    _dca["wait_msg"] = False
    go = sg["go"]
    if go:
        _state["sig"] = {"t": go, "side": "BUY" if go > 0 else "SELL", "e": sg["c"], "sl": _dca["sl"] or 0,
                         "bar": int(t_now), "kind": "ĐC*" if sg["rev"] else "", "at": _now_vn()}
        log(f"Tín hiệu DCA {'BUY' if go > 0 else 'SELL'}{' ĐC*' if sg['rev'] else ''} {c['tf']} @ {sg['c']:.2f}", "info", tele=False)

    # 2. Thoát chủ động
    closed = want_rev = False
    if dr:
        cl, vw, sd = sg["c"], sg["vwap"], sg["sd"]
        opp = go == -dr
        soft = d["soft"] and not _dca["rev"] and sg["ready"] and (cl < vw - d["soft_sig"] * sd if dr > 0 else cl > vw + d["soft_sig"] * sd)
        tout = d["time"] > 0 and not _dca["tp1"] and since(_dca["start_t"]) >= d["time"]
        tr_on = _dca["tp1"] if d["trail_after"] == 1 else _dca["tp2"]
        trail = d["trail"] and tr_on and (cl < sg["ema21"] if dr > 0 else cl > sg["ema21"])
        why = "tín hiệu ngược" if opp else "SL mềm VWAP" if soft else "hết giờ" if tout else "trail EMA21" if trail else ""
        if why:
            _close_all(why)
            log(f"DCA: đóng chiến dịch ({why})", "close", tele=False)
            _dca_reset()
            closed = not (opp and d["rev"])
            want_rev = opp and d["rev"]
            dr = 0

    # 4. Mở chiến dịch / thêm lệnh (chỉ trong khung giờ nếu bật)
    a, block = _account_guard()
    if go and not _in_sess(d, sg["t"]):
        if not dr or go == dr:
            log(f"DCA: bỏ tín hiệu {'BUY' if go > 0 else 'SELL'}, ngoài khung giờ {d['sess']} (giờ VN).", "info", tele=False)
        go = 0                                   # vẫn cập nhật TP ở bước 5
    if not dr and not closed and go:
        if (go > 0 and d["dir"] == "sell") or (go < 0 and d["dir"] == "buy"):
            return
        cl, vw, sd, atr = sg["c"], sg["vwap"], sg["sd"], sg["atr"]
        stretch_ok = sg["rev"] or not sg["ready"] or (cl <= vw + d["stretch"] * sd if go > 0 else cl >= vw - d["stretch"] * sd)
        if not stretch_ok:
            log(f"DCA: bỏ {'BUY' if go > 0 else 'SELL'}, giá cách VWAP quá {d['stretch']}σ.", "info", tele=False)
            return
        if sg["rev"]:
            sl = sg["ext"] - go * d["sl_buf"]
        else:
            slv = vw - go * d["sl_k"] * atr
            slw = (sg["swl"] - d["sl_buf"]) if go > 0 else (sg["swh"] + d["sl_buf"])
            sl = slv if d["sl_mode"] == "vwap" else slw if d["sl_mode"] == "swing" else (min(slv, slw) if go > 0 else max(slv, slw))
        if block:
            log(f"Không vào lệnh DCA: {block}", "err")
            return
        _dca_open(c, go, sl, sg, b, fixed, first=True, why="đảo chiều" if want_rev else "")
    elif dr and go == dr and not _dca["tp1"] and _dca["legs"] < _dca_cap(d, b, fixed):
        gap_ok = (sg["c"] <= _dca["last_px"] - d["gap"] * sg["atr"] if dr > 0 else sg["c"] >= _dca["last_px"] + d["gap"] * sg["atr"]) \
            and since(_dca["last_t"]) >= d["min_bars"]
        struct_ok = (sg["c"] > _dca["sl"] if dr > 0 else sg["c"] < _dca["sl"]) and (_dca["rev"] or (sg["c"] > sg["vwap"] if dr > 0 else sg["c"] < sg["vwap"]))
        if gap_ok and struct_ok:
            if block:
                log(f"Không thêm lệnh DCA: {block}", "err")
            else:
                _dca_open(c, dr, _dca["sl"], sg, b, fixed, first=False)

    # 5. Cập nhật TP theo giá trung bình (dải TMA/VWAP thay đổi theo nến)
    if _dca["dir"]:
        _dca_levels(d, sg)
        _dca_save()


def _dca_cap(d, b, fixed):
    """Số lệnh tối đa của chiến dịch: ô 'Số lệnh' (theo % rủi ro) hoặc thêm giới hạn của bảng vốn."""
    if d["risk"] > 0:
        return d["legs"]
    return min(d["legs"], b[1] if b else 1)


def _dca_open(c, dr, sl, sg, b, fixed, first, why=""):
    d = c["dca"]
    info = mt5.symbol_info(SYM)
    tick = mt5.symbol_info_tick(SYM)
    if not tick or tick.bid <= 0:
        log("DCA: chưa có giá XAUUSD.", "err")
        return
    buy = dr > 0
    price = tick.ask if buy else tick.bid
    mind = (info.trade_stops_level + 1) * info.point
    if (price - sl if buy else sl - price) < mind:
        log(f"DCA: giá {price} đã quá gần/vượt SL {sl:.2f}, bỏ tín hiệu.", "err")
        return
    if first:
        if d["risk"] > 0:
            a = mt5.account_info()
            loss1 = abs(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, SYM, 1.0, price, sl) or 0)
            if not a or loss1 <= 0:
                log("DCA: không tính được lot theo % rủi ro.", "err")
                return
            step = info.volume_step or 0.01
            lot = math.floor(a.equity * d["risk"] / 100 / (d["legs"] * loss1) / step + 1e-9) * step
            if lot < info.volume_min - 1e-9:
                log(f"DCA: rủi ro {d['risk']}% chia {d['legs']} lệnh ra lot {lot:.3f} < tối thiểu {info.volume_min}, bỏ tín hiệu.", "err")
                return
        elif fixed:
            lot = fixed
        elif b:
            lot = b[2]
        else:
            log("DCA: vốn dưới mức 100$ của bảng vốn.", "err")
            return
    else:
        lot = _dca["lot"]
    try:
        lot = _norm_lot(info, lot)
        n = 1 if first else _dca["legs"] + 1
        res = _send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": SYM, "volume": lot,
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": price, "deviation": DEVIATION,
            "magic": MAGIC, "comment": f"{COMMENT} DCA{n}", "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info),
            "sl": round(sl, info.digits), "tp": 0.0,
        })
    except Exception as e:
        log(f"Bỏ lệnh DCA {'BUY' if buy else 'SELL'}: {e}", "err")
        return
    _state["known"][res.order] = "BUY" if buy else "SELL"
    if first:
        _dca.update({**DCA_EMPTY, "dir": dr, "rev": bool(sg["rev"]), "sl": sl, "legs": 1, "lot": lot,
                     "last_px": sg["c"], "last_t": sg["t"], "start_t": sg["t"]})
    else:
        _dca.update({"legs": n, "last_px": sg["c"], "last_t": sg["t"]})
    _dca_levels(d, sg)
    _dca_save()
    kind = ("BUY" if buy else "SELL") + (" ĐC*" if _dca["rev"] else "")
    lbl = (f"Mở chiến dịch {kind}" + (f" ({why})" if why else "")) if first else f"Thêm lệnh DCA {n}/{_dca_cap(d, b, fixed)} {kind}"
    log(f"{lbl}: {res.volume} lot @ {res.price} · SL chung {sl:.2f} · TP1 {(_dca['tp1px'] or 0):.2f} · TP2 {(_dca['tp2px'] or 0):.2f}", "open")


def _step():
    c = _cfg()
    tf_code, tf_sec = TFS.get(c["tf"], TFS["M1"])
    with mt5_service.session():
        off, _ = server_offset()
        _check_closed(off)
        if not mt5.symbol_select(SYM, True):
            _state["msg"] = "Broker không có XAUUSD"
            return
        ha = c.get("strategy") == "ha"
        dca = c.get("strategy") == "dca"
        if dca:
            _dca_live(c)                         # chốt TP1/TP2 theo giá thực, mỗi 2 giây
        r = mt5.copy_rates_from_pos(SYM, tf_code, 0, 1500 if (ha or dca) else 600)
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
        if dca:
            bars = [(int(x["time"]) - off, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                     float(x["tick_volume"]), int(x["time"]) // 86400) for x in r[:-1]]
            _dca_bar(c, bars, b, c.get("lot"))
            return
        if ha:
            bars = [(int(x["time"]) - off, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                     float(x["tick_volume"]), int(x["time"]) // 86400) for x in r[:-1]]   # chỉ nến đã đóng
            ent = analyze_ha(bars, {"sl_buffer": c.get("ha_sl_buffer", 2.0), "sl_lookback": int(c.get("ha_sl_lookback", 20)),
                                    "candles": c.get("ha_candles", "ha")})
            last = ent[-1] if ent else None
            sg = None
            if last and last["i"] == len(bars) - 1:            # nến vừa đóng có nhãn BUY/SELL
                t = 1 if last["side"] == "BUY" else -1
                sg = {"t": t, "side": last["side"], "e": last["e"], "sl": last["sl"], "tp": [float(x) for x in last["tp"]],
                      "bar": int(bar), "kind": last["kind"]}
        else:
            sg = signal_at(rows, i, float(c["step"]))
        if not sg:
            return
        _state["sig"] = {**sg, "at": _now_vn()}
        log(f"Tín hiệu {STRATS.get(c.get('strategy'), '')} {sg['side']}{(' ' + sg['kind']) if sg.get('kind') else ''} {c['tf']} @ {sg['e']:.2f} (SL {sg['sl']:.2f})", "info", tele=False)
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
        _dca_load()
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
    strategy: str | None = None
    ha_sl_buffer: float | None = None
    ha_sl_lookback: int | None = None
    ha_candles: str | None = None
    dca: dict | None = None


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
            "sig": _state["sig"], "pos": pos, "log": list(_log)[:30], "camp": dict(_dca),
            "tg": bool(_env("TELEGRAM_BOT_TOKEN")), "tg_chat": bool(_env("TELEGRAM_CHAT_ID"))}


@router.post("/auto")
def auto_set(x: AutoIn):
    c = _cfg()
    was = c["on"]
    if x.tf is not None:
        if x.tf not in TFS:
            return {"ok": False, "loi": "Khung chỉ M1, M5 hoặc M15."}
        c["tf"] = x.tf
    if x.step is not None:
        if not (0.2 <= x.step <= 100):
            return {"ok": False, "loi": "Bước TP từ 0.2 đến 100 $."}
        c["step"] = round(x.step, 2)
    if x.tp is not None:
        if x.tp not in (1, 2, 3, 4):
            return {"ok": False, "loi": "TP chỉ 1 đến 4."}
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
    if x.strategy is not None:
        if x.strategy not in STRATS:
            return {"ok": False, "loi": "Chiến lược không hợp lệ."}
        c["strategy"] = x.strategy
    if x.ha_candles is not None:
        if x.ha_candles not in ("ha", "std"):
            return {"ok": False, "loi": "Loại nến không hợp lệ."}
        c["ha_candles"] = x.ha_candles
    if x.ha_sl_buffer is not None:
        if not (0 <= x.ha_sl_buffer <= 100):
            return {"ok": False, "loi": "SL cộng thêm từ 0 đến 100."}
        c["ha_sl_buffer"] = round(x.ha_sl_buffer, 2)
    if x.ha_sl_lookback is not None:
        if not (2 <= x.ha_sl_lookback <= 200):
            return {"ok": False, "loi": "Số nến SL từ 2 đến 200."}
        c["ha_sl_lookback"] = x.ha_sl_lookback
    if x.dca is not None:
        d = dict(c["dca"])
        for k, v in x.dca.items():
            if k in DCA_NUM:
                lo_, hi_, typ = DCA_NUM[k]
                try:
                    v = typ(v)
                except (TypeError, ValueError):
                    return {"ok": False, "loi": f"DCA: giá trị {k} không hợp lệ."}
                if not (lo_ <= v <= hi_):
                    return {"ok": False, "loi": f"DCA: {k} phải từ {lo_} đến {hi_}."}
                d[k] = round(v, 2) if typ is float else v
            elif k in DCA_ENUM:
                if v not in DCA_ENUM[k]:
                    return {"ok": False, "loi": f"DCA: {k} không hợp lệ."}
                d[k] = v
            elif k in DCA_BOOL:
                d[k] = bool(v)
            elif k == "sess":
                v = str(v).replace(" ", "").replace(":", "")
                if not _sess_ok(v):
                    return {"ok": False, "loi": "DCA: khung giờ dạng 1400-2300 (giờ VN)."}
                d[k] = v
        c["dca"] = d
    if c.get("strategy") != "ha" and c.get("tp") == 4:
        c["tp"] = 3
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
