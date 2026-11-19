"""PinYin CKG: bot tự vào lệnh XAUUSD theo tín hiệu EMA9 × EMA21 (giống hệt bảng phân tích vàng).

Chạy trên máy chủ (luồng nền), không cần mở trang.
  - Tín hiệu: nến vừa ĐÓNG có EMA9 cắt EMA21. BUY khi cắt lên, SELL khi cắt xuống.
  - SL: đáy (BUY) / đỉnh (SELL) của 4 nến gần nhất kể cả nến tín hiệu. TP: giá vào ± Bước TP × 1/2/3.
  - Có tín hiệu ngược chiều: chốt lệnh bot đang mở rồi vào lệnh mới.
  - Lot theo bảng vốn (vốn quy ra USD): 100$→0.01, 600$→0.02, 1000$→0.03; số lệnh bot tối đa 3/5 theo bậc.
  - Dùng chung chốt an toàn của buồng lái: chỉ tài khoản DEMO (trừ khi PINYIN_ALLOW_REAL=1), cần bật Algo Trading.
  - Lệnh bot có C().magic riêng 260117, không đụng lệnh vào tay hay lệnh buồng lái.
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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.ckg import server_offset
from app.api.ckg_trade import _account_guard, _filling, _norm_lot, _send, DEVIATION, MAX_LOT
from app.services.mt5_service import mt5_service
from app.services.ha_pro_dca import dca_signals, dca_gap, dca_leg_lot, dca_units, dca_sl
from app.services import copytrade


SYM = "XAUUSD"
MAGIC = 260117
COMMENT = "PinYin AUTO"
TFS = {"M1": (mt5.TIMEFRAME_M1, 60), "M5": (mt5.TIMEFRAME_M5, 300), "M15": (mt5.TIMEFRAME_M15, 900)}
STRATS = {"dca": "PinYin HA Pro DCA"}
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
           "strategy": "dca", "ha_sl_buffer": 2.0, "ha_sl_lookback": 20,   # chiến lược tín hiệu
           "ha_candles": "ha",   # HA Pro tính trên nến Heikin Ashi (như TradingView) hoặc nến Nhật
           "ha_be_tp1": True,    # v5: giá chạm TP1 thì dời SL về giá vào (như chỉ báo)
           "dca": {}}            # cài đặt chiến lược DCA (gộp với DCA_DEF)
# Cài đặt DCA mặc định = input mặc định của Pine Script. Khóa: (min, max, kiểu) để kiểm tra khi lưu.
DCA_DEF = {"legs": 3, "gap": 0.5, "min_bars": 5, "risk": 0.0, "dir": "both", "stretch": 2.0,
           "sl_mode": "min", "sl_k": 0.5, "sl_buf": 1.0, "soft": True, "soft_sig": 1.0,
           "tp1r": 1.0, "tp1p": 50, "tp2r": 2.0, "tp2p": 30, "cap": True, "be": True, "be_off": 0.0,
           "trail": True, "trail_after": 2, "time": 60, "rev": True, "candles": "std", "sig": "v5",
           "sess_on": False, "sess": "1400-2300",   # chỉ MỞ/THÊM lệnh trong khung giờ VN (như useSession của Pine)
           "gap_usd": 0.0,       # lệnh DCA thêm khi giá tốt hơn ≥ ... $ (0 = dùng gap × ATR)
           "lot_mult": 1.0,      # lot lệnh DCA thứ n = lot đầu × lot_mult^(n-1) (1 = bằng lệnh đầu)
           "sl_follow": True}    # thêm lệnh DCA: SL chung tính lại theo lệnh mới, mọi lệnh trước dời SL theo
DCA_NUM = {"legs": (1, 5, int), "gap": (0, 10, float), "min_bars": (1, 200, int), "risk": (0, 10, float),
           "stretch": (0.5, 10, float), "sl_k": (0, 10, float), "sl_buf": (0, 100, float), "soft_sig": (0.1, 5, float),
           "tp1r": (0.1, 10, float), "tp1p": (1, 100, int), "tp2r": (0.2, 20, float), "tp2p": (0, 100, int),
           "be_off": (0, 100, float), "trail_after": (1, 2, int), "time": (0, 2000, int),
           "gap_usd": (0, 100, float), "lot_mult": (1, 5, float)}
DCA_ENUM = {"dir": ("both", "buy", "sell"), "sl_mode": ("min", "vwap", "swing"), "candles": ("ha", "std"), "sig": ("v5", "dca")}
DCA_BOOL = ("soft", "cap", "be", "trail", "rev", "sess_on", "sl_follow")


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
        c = {**DEFAULT, **json.loads(C().cfg_file.read_text(encoding="utf-8"))}
    except Exception:
        c = dict(DEFAULT)
    c["dca"] = {**DCA_DEF, **(c.get("dca") or {})}
    c.pop("cpb", None)                           # chiến lược EMA Cloud Pullback đã gỡ (19/11/2026)
    if c.get("strategy") not in STRATS:          # các chiến lược cũ (EMA9×EMA21, HA Pro, EMA Cloud Pullback) đã bỏ
        c["strategy"] = "dca"
    return c


def _save(c):
    C().cfg_file.parent.mkdir(parents=True, exist_ok=True)
    C().cfg_file.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def _now_vn():
    return datetime.now(VN).strftime("%d/%m %H:%M:%S")


def log(msg, kind="info", tele=True):
    try:
        cx = C()
        if cx is not _MAIN or _ctxs or cx.tf_force:
            msg = f"[{_label(cx)}] {msg}"
        sid = cx.sid
    except NameError:
        sid = "main"
    e = {"t": _now_vn(), "k": kind, "m": msg, "b": sid}
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


def _sig(text):
    """Gửi tin vào/đóng lệnh sang NHÓM TÍN HIỆU cho khách (TELEGRAM_SIGNAL_CHAT), nếu bot này bật gửi.
    Chỉ nội dung lệnh: không số tài khoản, không số tiền, không tin nội bộ."""
    chat = _env("TELEGRAM_SIGNAL_CHAT")
    try:
        if not chat or not _cfg().get("tg_sig"):
            return
        name = _label(C())
    except Exception:
        return
    msg = f"{text[:1]} [{name}] {text[1:].strip()}" if text[:1] in "🟢🔵🛡✋⚠" else f"[{name}] {text}"

    def run():
        try:
            _tg_api("sendMessage", {"chat_id": chat, "text": msg})
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

# ---------- Lệnh ----------

def is_cent(ccy):
    """Tài khoản cent (USC, USc, UScent, EUC...)."""
    c = (ccy or "").strip()
    return c.upper() in ("USC", "USCENT", "UScent".upper(), "EUC", "EURC", "GBC", "CENT") or (len(c) == 3 and c[2] == "c")


def _to_usd(ccy):
    if ccy == "USD" or is_cent(ccy):
        # cent: chủ chọn tính số vốn như $ (500 USC ~ 500$ của tài khoản USD) để tra bảng vốn
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
    return [p for p in (mt5.positions_get(symbol=SYM) or []) if p.magic == C().magic]


def _close_pos(p, why, vol=None):
    info = mt5.symbol_info(SYM)
    tick = mt5.symbol_info_tick(SYM)
    buy = p.type == mt5.POSITION_TYPE_BUY
    part = vol is not None and vol < p.volume - 1e-9
    res = _send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYM, "position": p.ticket, "volume": vol if part else p.volume,
        "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
        "price": tick.bid if buy else tick.ask, "deviation": DEVIATION,
        "magic": C().magic, "comment": COMMENT, "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info),
    })
    side = "BUY" if buy else "SELL"
    mv = (res.price - p.price_open) if buy else (p.price_open - res.price)
    if part:
        log(f"Chốt {vol} / {p.volume} lot #{p.ticket} {side} @ {res.price} ({why})", "close")
        _sig(f"🔵 Chốt một phần {side} @ {res.price:.2f} ({why}) · {mv:+.2f} giá")
        return res
    C().st["known"].pop(p.ticket, None)
    log(f"Đóng #{p.ticket} {side} {p.volume} lot @ {res.price} ({why}), lãi/lỗ {p.profit + p.swap:.2f}", "close")
    _sig(f"🔵 Đóng {side} @ {res.price:.2f} ({why}) · {mv:+.2f} giá")
    return res


def _check_closed(off):
    """Lệnh bot biến mất (chạm SL/TP hoặc đóng tay trên MT5) → báo kết quả."""
    live = {p.ticket for p in _bot_positions()}
    for tk in [t for t in C().st["known"] if t not in live]:
        side = C().st["known"].pop(tk)
        deals = mt5.history_deals_get(position=tk) or []
        out = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
        pnl = sum(d.profit + d.swap + d.commission for d in deals)
        why = {mt5.DEAL_REASON_SL: "chạm SL", mt5.DEAL_REASON_TP: "chạm TP"}.get(out[-1].reason, "đóng") if out else "đóng"
        px = out[-1].price if out else "?"
        # sàn thường ghi mức kích hoạt vào ghi chú, vd "[sl 3893.05]" → biết SL đã bị dời hay do giá giật
        note = (out[-1].comment or "").strip() if out else ""
        sl_now = None
        try:
            od = [o for o in (mt5.history_orders_get(position=tk) or []) if o.ticket == out[-1].order] if out else []
            sl_now = od[0].price_open if od and out[-1].reason in (mt5.DEAL_REASON_SL, mt5.DEAL_REASON_TP) else None
        except Exception:
            pass
        extra = (f" · mức kích hoạt {sl_now}" if sl_now else "") + (f" · sàn ghi: {note}" if note else "")
        log(f"#{tk} {side} {why} @ {px}, lãi/lỗ {pnl:.2f}{extra}", "close")
        ins = [d for d in deals if d.entry == mt5.DEAL_ENTRY_IN]
        if out and ins:
            mv = (out[-1].price - ins[0].price) * (1 if side == "BUY" else -1)
            _sig(f"🔵 {side} {why} @ {out[-1].price:.2f} · {mv:+.2f} giá")
        else:
            _sig(f"🔵 {side} {why}")


# ---------- Chiến lược DCA: trạng thái chiến dịch ----------

DCA_EMPTY = {"dir": 0, "rev": False, "sl": None, "legs": 0, "lot": None, "last_px": None, "last_t": 0, "start_t": 0,
             "tp1": False, "tp2": False, "tp1px": None, "tp2px": None, "vol_tp": 0.0, "sl_be": None, "wait_msg": False, "sl_how": "", "sl_man": None}
_dca = dict(DCA_EMPTY)


def _dca_load():
    try:
        C().dca.update({**DCA_EMPTY, **json.loads(C().dca_file.read_text(encoding="utf-8"))})
    except Exception:
        pass


def _dca_save():
    try:
        C().dca_file.parent.mkdir(parents=True, exist_ok=True)
        C().dca_file.write_text(json.dumps(C().dca, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _dca_reset():
    _copy().set_plan(0)
    C().dca.clear()
    C().dca.update(DCA_EMPTY)
    _dca_save()


def _avg(ps):
    v = sum(p.volume for p in ps)
    return sum(p.price_open * p.volume for p in ps) / v if v else None


def _set_sl(p, sl):
    info = mt5.symbol_info(SYM)
    return _send({"action": mt5.TRADE_ACTION_SLTP, "symbol": SYM, "position": p.ticket,
                  "sl": round(sl, info.digits), "tp": p.tp, "magic": C().magic})


def _close_all(why):
    _copy().copy_close(why)
    for p in _bot_positions():
        try:
            _close_pos(p, why)
        except Exception as e:
            log(f"Không đóng được #{p.ticket}: {e}", "err")


def _close_part(pct, why):
    """Chốt pct% khối lượng chiến dịch (tính trên khối lượng lúc chạm TP1), chia vào các lệnh theo thứ tự."""
    info = mt5.symbol_info(SYM)
    step, vmin = info.volume_step or 0.01, info.volume_min
    want = math.floor(C().dca["vol_tp"] * pct / 100 / step + 1e-9) * step
    if want < vmin - 1e-9:
        log(f"{why}: khối lượng {C().dca['vol_tp']:.2f} lot quá nhỏ để chốt {pct}% (tối thiểu {vmin}), giữ nguyên lệnh.", "info")
        return
    left = round(want, 2)
    tot = sum(p.volume for p in _bot_positions())
    if tot > 0:
        _copy().copy_part(min(1.0, want / tot), why)
    if why == "TP1":
        _copy().set_plan(0)
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
    if avg is None or C().dca["sl"] is None or C().dca["tp1"]:
        return
    R = C().dca["dir"] * (avg - C().dca["sl"])
    if R <= 0:
        return
    t1 = avg + C().dca["dir"] * d["tp1r"] * R
    t2 = avg + C().dca["dir"] * d["tp2r"] * R
    if d["cap"] and sg:
        if C().dca["dir"] == 1:
            band = min(sg["up"], sg["upx"]) if sg["ready"] and sg["upx"] is not None else sg["up"]
        else:
            band = max(sg["lo"], sg["lox"]) if sg["ready"] and sg["lox"] is not None else sg["lo"]
        if C().dca["dir"] * (band - t1) > 0 and C().dca["dir"] * (band - t2) < 0:
            t2 = band
    C().dca["tp1px"], C().dca["tp2px"] = t1, t2
    _copy().set_plan(abs(t1 - avg), d["be"], d["be_off"])      # khoảng TP1 cho TP1 đặt sẵn trên sàn của tài khoản copy


_sl_seen = {}


def _sl_watch(ps, dr, px, d=None):
    """Bot chỉ tự dời SL khi: vào lệnh, thêm lệnh DCA (SL chung theo lệnh mới), chạm TP1 (hòa vốn).
    SL sửa tay trên MT5 → bot KHÔNG trả lại: nhận mức mới làm SL chung (dời các lệnh còn lại theo),
    TP1/TP2 giữ nguyên (SL sửa tay lưu riêng ở sl_man, không đổi R). Gỡ SL bằng tay → bot để nguyên, chỉ ghi nhật ký."""
    want = C().dca.get("sl_man") or (C().dca["sl_be"] if C().dca["tp1"] and C().dca["sl_be"] else C().dca["sl"])
    if not want:
        return
    info = mt5.symbol_info(SYM)
    if not info:
        return
    mind = (info.trade_stops_level + 1) * info.point
    for p in ps:
        if p.sl and abs(p.sl - want) <= info.point * 1.5:
            continue
        key = (p.ticket, round(p.sl, info.digits))
        if _sl_seen.get(p.ticket) == key:                      # đã báo / đã thử trả lại mức này
            continue
        _sl_seen[p.ticket] = key
        if p.sl:
            new = round(p.sl, info.digits)
            C().dca["sl_man"] = new
            _dca_save()
            moved = 0
            for q in ps:
                if q.ticket != p.ticket and abs((q.sl or 0) - new) > info.point * 1.5 and dr * (px - new) >= mind:
                    try:
                        _set_sl(q, new)
                        _sl_seen[q.ticket] = (q.ticket, new)
                        moved += 1
                    except Exception as e:
                        log(f"Không dời SL #{q.ticket} theo SL sửa tay: {e}", "err")
            log(f"✋ SL lệnh #{p.ticket} được sửa tay: {want:.{info.digits}f} → {new:.{info.digits}f}. Bot nhận làm SL chung"
                + (f", dời thêm {moved} lệnh cùng chiến dịch" if moved else "") + " (TP1/TP2 giữ nguyên).", "info")
            _sig(f"✋ Dời SL về {new:.{info.digits}f}")
            continue
        log(f"✋ SL lệnh #{p.ticket} đã bị gỡ bằng tay (SL bot: {want:.{info.digits}f}). Bot để nguyên, không đặt lại.", "info")
    for t in [t for t in _sl_seen if t not in {p.ticket for p in ps}]:
        _sl_seen.pop(t, None)


def _dca_live(c):
    """Chạy mỗi vòng (2 giây): phát hiện chiến dịch đã kết thúc, giá chạm TP1/TP2 → chốt từng phần, dời SL hòa vốn."""
    d = c["dca"]
    if not C().dca["dir"]:
        return
    ps = _bot_positions()
    if not ps:
        log(f"Chiến dịch {'BUY' if C().dca['dir'] > 0 else 'SELL'} DCA kết thúc ({C().dca['legs']} lệnh).", "info", tele=False)
        _copy().copy_close("chiến dịch đã kết thúc trên tài khoản tín hiệu")
        _dca_reset()
        return
    tick = mt5.symbol_info_tick(SYM)
    if not tick or tick.bid <= 0:
        return
    dr = C().dca["dir"]
    px = tick.bid if dr > 0 else tick.ask                       # giá đóng lệnh
    _sl_watch(ps, dr, px, d)
    tp2p = max(0, min(d["tp2p"], 100 - d["tp1p"]))
    changed = False
    if not C().dca["tp1"] and C().dca["tp1px"] and dr * (px - C().dca["tp1px"]) >= 0:
        avg0 = _avg(ps)                                         # giá TB cả chiến dịch (trước khi chốt từng phần)
        C().dca["tp1"], C().dca["vol_tp"], changed = True, round(sum(p.volume for p in ps), 2), True
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
            C().dca["sl_be"] = be
            C().dca["sl_man"] = None
            _sig(f"🛡 Chạm TP1 → dời SL về hòa vốn {be:.2f}")
            log(f"DCA: chạm TP1 → dời SL về hòa vốn {be:.2f}", "info")
            _copy().copy_beavg(d["be_off"])
        if tp2p == 0:
            C().dca["tp2"] = True
    if C().dca["tp1"] and not C().dca["tp2"] and tp2p > 0 and C().dca["tp2px"] and dr * (px - C().dca["tp2px"]) >= 0 and _bot_positions():
        C().dca["tp2"], changed = True, True
        _close_part(tp2p, "TP2")
    if changed:
        _dca_save()


def _dca_bar(c, bars, b, fixed):
    """Chạy khi có nến mới đóng: thoát chủ động, mở chiến dịch / thêm lệnh DCA, cập nhật TP."""
    d = c["dca"]
    sigs = dca_signals(bars, d)
    sg = sigs[-1] if sigs else None
    if not sg:
        C().st["msg"] += " · DCA: chưa đủ nến"
        return
    t_now = bars[-1][0]
    since = lambda t0: sum(1 for x in bars if x[0] > t0)
    ps = _bot_positions()
    dr = C().dca["dir"]
    if dr and not ps:                                           # SL/đóng tay giữa 2 vòng
        _copy().copy_close("chiến dịch đã kết thúc trên tài khoản tín hiệu")
        _dca_reset()
        dr = 0
    if not dr and ps:                                           # lệnh bot cũ (chiến lược khác) còn mở
        if not C().dca["wait_msg"]:
            C().dca["wait_msg"] = True
            log(f"DCA: còn {len(ps)} lệnh bot cũ đang mở, chờ chúng đóng mới bắt đầu chiến dịch.", "info", tele=False)
        return
    C().dca["wait_msg"] = False
    go = sg["go"]
    if go:
        C().st["sig"] = {"t": go, "side": "BUY" if go > 0 else "SELL", "e": sg["c"], "sl": C().dca["sl"] or 0,
                         "bar": int(t_now), "kind": "ĐC*" if sg["rev"] else "", "at": _now_vn()}
        log(f"Tín hiệu DCA {'BUY' if go > 0 else 'SELL'}{' ĐC*' if sg['rev'] else ''} {c['tf']} @ {sg['c']:.2f}", "info", tele=False)

    # 2. Thoát chủ động
    closed = want_rev = False
    if dr:
        cl, vw, sd = sg["c"], sg["vwap"], sg["sd"]
        opp = go == -dr
        soft = d["soft"] and not C().dca["rev"] and sg["ready"] and (cl < vw - d["soft_sig"] * sd if dr > 0 else cl > vw + d["soft_sig"] * sd)
        tout = d["time"] > 0 and not C().dca["tp1"] and since(C().dca["start_t"]) >= d["time"]
        tr_on = C().dca["tp1"] if d["trail_after"] == 1 else C().dca["tp2"]
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
        cl, vw, sd = sg["c"], sg["vwap"], sg["sd"]
        stretch_ok = sg["rev"] or not sg["ready"] or (cl <= vw + d["stretch"] * sd if go > 0 else cl >= vw - d["stretch"] * sd)
        if not stretch_ok:
            log(f"DCA: bỏ {'BUY' if go > 0 else 'SELL'}, giá cách VWAP quá {d['stretch']}σ.", "info", tele=False)
            return
        sl, how = dca_sl(d, sg, go, c["tf"])
        sg = {**sg, "sl_how": how}
        if block:
            log(f"Không vào lệnh DCA: {block}", "err")
            return
        _dca_open(c, go, sl, sg, b, fixed, first=True, why="đảo chiều" if want_rev else "")
    elif dr and go == dr and not C().dca["tp1"] and C().dca["legs"] < _dca_cap(d, b, fixed):
        gp = dca_gap(d, sg["atr"])
        gap_ok = (sg["c"] <= C().dca["last_px"] - gp if dr > 0 else sg["c"] >= C().dca["last_px"] + gp) \
            and since(C().dca["last_t"]) >= d["min_bars"]
        struct_ok = (sg["c"] > C().dca["sl"] if dr > 0 else sg["c"] < C().dca["sl"]) and (C().dca["rev"] or (sg["c"] > sg["vwap"] if dr > 0 else sg["c"] < sg["vwap"]))
        if gap_ok and struct_ok:
            if block:
                log(f"Không thêm lệnh DCA: {block}", "err")
            else:
                sl_add, how2 = C().dca.get("sl_man") or C().dca["sl"], None
                if d.get("sl_follow", True):
                    ns, how2 = dca_sl(d, sg, dr, c["tf"])
                    if dr * (sg["c"] - ns) > 0:                 # SL mới phải nằm đúng phía giá
                        sl_add = ns
                    else:
                        how2 = None
                n0 = C().dca["legs"]
                _dca_open(c, dr, sl_add, sg, b, fixed, first=False)
                if how2 and C().dca["legs"] > n0:
                    _dca_follow_sl(sl_add, how2)

    # 5. Cập nhật TP theo giá trung bình (dải TMA/VWAP thay đổi theo nến)
    if C().dca["dir"]:
        _dca_levels(d, sg)
        _dca_save()


def _dca_follow_sl(new, how):
    """Thêm lệnh DCA: SL chung = SL của lệnh mới, dời SL mọi lệnh trước theo (TP1/TP2 tính lại theo SL mới)."""
    info = mt5.symbol_info(SYM)
    tick = mt5.symbol_info_tick(SYM)
    dr = C().dca["dir"]
    old = C().dca.get("sl_man") or C().dca["sl"]
    C().dca.update(sl=new, sl_how=f"{how} (theo lệnh DCA {C().dca['legs']})", sl_man=None)
    _dca_save()
    mind = (info.trade_stops_level + 1) * info.point
    px = (tick.bid if dr > 0 else tick.ask) if tick else None
    moved = 0
    for p in _bot_positions():
        if abs((p.sl or 0) - new) <= info.point * 1.5:
            continue
        if px is not None and dr * (px - new) < mind:
            continue
        try:
            _set_sl(p, new)
            _sl_seen[p.ticket] = (p.ticket, round(new, info.digits))
            moved += 1
        except Exception as e:
            log(f"Không dời SL #{p.ticket} theo lệnh DCA mới: {e}", "err")
    _copy().copy_slsync()
    log(f"DCA: SL chung dời theo lệnh DCA {C().dca['legs']}: {old:.2f} → {new:.2f} ({how}), dời {moved} lệnh trước.", "info")
    _sig(f"🛡 SL chung dời theo lệnh DCA {C().dca['legs']}: {new:.2f}")


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
            lot = math.floor(a.equity * d["risk"] / 100 / (dca_units(d, d["legs"]) * loss1) / step + 1e-9) * step
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
        lot = dca_leg_lot(C().dca["lot"], d, C().dca["legs"] + 1)
    try:
        lot = _norm_lot(info, lot)
        n = 1 if first else C().dca["legs"] + 1
        res = _send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": SYM, "volume": lot,
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": price, "deviation": DEVIATION,
            "magic": C().magic, "comment": f"{COMMENT} DCA{n}", "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info),
            "sl": round(sl, info.digits), "tp": 0.0,
        })
    except Exception as e:
        log(f"Bỏ lệnh DCA {'BUY' if buy else 'SELL'}: {e}", "err")
        return
    C().st["known"][res.order] = "BUY" if buy else "SELL"
    if first:
        C().dca.update({**DCA_EMPTY, "dir": dr, "rev": bool(sg["rev"]), "sl": sl, "legs": 1, "lot": lot,
                     "last_px": sg["c"], "last_t": sg["t"], "start_t": sg["t"], "sl_how": sg.get("sl_how", "")})
    else:
        C().dca.update({"legs": n, "last_px": sg["c"], "last_t": sg["t"]})
    _dca_levels(d, sg)
    _dca_save()
    kind = ("BUY" if buy else "SELL") + (" ĐC*" if C().dca["rev"] else "")
    lbl = (f"Mở chiến dịch {kind}" + (f" ({why})" if why else "")) if first else f"Thêm lệnh DCA {n}/{_dca_cap(d, b, fixed)} {kind}"
    _sig(f"🟢 {lbl} XAUUSD @ {res.price:.{info.digits}f} · SL {sl:.2f} · TP1 {(C().dca['tp1px'] or 0):.2f} · TP2 {(C().dca['tp2px'] or 0):.2f}")
    log(f"{lbl}: {res.volume} lot @ {res.price:.{info.digits}f} · SL chung {sl:.2f}{(' = ' + C().dca['sl_how']) if first and C().dca.get('sl_how') else ''} · TP1 {(C().dca['tp1px'] or 0):.2f} · TP2 {(C().dca['tp2px'] or 0):.2f}", "open")
    _copy().copy_open(dr, float(res.volume), abs(float(res.price) - sl), d["tp1p"] if not C().dca["tp1"] else 0, float(res.price))


def _hedging(a):
    return bool(a) and a.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING


# ---------- Nhiều bot chạy song song ----------
# Bot 1 = cài đặt cũ (data/auto_xau.json, magic 260117, được copy lệnh). Bot phụ: magic 260131+, tệp riêng, KHÔNG copy.
# Ngữ cảnh theo luồng: luồng không chọn bot (copy lệnh, lá chắn, theo dõi máy chủ) luôn thấy Bot 1.

class _Ctx:
    def __init__(self, sid, name, magic, cfg_file, dca_file, dca, st, copy):
        self.sid, self.name, self.magic, self.copy = sid, name, magic, copy
        self.cfg_file, self.dca_file = cfg_file, dca_file
        self.dca, self.st = dca, st
        self.err_at = 0
        self.tf_force = None             # bot chạy nhiều khung: khung riêng của ngữ cảnh này
        self.parent = None               # ngữ cảnh con (khung thêm) → bot cha


TF_COMBOS = ("M1+M5",)                   # chạy nhiều khung ĐỘC LẬP trong 1 bot
KID_MAGIC_OFF = 500                      # magic khung thêm = magic bot + 500 × thứ tự
_kids = {}                               # (id bot, khung) → ngữ cảnh con


def _tfs_of(c):
    t = [x for x in str(c.get("tf") or "M1").split("+") if x in TFS]
    return t or ["M1"]


def _kid(ctx, tf, i):
    k = (ctx.sid, tf)
    x = _kids.get(k)
    if not x:
        d = ROOT / "data"
        sfx = f"{ctx.sid}_{tf}"
        x = _Ctx(f"{ctx.sid}.{tf}", ctx.name, ctx.magic + KID_MAGIC_OFF * i, ctx.cfg_file, d / f"auto_dca_{sfx}.json",
                 dict(DCA_EMPTY),
                 {"last_bar": 0, "check": 0, "msg": "Chưa chạy", "sig": None, "lot": None, "known": {}}, True)
        x.parent = ctx
        with _use(x):
            _dca_load()
        _kids[k] = x
    x.name, x.tf_force, x.magic = ctx.name, tf, ctx.magic + KID_MAGIC_OFF * i
    return x


def _kids_of(ctx, tfs=None):
    """Ngữ cảnh con của bot (các khung thêm đang chọn)."""
    if tfs is None:
        with _use(ctx):
            tfs = _tfs_of(_cfg())
    return [_kid(ctx, tf, i) for i, tf in enumerate(tfs[1:], 1)]


def _run_ctxs():
    """Mọi ngữ cảnh phải chạy: bot + khung thêm (bot chạy M1 + M5 → 2 chiến dịch độc lập)."""
    out = []
    for ctx in _all_ctx():
        with _use(ctx):
            tfs = _tfs_of(_cfg())
        ctx.tf_force = tfs[0] if len(tfs) > 1 else None
        out.append(ctx)
        out += _kids_of(ctx, tfs)
    return out


def _label(cx):
    return cx.name + (f" · {cx.tf_force}" if cx.tf_force else "")


_MAIN = _Ctx("main", "Bot 1", 260117, CFG_FILE, DCA_FILE, _dca, _state, True)   # mọi bot đều là nguồn copy
_tl = threading.local()
SLOTS_FILE = ROOT / "data" / "auto_slots.json"
MAX_BOTS = 6
SLOT_MAGIC0 = 260131
_ctxs = {}                       # id → _Ctx của bot phụ


def C():
    return getattr(_tl, "c", None) or _MAIN


@contextmanager
def _use(ctx):
    old = getattr(_tl, "c", None)
    _tl.c = ctx
    try:
        yield ctx
    finally:
        _tl.c = old


class _NoCopy:
    def __getattr__(self, k):
        return lambda *a, **kw: None


_NOCOPY = _NoCopy()


class _CopySrc:
    """copytrade gắn sẵn bot nguồn: chỉ tài khoản copy chọn bot này mới nhận lệnh."""
    def __init__(self, src):
        self.src = src

    def __getattr__(self, k):
        fn = getattr(copytrade, k)
        if k in ("copy_open", "copy_part", "copy_beavg", "copy_close", "copy_order", "copy_slsync", "set_plan"):
            return lambda *a, **kw: fn(*a, **{**kw, "src": self.src})
        return fn


def _copy():
    return _CopySrc(C().sid) if C().copy else _NOCOPY


def strategy_of(src):
    ctx = _ctx_of(src)
    if not ctx:
        return None
    with _use(ctx):
        return _cfg().get("strategy")


def bot_name(src):
    ctx = _ctx_of(src)
    return ctx.name if ctx else src


def copy_dirs(src):
    """Chiều các lệnh đang mở của bot nguồn (cho copy lệnh đối chiếu). None = chưa rõ / bot không còn."""
    ctx = _ctx_of(src)
    if not ctx:
        return None
    return {ctx.dca["dir"]} if ctx.dca.get("dir") else set()


def _slots():
    try:
        x = json.loads(SLOTS_FILE.read_text(encoding="utf-8"))
        return {"main": x.get("main") or "Bot 1", "bots": list(x.get("bots") or [])}
    except Exception:
        return {"main": "Bot 1", "bots": []}


def _slots_save(x):
    SLOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SLOTS_FILE.write_text(json.dumps(x, ensure_ascii=False), encoding="utf-8")


def _all_ctx():
    """[Bot 1, bot phụ...] — đồng bộ với tệp danh sách bot."""
    x = _slots()
    _MAIN.name = x["main"]
    ids = set()
    for b in x["bots"]:
        ids.add(b["id"])
        ctx = _ctxs.get(b["id"])
        if not ctx:
            d = ROOT / "data"
            ctx = _Ctx(b["id"], b["name"], int(b["magic"]), d / f"auto_bot_{b['id']}.json", d / f"auto_dca_{b['id']}.json",
                       dict(DCA_EMPTY),
                       {"last_bar": 0, "check": 0, "msg": "Chưa chạy", "sig": None, "lot": None, "known": {}}, True)
            with _use(ctx):
                _dca_load()
            _ctxs[b["id"]] = ctx
        ctx.name = b["name"]
    for k in [k for k in _ctxs if k not in ids]:
        _ctxs.pop(k, None)
    return [_MAIN] + [_ctxs[b["id"]] for b in x["bots"]]


def _ctx_of(slot):
    if slot and "." in slot:                     # khung thêm của bot: "main.M5", "b2.M5"
        base, tf = slot.split(".", 1)
        ctx = _ctx_of(base)
        if not ctx:
            return None
        with _use(ctx):
            tfs = _tfs_of(_cfg())
        return _kid(ctx, tf, tfs.index(tf)) if tf in tfs[1:] else _kids.get((base, tf))
    if not slot or slot == "main":
        return _MAIN
    _all_ctx()
    return _ctxs.get(slot)


def _on_of(ctx):
    with _use(ctx):
        return bool(_cfg().get("on"))


def stop_all(why):
    """Tắt mọi bot (đổi tài khoản MT5...)."""
    for ctx in _run_ctxs():
        with _use(ctx):
            c = _cfg()
            if c.get("on"):
                _save({**c, "on": False})
            C().st["known"].clear()
            C().st["last_bar"] = 0


def _step():
    c = _cfg()
    if C().tf_force:                             # bot nhiều khung: mỗi ngữ cảnh chạy khung riêng
        c["tf"] = C().tf_force
    tf_code, tf_sec = TFS.get(c["tf"], TFS["M1"])
    with mt5_service.session():
        off, _ = server_offset()
        _check_closed(off)
        if not mt5.symbol_select(SYM, True):
            C().st["msg"] = "Broker không có XAUUSD"
            return
        _dca_live(c)                             # chốt TP1/TP2 theo giá thực, mỗi 2 giây
        r = mt5.copy_rates_from_pos(SYM, tf_code, 0, 1500)
        if r is None or len(r) < 30:
            C().st["msg"] = "Chưa đủ nến"
            return
        bar = int(r[-2]["time"]) - off            # nến vừa đóng (nến cuối đang chạy)
        if not C().st["last_bar"]:              # vừa bật: không vào theo tín hiệu cũ
            C().st["last_bar"] = bar
        a = mt5.account_info()
        cap, b = _tier(a) if a else (None, None)
        if a:
            _copy().set_master(a.login, a.server)
        C().st["lot"] = b[2] if b else None
        C().st["msg"] = f"Đang theo dõi {c['tf']} · nến đóng {datetime.fromtimestamp(bar, VN):%H:%M}"
        if bar <= C().st["last_bar"]:
            return
        C().st["last_bar"] = bar
        bars = [(int(x["time"]) - off, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                 float(x["tick_volume"]), int(x["time"]) // 86400) for x in r[:-1]]
        _dca_bar(c, bars, b, c.get("lot"))


def _hb(name):
    try:
        from app.services.health import beat
        beat(name)
    except Exception:
        pass


def _loop():
    _load_log()
    while True:
        for ctx in _run_ctxs():
            with _use(ctx):
                try:
                    if _cfg()["on"]:
                        with _lock:
                            _step()
                            C().st["check"] = time.time()
                    else:
                        C().st["msg"] = "Đang tắt"
                        C().st["last_bar"] = 0
                        if C().st["known"]:          # bot tắt nhưng lệnh cũ còn: vẫn báo khi chạm SL/TP
                            with _lock, mt5_service.session():
                                _check_closed(0)
                except Exception as e:
                    C().st["msg"] = f"Lỗi: {e}"
                    if time.time() - ctx.err_at > 600:  # báo lỗi tối đa 10 phút/lần mỗi bot
                        ctx.err_at = time.time()
                        log(f"Lỗi: {e}", "err")
        _hb('bot')
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
                for ctx in _run_ctxs():
                    with _use(ctx):
                        for p in _bot_positions():
                            C().st["known"][p.ticket] = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
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
    tg_sig: bool | None = None          # gửi tin vào/đóng lệnh của bot này sang nhóm tín hiệu cho khách


class TgIn(BaseModel):
    token: str


@router.get("/auto")
def auto_status(slot: str = "main"):
    ctx = _ctx_of(slot)
    if not ctx:
        return {"ok": False, "loi": "Không có bot này."}
    with _lock, _use(ctx):
        return _auto_status()


def _auto_status():
    c = _cfg()
    pos = []
    try:
        with mt5_service.session():
            a = mt5.account_info()
            if a and C().st["lot"] is None:
                b = _tier(a)[1]
                C().st["lot"] = b[2] if b else None
            tfs = _tfs_of(c)
            me = C()
            for cx in [me] + _kids_of(me, tfs):
                with _use(cx):
                    pos += [{"ticket": p.ticket, "t": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL", "lot": p.volume,
                             "e": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit + p.swap,
                             "tf": (tfs[0] if cx is me else cx.tf_force) if len(tfs) > 1 else None} for p in _bot_positions()]
    except Exception:
        pass
    camps = None
    if len(_tfs_of(c)) > 1:
        me = C()
        camps = [{"tf": _tfs_of(c)[0], "camp": dict(me.dca), "msg": me.st.get("msg", "")}] + \
                [{"tf": k.tf_force, "camp": dict(k.dca), "msg": k.st.get("msg", "")} for k in _kids_of(me)]
    return {"ok": True, "cfg": c, "msg": C().st["msg"], "check": C().st["check"], "lot": C().st["lot"],
            "sig": C().st["sig"], "pos": pos, "log": [e for e in _log if e.get("b", "main").split(".")[0] == C().sid][:30], "camp": dict(C().dca), "camps": camps,
            "bot": {"id": C().sid, "name": C().name, "magic": C().magic, "copy": C().copy},
            "tg": bool(_env("TELEGRAM_BOT_TOKEN")), "tg_chat": bool(_env("TELEGRAM_CHAT_ID")),
            "tg_group": _env("TELEGRAM_SIGNAL_TITLE") if _env("TELEGRAM_SIGNAL_CHAT") else ""}


@router.post("/auto")
def auto_set(x: AutoIn, slot: str = "main"):
    ctx = _ctx_of(slot)
    if not ctx:
        return {"ok": False, "loi": "Không có bot này."}
    with _lock, _use(ctx):
        return _auto_set(x)


def _auto_set(x: AutoIn):
    c = _cfg()
    was = c["on"]
    if x.tf is not None:
        if x.tf not in TFS and x.tf not in TF_COMBOS:
            return {"ok": False, "loi": "Khung chỉ M1, M5, M15 hoặc M1 + M5."}
        drop = [t for t in _tfs_of(c)[1:] if t not in _tfs_of({"tf": x.tf})]
        for t in drop:                          # bỏ khung thêm đang còn lệnh: không cho (lệnh sẽ không ai quản)
            k = _ctx_of(f"{C().sid}.{t}")
            try:
                with _use(k), mt5_service.session():
                    n = len(_bot_positions())
            except Exception:
                n = 0
            if n:
                return {"ok": False, "loi": f"Khung {t} của bot còn {n} lệnh mở. Đóng hết (hoặc chờ đóng) rồi mới bỏ khung {t}."}
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
        if x.strategy != c["strategy"]:
            try:
                me = C()
                n_open = 0
                with mt5_service.session():
                    for cx in [me] + _kids_of(me):
                        with _use(cx):
                            n_open += len(_bot_positions())
            except Exception:
                n_open = 0
            if n_open:
                return {"ok": False, "loi": f"Đang còn {n_open} lệnh bot mở. Đóng hết (hoặc chờ đóng) rồi mới đổi chiến lược."}
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
    if x.tg_sig is not None:
        c["tg_sig"] = bool(x.tg_sig)
    if x.on is not None:
        c["on"] = x.on
    if c["on"] and not was:
        others = [o.name for o in _all_ctx() if o is not C() and _on_of(o)]
        if len(_tfs_of(c)) > 1:
            others.append(f"{C().name} chạy {c['tf']} (các khung độc lập)")
        if others:
            try:
                with mt5_service.session():
                    hed = _hedging(mt5.account_info())
            except Exception as e:
                return {"ok": False, "loi": f"Không đọc được MT5: {e}"}
            if not hed:
                return {"ok": False, "loi": f"Đang chạy {', '.join(others)}. Chạy nhiều bot cùng lúc cần tài khoản HEDGING "
                                            "(tài khoản này là Netting: các lệnh cùng mã gộp làm một, bot sẽ giẫm lên nhau)."}
    if c["on"] and c["strategy"] == "dca" and c.get("lot") and c["dca"].get("risk", 0) <= 0:
        big = dca_leg_lot(c["lot"], c["dca"], c["dca"]["legs"])
        if big > MAX_LOT + 1e-9:
            return {"ok": False, "loi": f"Lot {c['lot']} × {c['dca']['lot_mult']} qua {c['dca']['legs']} lệnh DCA → lệnh cuối {big:.2f} lot, vượt giới hạn {MAX_LOT} "
                                        "(PINYIN_MAX_LOT trong .env). Giảm lot / hệ số / số lệnh, hoặc tăng giới hạn."}
    _save(c)
    with _lock:
        C().st["last_bar"] = 0                  # đổi cài đặt / bật lại: chỉ vào theo tín hiệu MỚI
    nm = STRATS.get(c["strategy"], c["strategy"])
    if c["on"] != was:
        log(("BẬT" if c["on"] else "TẮT") + f" bot · {nm} · {c['tf']} · lot {c.get('lot') or 'theo vốn'}")
    elif c["on"]:
        log(f"Đổi cài đặt: {nm} · {c['tf']}", tele=False)
    return {"ok": True, "cfg": c}


class TgGroupIn(BaseModel):
    act: str = "connect"                # connect | off


@router.post("/auto/telegram_group")
def auto_telegram_group(x: TgGroupIn):
    """Nhóm / kênh Telegram cho khách nhận tín hiệu: thêm bot vào nhóm (kênh: làm quản trị), nhắn 1 tin trong nhóm,
    rồi bấm Kết nối: tìm nhóm mới nhất bot thấy trong getUpdates."""
    if x.act == "off":
        _set_env("TELEGRAM_SIGNAL_CHAT", "")
        _set_env("TELEGRAM_SIGNAL_TITLE", "")
        return {"ok": True}
    if not _env("TELEGRAM_BOT_TOKEN"):
        return {"ok": False, "loi": "Kết nối bot Telegram của chủ trước (ô token ở trên)."}
    try:
        up = _tg_api("getUpdates") or {}
        found = None
        for u in up.get("result", []):
            for k in ("message", "channel_post", "my_chat_member", "edited_message"):
                ch = (u.get(k) or {}).get("chat")
                if ch and ch.get("type") in ("group", "supergroup", "channel"):
                    found = ch
        if not found:
            me = _tg_api("getMe") or {}
            un = (me.get("result") or {}).get("username", "")
            return {"ok": False, "loi": f"Chưa thấy nhóm nào. Thêm bot @{un} vào nhóm (kênh: thêm làm quản trị viên), nhắn 1 tin bất kỳ trong nhóm rồi bấm Kết nối lại."}
        r = _tg_api("sendMessage", {"chat_id": found["id"], "text": "✅ Nhóm này sẽ nhận tín hiệu vào / đóng lệnh của bot PinYin. "
                                                                  "Tín hiệu chỉ để tham khảo, không phải lời khuyên đầu tư: https://pinyin.id.vn/mien-tru-trach-nhiem"})
        if not r or not r.get("ok"):
            return {"ok": False, "loi": f"Bot không gửi được tin vào nhóm {found.get('title')}: {(r or {}).get('description', '')}. Kênh thì bot phải là quản trị viên."}
        _set_env("TELEGRAM_SIGNAL_CHAT", str(found["id"]))
        _set_env("TELEGRAM_SIGNAL_TITLE", (found.get("title") or "nhóm").replace("\n", " ")[:60])
        return {"ok": True, "title": found.get("title")}
    except Exception as e:
        return {"ok": False, "loi": str(e)}


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


# ---------- Backtest lịch sử (chạy lại luật của bot trên nến đã qua, lệnh giả) ----------

class BtIn(BaseModel):
    days: int = 30
    cfg: dict = {}                      # cài đặt bot muốn thử (giống ô cài đặt trên trang); thiếu thì lấy cài đặt đang lưu
    lot: float | None = None            # lot cố định; None = theo bảng vốn
    cap: float = 1000                   # vốn ban đầu (đơn vị tiền của tài khoản)
    comm: float = 0.0                   # phí mỗi lot (khứ hồi)
    sweep: dict | None = None           # {"key": "dca.gap", "values": [0.2, 0.5, 1]} → so sánh nhiều giá trị
    strategy: str = "dca"               # chỉ còn dca (PinYin HA Pro DCA)


BT_PER_DAY = {"M1": 1440, "M5": 288, "M15": 96}
BT_SWEEP = {"dca." + k for k in DCA_NUM}


def _bt_sweep_keys():
    return BT_SWEEP


@router.post("/auto/backtest")
def auto_backtest(x: BtIn, slot: str = "main"):
    ctx = _ctx_of(slot) or _MAIN
    with _use(ctx):                              # không giữ khóa: backtest chạy lâu, bot vẫn chạy song song
        return _auto_backtest(x)


def _auto_backtest(x: BtIn):
    if "+" in str((x.cfg or {}).get("tf") or ""):
        return {"ok": False, "loi": "Bot chạy M1 + M5 là 2 chiến dịch độc lập: backtest từng khung (chọn M1, rồi M5), lãi/lỗ cộng lại."}
    from app.services.backtest import run as bt_run
    c = _cfg()
    over = dict(x.cfg or {})
    dca_over = over.pop("dca", None) or {}
    c.update({k: v for k, v in over.items() if k in DEFAULT and k not in ("on", "dca")})
    c["dca"] = {**c["dca"], **{k: v for k, v in dca_over.items() if k in DCA_DEF}}
    c["strategy"] = "dca"
    if c["tf"] not in TFS:
        return {"ok": False, "loi": "Khung chỉ M1, M5 hoặc M15."}
    if not (1 <= x.days <= 730):
        return {"ok": False, "loi": "Số ngày từ 1 đến 730."}
    if x.cap <= 0 or x.comm < 0 or (x.lot is not None and not (0.01 <= x.lot <= 1000)):
        return {"ok": False, "loi": "Vốn / phí / lot không hợp lệ."}
    sw = None
    if x.sweep:
        key, vals = str(x.sweep.get("key", "")), x.sweep.get("values") or []
        if key not in _bt_sweep_keys() or not (1 <= len(vals) <= 10):
            return {"ok": False, "loi": "So sánh: chọn thông số hợp lệ và 1–10 giá trị."}
        try:
            sw = (key, [float(v) for v in vals])
        except (TypeError, ValueError):
            return {"ok": False, "loi": "So sánh: giá trị phải là số."}
    tf_code = TFS[c["tf"]][0]
    want = min(200000, x.days * BT_PER_DAY[c["tf"]] + 400)
    try:
        with mt5_service.session():
            if not mt5.symbol_select(SYM, True):
                return {"ok": False, "loi": "Broker không có XAUUSD."}
            off, _ = server_offset()
            r = None
            n_try = want
            while n_try >= 500:          # xin quá số nến MT5 đang giữ thì MT5 trả rỗng: giảm dần tới mức có
                r = mt5.copy_rates_from_pos(SYM, tf_code, 1, n_try)          # bỏ nến đang chạy
                if r is not None and len(r):
                    break
                n_try = min(n_try - 1, 99000) if n_try > 99000 else n_try // 2
            info = mt5.symbol_info(SYM)
            a = mt5.account_info()
            px = float(r[-1]["close"]) if r is not None and len(r) else 0
            vpu = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, SYM, 1.0, px, px + 1.0) if px else None
    except Exception as e:
        return {"ok": False, "loi": str(e)}
    if r is None or len(r) < 500:
        return {"ok": False, "loi": "MT5 chưa có đủ lịch sử nến (mở biểu đồ XAUUSD khung này trên MT5 và kéo về quá khứ)."}
    if not vpu:
        vpu = float(info.trade_contract_size or 100)
    bars = [(int(q["time"]) - off, float(q["open"]), float(q["high"]), float(q["low"]), float(q["close"]),
             float(q["tick_volume"]), int(q["time"]) // 86400) for q in r]
    spr = [float(q["spread"]) * info.point for q in r]
    t_from = bars[-1][0] - x.days * 86400
    warm = next((i for i, b in enumerate(bars) if b[0] >= t_from), 0)
    warm = max(warm, 300)
    common = {"lot": x.lot, "cap0": x.cap, "vpu": vpu, "comm": x.comm, "warm": warm,
              "step": info.volume_step or 0.01, "vmin": info.volume_min or 0.01}
    rng = {"from": datetime.fromtimestamp(bars[warm][0], VN).strftime("%d/%m/%Y %H:%M"),
           "to": datetime.fromtimestamp(bars[-1][0], VN).strftime("%d/%m/%Y %H:%M"),
           "bars": len(bars) - warm, "short": bars[0][0] > t_from - 300 * TFS[c["tf"]][1]}
    meta = {"ccy": a.currency if a else "", "vpu": round(vpu, 4), "spread_avg": round(sum(spr[warm:]) / max(1, len(spr) - warm), 3)}
    try:
        if not sw:
            res = bt_run(bars, spr, c, **common)
            return {"ok": True, "range": rng, "meta": meta, "cfg": c, "res": res}
        key, vals = sw
        rows = []
        for v in vals:
            cc = json.loads(json.dumps(c))
            if key.startswith("dca."):
                k2 = key[4:]
                cc["dca"][k2] = DCA_NUM[k2][2](v)
            s = bt_run(bars, spr, cc, **common)
            rows.append({"v": v, **{k: s[k] for k in ("net", "net_pct", "n", "winrate", "pf", "mdd", "mdd_pct", "streak", "avg_win", "avg_loss")}})
        return {"ok": True, "range": rng, "meta": meta, "cfg": c, "sweep": {"key": key, "rows": rows}}
    except Exception as e:
        return {"ok": False, "loi": f"Lỗi backtest: {e}"}


# ---------- Danh sách bot (nhiều bot chạy song song) ----------

class SlotIn(BaseModel):
    act: str                            # add | name | del
    id: str = ""
    name: str = ""


@router.get("/auto/slots")
def auto_slots():
    ctxs = _all_ctx()
    bym = {}
    try:
        with mt5_service.session():
            for p in mt5.positions_get(symbol=SYM) or []:
                g = bym.setdefault(p.magic, [0, 0.0])
                g[0] += 1
                g[1] += p.profit + p.swap
    except Exception:
        pass
    out = []
    for ctx in ctxs:
        with _use(ctx):
            c = _cfg()
        n, pnl = 0, 0.0
        for cx in [ctx] + _kids_of(ctx):
            g = bym.get(cx.magic, [0, 0.0])
            n, pnl = n + g[0], pnl + g[1]
        nf = sum(1 for f in copytrade.load() if ctx.sid in copytrade.srcs_of(f))
        out.append({"id": ctx.sid, "name": ctx.name, "magic": ctx.magic, "copy": ctx.copy, "nfollow": nf, "on": bool(c.get("on")),
                    "strategy": c.get("strategy"), "sname": STRATS.get(c.get("strategy"), ""), "tf": c.get("tf"),
                    "lot": c.get("lot"), "npos": n, "pnl": round(pnl, 2), "msg": ctx.st.get("msg", "")})
    return {"ok": True, "bots": out, "max": MAX_BOTS}


@router.post("/auto/slots")
def auto_slots_set(x: SlotIn):
    with _lock:
        sl = _slots()
        name = (x.name or "").strip()[:30]
        if x.act == "add":
            if 1 + len(sl["bots"]) >= MAX_BOTS:
                return {"ok": False, "loi": f"Tối đa {MAX_BOTS} bot."}
            used_id = {b["id"] for b in sl["bots"]}
            used_m = {int(b["magic"]) for b in sl["bots"]}
            k = next(i for i in range(2, 100) if f"b{i}" not in used_id)
            m = next(SLOT_MAGIC0 + i for i in range(100) if SLOT_MAGIC0 + i not in used_m)
            sl["bots"].append({"id": f"b{k}", "name": name or f"Bot {k}", "magic": m})
            _slots_save(sl)
            _all_ctx()
            log(f"Thêm bot [{name or f'Bot {k}'}] (magic {m}).", "info", tele=False)
            return {"ok": True, "id": f"b{k}"}
        if x.act == "name":
            if not name:
                return {"ok": False, "loi": "Nhập tên bot."}
            if x.id == "main":
                sl["main"] = name
            else:
                b = next((b for b in sl["bots"] if b["id"] == x.id), None)
                if not b:
                    return {"ok": False, "loi": "Không có bot này."}
                b["name"] = name
            _slots_save(sl)
            _all_ctx()
            return {"ok": True}
        if x.act == "del":
            if x.id == "main":
                return {"ok": False, "loi": "Không xóa được Bot 1."}
            ctx = _ctx_of(x.id)
            if not ctx:
                return {"ok": False, "loi": "Không có bot này."}
            if _on_of(ctx):
                return {"ok": False, "loi": "Tắt bot trước khi xóa."}
            try:
                n = 0
                with mt5_service.session():
                    for cx in [ctx] + _kids_of(ctx):
                        with _use(cx):
                            n += len(_bot_positions())
            except Exception:
                n = 0
            if n:
                return {"ok": False, "loi": f"Bot còn {n} lệnh mở. Đóng hết rồi mới xóa."}
            nf = sum(1 for f in copytrade.load() if x.id in copytrade.srcs_of(f))
            if nf:
                return {"ok": False, "loi": f"Có {nf} tài khoản copy đang theo bot này. Đổi chúng sang bot khác rồi mới xóa."}
            sl["bots"] = [b for b in sl["bots"] if b["id"] != x.id]
            _slots_save(sl)
            _all_ctx()
            return {"ok": True}
        return {"ok": False, "loi": "Thao tác không hợp lệ."}


# ---------- Tài khoản vào lệnh (copy qua EA PinYinBridge) ----------
copytrade.set_logger(lambda m, k="info", tele=True: log(m, k, tele=tele))
copytrade._start()               # hàng đợi + tự đối chiếu tài khoản vào lệnh


class CopyIn(BaseModel):
    act: str                            # add | set | del | key
    id: str | None = None
    name: str | None = None
    on: bool | None = None
    lot_mode: str | None = None
    lot: float | None = None
    ttl: int | None = None              # lệnh vào chờ EA tối đa (giây)
    slip: float | None = None           # bỏ lệnh vào nếu giá đã chạy ngược quá X$ (0 = không xét)
    real: bool | None = None            # cho phép copy sang tài khoản THẬT này (bật cần mật khẩu chủ)
    src: str | None = None              # (cũ) copy theo 1 bot
    srcs: list[str] | None = None       # copy theo NHIỀU bot cùng lúc (id bot: main, b2, ...)
    pw: str = ""


@router.get("/auto/copy")
def copy_list():
    from app.api.ea import EA_LATEST
    return {"ok": True, "list": copytrade.status(), "ea_latest": EA_LATEST}


@router.post("/auto/copy")
def copy_set(x: CopyIn):
    try:
        if x.act == "add":
            f = copytrade.add(x.name or "")
            log(f"Thêm tài khoản vào lệnh [{f['name']}].", "info", tele=False)
            return {"ok": True, "item": f}
        if not x.id:
            return {"ok": False, "loi": "Thiếu tài khoản."}
        if x.act == "del":
            copytrade.update(x.id, delete=True)
            return {"ok": True}
        if x.act == "key":
            return {"ok": True, "item": copytrade.update(x.id, new_key=True)}
        if x.act == "set":
            if x.lot_mode is not None and x.lot_mode not in copytrade.LOT_MODES:
                return {"ok": False, "loi": "Cách tính lot không hợp lệ."}
            if x.lot is not None and not (0.01 <= x.lot <= 100):
                return {"ok": False, "loi": "Lot / hệ số từ 0.01 đến 100."}
            if x.ttl is not None and not (5 <= x.ttl <= 600):
                return {"ok": False, "loi": "Thời hạn lệnh vào từ 5 đến 600 giây."}
            if x.slip is not None and not (0 <= x.slip <= 100):
                return {"ok": False, "loi": "Trượt giá tối đa từ 0 đến 100$."}
            if x.src is not None and x.srcs is None:
                x.srcs = [x.src]
            if x.srcs is not None:
                x.srcs = list(dict.fromkeys(str(v) for v in x.srcs))
                if any(not _ctx_of(v) for v in x.srcs):
                    return {"ok": False, "loi": "Không có bot này."}
                dca = [bot_name(v) for v in x.srcs if strategy_of(v) == "dca"]
                if len(dca) > 1:
                    return {"ok": False, "loi": f"Mỗi tài khoản chỉ copy được 1 bot DCA cùng lúc ({', '.join(dca)}). Chọn 1 bot DCA cho tài khoản này."}
            if x.real:
                import hmac
                pw = os.getenv("PINYIN_PASSWORD", "")
                if not pw or not hmac.compare_digest(x.pw.encode(), pw.encode()):
                    return {"ok": False, "loi": "Sai mật khẩu chủ."}
            f = copytrade.update(x.id, name=(x.name or "").strip()[:40] or None, on=x.on, lot_mode=x.lot_mode, lot=x.lot,
                                 ttl=x.ttl, slip=x.slip, real=x.real, srcs=x.srcs, src=x.srcs[0] if x.srcs else None)
            if x.srcs is not None:
                log(f"[{f['name']}] copy theo: " + (", ".join(bot_name(v) for v in x.srcs) or "(không bot nào)") + ".", "info")
            if x.real is not None:
                log(f"{'🔓 CHO PHÉP' if x.real else '🔒 CHẶN'} copy lệnh sang tài khoản THẬT [{f['name']}].", "info")
            if x.on is not None:
                log(f"{'BẬT' if x.on else 'TẮT'} copy lệnh bot sang [{f['name']}].", "info")
            return {"ok": True, "item": f}
        return {"ok": False, "loi": "Thao tác không hợp lệ."}
    except ValueError as e:
        return {"ok": False, "loi": str(e)}

