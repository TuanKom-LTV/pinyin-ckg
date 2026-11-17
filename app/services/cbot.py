"""Bot PinYin HA Pro DCA riêng cho từng KHÁCH (chủ duyệt mới được dùng).

- Tín hiệu và giá: MT5 của máy chủ (cùng nguồn với bot của chủ). Mỗi khách có cài đặt riêng (khung, lot, thông số DCA).
- Máy chủ chạy chiến dịch DCA "ảo" cho từng khách theo đúng luật của bot chủ (ckg_auto._dca_live / _dca_bar):
  mở / thêm lệnh, chốt TP1 / TP2 từng phần, dời hòa vốn, trail EMA21, SL mềm VWAP, hết giờ, đảo chiều.
- Mỗi hành động gửi xuống MT5 của khách qua EA PinYinBridge (≥ 1.6) bằng hàng đợi của copytrade, phạm vi "B":
  chỉ áp lên lệnh của bot (ghi chú "PinYin BOT"), không đụng lệnh khách vào tay qua buồng lái.
  TP1 đặt sẵn trên sàn của khách (EA tách lệnh), sàn chốt TP1 xong EA tự dời hòa vốn.
- Lưu ở data/cbot/<mã khách>.json (cài đặt + chiến dịch đang chạy), khởi động lại máy chủ vẫn giữ.
"""

import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

from app.services import copytrade
from app.services.ha_pro_dca import dca_signals
from app.services.mt5_service import mt5_service

ROOT = Path(__file__).resolve().parent.parent.parent
DIR = ROOT / "data" / "cbot"
SYM = "XAUUSD"
VN = timezone(timedelta(hours=7))
TFS = {"M1": (mt5.TIMEFRAME_M1, 60), "M5": (mt5.TIMEFRAME_M5, 300), "M15": (mt5.TIMEFRAME_M15, 900)}
TIERS = [(100, 3, .01), (300, 5, .01), (600, 3, .02), (900, 5, .02), (1000, 3, .03), (1200, 5, .03)]
EMPTY = {"dir": 0, "rev": False, "sl": None, "legs": 0, "lot": None, "last_px": None, "last_t": 0, "start_t": 0,
         "tp1": False, "tp2": False, "tp1px": None, "tp2px": None, "vol_tp": 0.0, "sl_be": None}
_lock = threading.RLock()
_C = {}                 # mã khách → trạng thái
_bars = {}              # khung → {"t": nến đóng cuối, "bars": [...]}
_sigs = {}              # (khung, sig, nến) → (t, danh sách tín hiệu)
_started = False


def _now():
    return datetime.now(VN).strftime("%d/%m %H:%M:%S")


def _file(u):
    return DIR / (u.replace(":", "_") + ".json")


def _default_cfg():
    from app.api.ckg_auto import DCA_DEF
    return {"on": False, "tf": "M5", "lot": 0.01, "dca": {**DCA_DEF, "risk": 0.0}}


def get(u):
    with _lock:
        if u not in _C:
            c = {"cfg": _default_cfg(), "E": dict(EMPTY), "P": [], "last_bar": 0, "sig": None, "log": [], "msg": "Đang tắt"}
            try:
                d = json.loads(_file(u).read_text(encoding="utf-8"))
                c["cfg"] = {**c["cfg"], **d.get("cfg", {})}
                c["cfg"]["dca"] = {**_default_cfg()["dca"], **(d.get("cfg", {}).get("dca") or {}), "risk": 0.0}
                c["E"] = {**EMPTY, **d.get("E", {})}
                c["P"] = d.get("P", [])
                c["log"] = d.get("log", [])[-60:]
            except Exception:
                pass
            c["log"] = deque(c["log"], maxlen=60)
            _C[u] = c
        return _C[u]


def save(u):
    c = _C.get(u)
    if not c:
        return
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        _file(u).write_text(json.dumps({"cfg": c["cfg"], "E": c["E"], "P": c["P"], "log": list(c["log"])}, ensure_ascii=False),
                            encoding="utf-8")
    except Exception:
        pass


def log(u, msg, kind="info"):
    c = get(u)
    c["log"].appendleft({"t": _now(), "m": msg, "k": kind})
    save(u)


# ---------- đích gửi lệnh (EA của khách) ----------

def _target(u):
    from app.core import users
    email, d = users.by_id(u)
    f = copytrade.EXTRA.get("cb-" + u)
    if not f:
        f = {"id": "cb-" + u, "uid": u, "own": True, "scope": "B", "ttl": 60, "slip": 0, "lot_mode": "same",
             "log": lambda m, k="info", _u=u: log(_u, m, k),
             "dir_fn": lambda _u=u: get(_u)["E"].get("dir", 0)}
        copytrade.EXTRA[f["id"]] = f
    f["name"] = (d or {}).get("ten") or email or u
    f["on"] = bool(get(u)["cfg"].get("on"))
    return f


def _ea(u):
    from app.api import ea
    s = ea._st.get(u) or {}
    return s, bool(s.get("acc")) and ea._online(s), copytrade._ver_ok(s, (1, 6))


def _act(u, line, what, extra=None):
    copytrade.enqueue(_target(u), line, what, extra)


# ---------- chiến dịch ảo (bản sao luật bot chủ) ----------

def _avg(P):
    v = sum(p["lot"] for p in P)
    return sum(p["e"] * p["lot"] for p in P) / v if v else None


def _tier(eq):
    b = None
    for x in TIERS:
        if eq is not None and eq >= x[0]:
            b = x
    return b


def _cap(d, b):
    return min(d["legs"], b[1] if b else 1)


def _levels(u, c, sg=None):
    E, P, d = c["E"], c["P"], c["cfg"]["dca"]
    a = _avg(P)
    if a is None or E["sl"] is None or E["tp1"]:
        return
    R = E["dir"] * (a - E["sl"])
    if R <= 0:
        return
    t1 = a + E["dir"] * d["tp1r"] * R
    t2 = a + E["dir"] * d["tp2r"] * R
    if d["cap"] and sg:
        if E["dir"] == 1:
            band = min(sg["up"], sg["upx"]) if sg["ready"] and sg["upx"] is not None else sg["up"]
        else:
            band = max(sg["lo"], sg["lox"]) if sg["ready"] and sg["lox"] is not None else sg["lo"]
        if E["dir"] * (band - t1) > 0 and E["dir"] * (band - t2) < 0:
            t2 = band
    E["tp1px"], E["tp2px"] = t1, t2
    copytrade.set_plan_for(u, abs(t1 - a), d["be"], d["be_off"])


def _reset(u, c):
    c["E"] = dict(EMPTY)
    c["P"] = []
    copytrade.set_plan_for(u, 0)


def _vpart(c, pct):
    """Chốt ảo pct% khối lượng (tính trên khối lượng lúc chạm TP1). Trả tỉ lệ so với khối lượng đang có."""
    E, P = c["E"], c["P"]
    want = math.floor(E["vol_tp"] * pct / 100 / 0.01 + 1e-9) * 0.01
    tot = sum(p["lot"] for p in P)
    if want < 0.01 - 1e-9 or tot <= 0:
        return 0
    left = round(want, 2)
    for p in list(P):
        if left < 0.01 - 1e-9:
            break
        v = min(p["lot"], left)
        if 1e-9 < p["lot"] - v < 0.01 - 1e-9:
            v = p["lot"]
        p["lot"] = round(p["lot"] - v, 2)
        left = round(left - v, 2)
        if p["lot"] < 0.01 - 1e-9:
            P.remove(p)
    return min(1.0, want / tot)


def _tick(u, c, bid, ask):
    """Mỗi vòng (2 giây): SL ảo, TP1 / TP2 theo giá thực, dời hòa vốn."""
    E, P, d = c["E"], c["P"], c["cfg"]["dca"]
    if not E["dir"]:
        return
    dr = E["dir"]
    for p in list(P):
        if (dr > 0 and bid <= p["sl"]) or (dr < 0 and ask >= p["sl"]):
            P.remove(p)
    if not P:
        log(u, f"Chiến dịch {'BUY' if dr > 0 else 'SELL'} kết thúc (chạm SL {'hòa vốn' if E['tp1'] else 'chung'}).", "close")
        _act(u, "CLOSE|0", "đóng chiến dịch")
        _reset(u, c)
        save(u)
        return
    px = bid if dr > 0 else ask
    tp2p = max(0, min(d["tp2p"], 100 - d["tp1p"]))
    changed = False
    if not E["tp1"] and E["tp1px"] and dr * (px - E["tp1px"]) >= 0:
        a0 = _avg(P)
        E["tp1"], E["vol_tp"], changed = True, round(sum(p["lot"] for p in P), 2), True
        fr = _vpart(c, d["tp1p"])
        copytrade.set_plan_for(u, 0)
        if fr > 0:
            _act(u, f"TP1PART|{fr:.4f}", "chốt TP1")
        log(u, f"Chạm TP1 {E['tp1px']:.2f} → chốt {d['tp1p']}%", "close")
        if d["be"] and P:
            be = a0 + dr * d["be_off"]
            for p in P:
                if dr * (be - p["sl"]) > 0:
                    p["sl"] = be
            E["sl_be"] = be
            _act(u, f"BEAVG|{float(d['be_off']):.2f}", "dời SL hòa vốn")
        if tp2p == 0:
            E["tp2"] = True
    if E["tp1"] and not E["tp2"] and tp2p > 0 and E["tp2px"] and P and dr * (px - E["tp2px"]) >= 0:
        E["tp2"], changed = True, True
        fr = _vpart(c, tp2p)
        if fr > 0:
            _act(u, f"PART|{fr:.4f}", "chốt TP2")
        log(u, f"Chạm TP2 {E['tp2px']:.2f} → chốt {tp2p}%", "close")
    if not P:
        _act(u, "CLOSE|0", "đóng chiến dịch")
        _reset(u, c)
        changed = True
    if changed:
        save(u)


def _in_sess(d, t_utc):
    if not d.get("sess_on"):
        return True
    try:
        a, b = d.get("sess", "").split("-")
        f = lambda x: int(x[:2]) * 60 + int(x[2:])
        a, b = f(a), f(b)
    except Exception:
        return True
    m = ((int(t_utc) + 7 * 3600) % 86400) // 60
    return a <= m < b if a <= b else (m >= a or m < b)


def _open(u, c, dr, sl, sg, b, bid, ask, first, why=""):
    E, P, d, cfg = c["E"], c["P"], c["cfg"]["dca"], c["cfg"]
    s, online, v16 = _ea(u)
    if not online or not v16:
        log(u, f"Bỏ tín hiệu {'BUY' if dr > 0 else 'SELL'}: " + ("EA trên MT5 của bạn đang offline." if not online else "EA cần bản 1.6 (tải lại EA)."), "err")
        return
    px = ask if dr > 0 else bid
    if dr * (px - sl) <= 0.1:
        log(u, f"Bỏ tín hiệu: giá {px:.2f} đã quá sát / vượt SL {sl:.2f}.", "err")
        return
    if first:
        lot = cfg.get("lot") or (b[2] if b else None)
        if not lot:
            log(u, "Bỏ tín hiệu: vốn dưới 100 của bảng vốn (chọn lot cố định).", "err")
            return
    else:
        lot = E["lot"]
    lot = round(float(lot), 2)
    n = 1 if first else E["legs"] + 1
    P.append({"lot": lot, "e": px, "sl": sl})
    if first:
        c["E"] = E = {**EMPTY, "dir": dr, "rev": bool(sg["rev"]), "sl": sl, "legs": 1, "lot": lot,
                      "last_px": sg["c"], "last_t": sg["t"], "start_t": sg["t"]}
    else:
        E.update({"legs": n, "last_px": sg["c"], "last_t": sg["t"]})
    _levels(u, c, sg)
    side = "BUY" if dr > 0 else "SELL"
    _act(u, f"DORD|{side}|{lot:.2f}|{abs(px - sl):.2f}|{float(d['tp1p'] if not E['tp1'] else 0):.0f}", f"vào {side}",
         {"side": side, "px": px})
    kind = side + (" ĐC*" if E["rev"] else "")
    lbl = (f"Mở chiến dịch {kind}" + (f" ({why})" if why else "")) if first else f"Thêm lệnh DCA {n}/{_cap(d, b)} {kind}"
    log(u, f"{lbl}: {lot} lot @ {px:.2f} · SL chung {sl:.2f} · TP1 {(E['tp1px'] or 0):.2f} · TP2 {(E['tp2px'] or 0):.2f}", "open")
    save(u)


def _bar(u, c, bars, sigs, b, bid, ask):
    """Nến mới đóng: thoát chủ động, mở chiến dịch / thêm lệnh DCA, cập nhật TP."""
    d = c["cfg"]["dca"]
    sg = sigs[-1] if sigs else None
    if not sg:
        return
    E, P = c["E"], c["P"]
    since = lambda t0: sum(1 for x in bars if x[0] > t0)
    dr = E["dir"]
    if dr and not P:
        _reset(u, c)
        E, P, dr = c["E"], c["P"], 0
    go = sg["go"]
    if go:
        c["sig"] = {"t": go, "side": "BUY" if go > 0 else "SELL", "e": sg["c"], "at": _now()}
        log(u, f"Tín hiệu {'BUY' if go > 0 else 'SELL'}{' ĐC*' if sg['rev'] else ''} {c['cfg']['tf']} @ {sg['c']:.2f}", "info")
    closed = want_rev = False
    if dr:
        cl, vw, sd = sg["c"], sg["vwap"], sg["sd"]
        opp = go == -dr
        soft = d["soft"] and not E["rev"] and sg["ready"] and (cl < vw - d["soft_sig"] * sd if dr > 0 else cl > vw + d["soft_sig"] * sd)
        tout = d["time"] > 0 and not E["tp1"] and since(E["start_t"]) >= d["time"]
        tr_on = E["tp1"] if d["trail_after"] == 1 else E["tp2"]
        trail = d["trail"] and tr_on and (cl < sg["ema21"] if dr > 0 else cl > sg["ema21"])
        why = "tín hiệu ngược" if opp else "SL mềm VWAP" if soft else "hết giờ" if tout else "trail EMA21" if trail else ""
        if why:
            _act(u, "CLOSE|0", f"đóng chiến dịch ({why})")
            log(u, f"Đóng chiến dịch ({why})", "close")
            _reset(u, c)
            E, P = c["E"], c["P"]
            closed = not (opp and d["rev"])
            want_rev = opp and d["rev"]
            dr = 0
    if go and not _in_sess(d, sg["t"]):
        go = 0
    if not dr and not closed and go:
        if (go > 0 and d["dir"] == "sell") or (go < 0 and d["dir"] == "buy"):
            pass
        else:
            cl, vw, sd, atr = sg["c"], sg["vwap"], sg["sd"], sg["atr"]
            ok = sg["rev"] or not sg["ready"] or (cl <= vw + d["stretch"] * sd if go > 0 else cl >= vw - d["stretch"] * sd)
            if not ok:
                log(u, f"Bỏ {'BUY' if go > 0 else 'SELL'}: giá cách VWAP quá {d['stretch']}σ.", "info")
            else:
                if sg["rev"]:
                    sl = sg["ext"] - go * d["sl_buf"]
                else:
                    slv = vw - go * d["sl_k"] * atr
                    slw = (sg["swl"] - d["sl_buf"]) if go > 0 else (sg["swh"] + d["sl_buf"])
                    sl = slv if d["sl_mode"] == "vwap" else slw if d["sl_mode"] == "swing" else (min(slv, slw) if go > 0 else max(slv, slw))
                _open(u, c, go, sl, sg, b, bid, ask, True, "đảo chiều" if want_rev else "")
    elif dr and go == dr and not E["tp1"] and E["legs"] < _cap(d, b):
        gap_ok = (sg["c"] <= E["last_px"] - d["gap"] * sg["atr"] if dr > 0 else sg["c"] >= E["last_px"] + d["gap"] * sg["atr"]) \
            and since(E["last_t"]) >= d["min_bars"]
        struct_ok = (sg["c"] > E["sl"] if dr > 0 else sg["c"] < E["sl"]) and (E["rev"] or (sg["c"] > sg["vwap"] if dr > 0 else sg["c"] < sg["vwap"]))
        if gap_ok and struct_ok:
            _open(u, c, dr, E["sl"], sg, b, bid, ask, False)
    if c["E"]["dir"]:
        _levels(u, c, sg)
    save(u)


# ---------- vòng chạy ----------

def _customers():
    """Khách được chủ cho dùng bot, còn hạn, có quyền giao dịch, và đang BẬT bot."""
    from app.core import users
    out = []
    for email, d in users.load().items():
        uid = d.get("id")
        if uid and d.get("bot") and users.can_trade(uid):
            c = get(uid)
            if c["cfg"].get("on"):
                out.append(uid)
    return out


def _bars_for(tf, off):
    code, sec = TFS[tf]
    r2 = mt5.copy_rates_from_pos(SYM, code, 0, 3)
    if r2 is None or len(r2) < 2:
        return None, None
    t_closed = int(r2[-2]["time"]) - off
    cc = _bars.get(tf)
    if not cc or cc["t"] != t_closed:
        r = mt5.copy_rates_from_pos(SYM, code, 0, 1500)
        if r is None or len(r) < 300:
            return None, None
        bars = [(int(x["time"]) - off, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                 float(x["tick_volume"]), int(x["time"]) // 86400) for x in r[:-1]]
        _bars[tf] = cc = {"t": bars[-1][0], "bars": bars}
    return cc["t"], cc["bars"]


def _signals(tf, d, t, bars):
    k = (tf, d.get("sig", "v5"), d.get("candles", "std"))
    hit = _sigs.get(k)
    if hit and hit[0] == t:
        return hit[1]
    sg = dca_signals(bars, d)
    _sigs[k] = (t, sg)
    return sg


def _loop():
    from app.api.ckg import server_offset
    from app.api import ea
    while True:
        try:
            us = _customers()
            for u in list(copytrade.EXTRA):            # khách tắt bot / bị thu quyền: thôi đối chiếu
                if copytrade.EXTRA[u].get("own") and copytrade.EXTRA[u]["uid"] not in us:
                    copytrade.EXTRA[u]["on"] = False
            if us:
                with mt5_service.session():
                    if not mt5.symbol_select(SYM, True):
                        raise RuntimeError("Máy chủ không có XAUUSD")
                    off, _ = server_offset()
                    tk = mt5.symbol_info_tick(SYM)
                    data = {}
                    for u in us:
                        tf = get(u)["cfg"]["tf"]
                        if tf not in data:
                            data[tf] = _bars_for(tf, off)
                if not tk or tk.bid <= 0:
                    time.sleep(2)
                    continue
                for u in us:
                    with _lock:
                        c = get(u)
                        _target(u)
                        t, bars = data.get(c["cfg"]["tf"], (None, None))
                        _tick(u, c, tk.bid, tk.ask)
                        if not t:
                            c["msg"] = "Chưa đủ nến"
                            continue
                        s = ea._st.get(u) or {}
                        eq = (s.get("acc") or {}).get("equity")
                        b = _tier(eq)
                        c["tierlot"] = b[2] if b else None
                        c["msg"] = f"Đang theo dõi {c['cfg']['tf']} · nến đóng {datetime.fromtimestamp(t, VN):%H:%M}"
                        if not c["last_bar"]:
                            c["last_bar"] = t                 # vừa bật: không vào theo tín hiệu cũ
                        if t > c["last_bar"]:
                            c["last_bar"] = t
                            _bar(u, c, bars, _signals(c["cfg"]["tf"], c["cfg"]["dca"], t, bars), b, tk.bid, tk.ask)
        except Exception as e:
            for u in list(_C):
                _C[u]["msg"] = f"Lỗi: {e}"
        from app.services.health import beat
        beat("cbot")
        time.sleep(2)


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="pinyin-cbot").start()


# ---------- cho API ----------

def status(u):
    c = get(u)
    s, online, v16 = _ea(u)
    E = c["E"]
    pos = [{"ticket": p["ticket"], "t": p["t"], "lot": p["lot"], "e": p["e"], "sl": p["sl"], "tp": p["tp"], "profit": p["profit"]}
           for p in (s.get("pos") or []) if p.get("bot")]
    cfg = {**c["cfg"], "strategy": "dca"}
    return {"ok": True, "cfg": cfg, "msg": c.get("msg", "Đang tắt") if cfg.get("on") else "Đang tắt", "check": 0,
            "lot": c.get("tierlot"), "sig": c.get("sig"), "pos": pos, "log": list(c["log"])[:30],
            "camp": {k: E.get(k) for k in ("dir", "rev", "legs", "lot", "sl", "sl_be", "tp1", "tp2", "tp1px", "tp2px")},
            "tg": False, "tg_chat": False, "cust": True,
            "ea": {"online": online, "v16": v16, "ver": s.get("ver")}}


def set_cfg(u, x):
    """x: dict từ trang (tf, lot, lot_auto, dca{...}, on). Kiểm tra giống bot chủ."""
    from app.api.ckg_auto import DCA_NUM, DCA_ENUM, DCA_BOOL, TFS as BTF, _sess_ok
    with _lock:
        c = get(u)
        cfg = dict(c["cfg"])
        if x.get("tf") is not None:
            if x["tf"] not in BTF:
                return {"ok": False, "loi": "Khung chỉ M1, M5 hoặc M15."}
            if x["tf"] != cfg["tf"]:
                c["last_bar"] = 0
            cfg["tf"] = x["tf"]
        if x.get("lot_auto"):
            cfg["lot"] = None
        elif x.get("lot") is not None:
            try:
                lot = round(float(x["lot"]), 2)
            except (TypeError, ValueError):
                return {"ok": False, "loi": "Lot không hợp lệ."}
            if not (0.01 <= lot <= 50):
                return {"ok": False, "loi": "Lot từ 0.01 đến 50."}
            cfg["lot"] = lot
        if isinstance(x.get("dca"), dict):
            d = dict(cfg["dca"])
            for k, v in x["dca"].items():
                if k == "risk":
                    continue                                  # khách: lot cố định hoặc theo vốn
                if k in DCA_NUM:
                    lo_, hi_, typ = DCA_NUM[k]
                    try:
                        v = typ(v)
                    except (TypeError, ValueError):
                        return {"ok": False, "loi": f"DCA: {k} không hợp lệ."}
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
                        return {"ok": False, "loi": "Khung giờ dạng 1400-2300 (giờ VN)."}
                    d[k] = v
            d["risk"] = 0.0
            cfg["dca"] = d
        if x.get("on") is not None:
            on = bool(x["on"])
            if on:
                s, online, v16 = _ea(u)
                if not online:
                    return {"ok": False, "loi": "EA trên MT5 của bạn đang offline. Mở MT5, gắn EA PinYinBridge rồi bật bot."}
                if not v16:
                    return {"ok": False, "loi": "Bot cần EA PinYinBridge bản 1.6: tải EA mới, chép đè, gỡ khỏi chart rồi gắn lại."}
                if not s["acc"].get("algo"):
                    return {"ok": False, "loi": "MT5 của bạn đang TẮT nút Algo Trading."}
            if on != bool(cfg.get("on")):
                c["last_bar"] = 0
                log(u, "BẬT bot." if on else "TẮT bot. Lệnh bot đang mở vẫn giữ SL trên MT5.", "info")
            cfg["on"] = on
        c["cfg"] = cfg
        save(u)
        _target(u)
    if cfg.get("on"):
        start()
        copytrade._start()
    return {"ok": True}


def brief(u):
    """Tóm tắt cho trang quản lý của chủ."""
    c = get(u)
    E = c["E"]
    return {"on": bool(c["cfg"].get("on")), "tf": c["cfg"]["tf"], "lot": c["cfg"].get("lot"),
            "camp": ("BUY" if E["dir"] > 0 else "SELL") + f" {E['legs']} lệnh" if E.get("dir") else None}
