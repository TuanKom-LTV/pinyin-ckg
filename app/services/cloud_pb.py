"""PinYin EMA Cloud Pullback — bản Python của pine/PinYin_EMA_Cloud_Pullback.pine (dùng cho biểu đồ, backtest, bot).

Mây EMA nhanh/chậm: xu hướng rõ (mây dày + EMA chậm dốc) → giá chạy ra khỏi mây → hồi về chạm EMA nhanh → BUY / SELL.
SL = đáy (BUY) / đỉnh (SELL) nhịp hồi ± slbuf × ATR; TP = giá vào ± rr × R.

Giữ đúng cách tính của TradingView:
- ta.ema: giá trị đầu là SMA(length), sau đó alpha = 2/(length+1).
- ta.atr: RMA (Wilder) của True Range, giá trị đầu là SMA 14 TR (ATR có sẵn của MT5 tính kiểu khác, KHÔNG dùng).
- Chỉ tính trên nến ĐÃ ĐÓNG (như alert once_per_bar_close), thứ tự cập nhật trạng thái y hệt Pine.
"""

CPB_DEF = {
    "fast": 9,          # EMA nhanh
    "slow": 21,         # EMA chậm
    "minw": 0.15,       # mây dày tối thiểu (× ATR)
    "slope": 3,         # EMA chậm dốc so với n nến trước
    "deep": 0.3,        # cho xuyên qua EMA chậm tối đa (× ATR)
    "conf": "touch",    # touch = Chạm mây (vào ngay) | rej = Nến từ chối | brk = Phá nến chạm
    "wait": 5,          # chờ xác nhận tối đa (nến)
    "gap": 5,           # cách tín hiệu trước cùng chiều ≥ (nến)
    "slbuf": 0.2,       # SL cách đáy/đỉnh nhịp hồi (× ATR)
    "rr": 1.5,          # TP = Entry ± R × rr
}
CONF_NAMES = {"touch": "Chạm mây (vào ngay)", "rej": "Nến từ chối", "brk": "Phá nến chạm"}
ATR_LEN = 14


def _ema(src, n):
    out = [None] * len(src)
    if len(src) < n:
        return out
    a = 2.0 / (n + 1)
    v = sum(src[:n]) / n
    out[n - 1] = v
    for i in range(n, len(src)):
        v = a * src[i] + (1 - a) * v
        out[i] = v
    return out


def _atr(h, l, c, n=ATR_LEN):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    out = [None] * len(c)
    if len(c) < n:
        return out
    v = sum(tr[:n]) / n
    out[n - 1] = v
    for i in range(n, len(c)):
        v = (v * (n - 1) + tr[i]) / n
        out[i] = v
    return out


def params(p=None):
    d = dict(CPB_DEF)
    for k, v in (p or {}).items():
        if k in d and v is not None:
            d[k] = v
    return d


def compute(bars, p=None):
    """bars: [(t, o, h, l, c, ...)] nến đã đóng, cũ → mới.
    Trả {"sig": [...], "e1": [...], "e2": [...], "st": [...], "atr": [...]}:
      sig: {"i", "t", "side", "go" (+1/-1), "e", "sl", "tp", "R"}
      st: trạng thái mây mỗi nến: 0 = mỏng (xám), 1 = EMA nhanh trên (xanh), -1 = EMA nhanh dưới (đỏ)."""
    d = params(p)
    o = [b[1] for b in bars]
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    c = [b[4] for b in bars]
    E1, E2, A = _ema(c, int(d["fast"])), _ema(c, int(d["slow"])), _atr(h, l, c)
    sn, deep, conf = int(d["slope"]), float(d["deep"]), d["conf"]
    st, sig = [], []
    # trạng thái BUY / SELL (var trong Pine)
    bSt, bExt, bTrig, bBar, armB, lastB = 0, None, None, 0, False, -100000
    sSt, sExt, sTrig, sBar, armS, lastS = 0, None, None, 0, False, -100000
    for i in range(len(c)):
        e1, e2, atr = E1[i], E2[i], A[i]
        e2p = E2[i - sn] if i >= sn else None
        if e1 is None or e2 is None or atr is None:
            st.append(0)
            continue                                   # Pine: giá trị na → mọi so sánh sai, trạng thái giữ nguyên
        thick = abs(e1 - e2) >= d["minw"] * atr
        upT = e2p is not None and e1 > e2 and e2 > e2p and thick
        dnT = e2p is not None and e1 < e2 and e2 < e2p and thick
        st.append(0 if not thick else 1 if e1 > e2 else -1)

        # ---- BUY ----
        buy = False
        if e1 < e2:
            armB = False
        if bSt == 1:
            if c[i] < e2 - deep * atr or e1 <= e2 or i - bBar > d["wait"]:
                bSt = 0
            else:
                bExt = min(bExt, l[i])
                ok = (c[i] > o[i] and c[i] > e1) if conf == "rej" else (c[i] > bTrig and c[i] > e1 and c[i] > o[i])
                if ok:
                    buy = True
                elif l[i] <= e1:
                    bTrig, bBar = h[i], i
        touchB = (upT and armB and l[i] <= e1 and l[i] >= e2 - deep * atr and c[i] >= e2 - deep * atr
                  and i - lastB >= d["gap"])
        if not buy and bSt == 0 and touchB:
            bSt, bExt, bTrig, bBar = 1, l[i], h[i], i
            if conf == "touch" or (conf == "rej" and c[i] > o[i] and c[i] > e1):
                buy = True
        if buy:
            bSt, armB, lastB = 0, False, i
        elif upT and l[i] > e1:
            armB = True

        # ---- SELL ----
        sell = False
        if e1 > e2:
            armS = False
        if sSt == 1:
            if c[i] > e2 + deep * atr or e1 >= e2 or i - sBar > d["wait"]:
                sSt = 0
            else:
                sExt = max(sExt, h[i])
                ok = (c[i] < o[i] and c[i] < e1) if conf == "rej" else (c[i] < sTrig and c[i] < e1 and c[i] < o[i])
                if ok:
                    sell = True
                elif h[i] >= e1:
                    sTrig, sBar = l[i], i
        touchS = (dnT and armS and h[i] >= e1 and h[i] <= e2 + deep * atr and c[i] <= e2 + deep * atr
                  and i - lastS >= d["gap"])
        if not sell and sSt == 0 and touchS:
            sSt, sExt, sTrig, sBar = 1, h[i], l[i], i
            if conf == "touch" or (conf == "rej" and c[i] < o[i] and c[i] < e1):
                sell = True
        if sell:
            sSt, armS, lastS = 0, False, i
        elif dnT and h[i] < e1:
            armS = True

        for go, on, ext in ((1, buy, bExt), (-1, sell, sExt)):
            if not on:
                continue
            sl = ext - go * d["slbuf"] * atr
            R = go * (c[i] - sl)
            sig.append({"i": i, "t": bars[i][0], "side": "BUY" if go > 0 else "SELL", "go": go, "e": c[i], "sl": sl,
                        "tp": c[i] + go * d["rr"] * R, "R": R, "atr": atr})
    return {"sig": sig, "e1": E1, "e2": E2, "st": st, "atr": A}


# ---------------- Cài đặt bot / backtest (quản lý lệnh) ----------------

BOT_DEF = {
    **CPB_DEF,
    "mg": "pine",       # pine = 1 SL + 1 TP như Pine | half = chốt một phần ở half_r rồi dời SL hòa vốn
    "half_r": 1.0,      # chốt một phần khi lãi đạt ... R
    "half_pct": 50,     # % khối lượng chốt
    "be_off": 0.0,      # SL hòa vốn = giá vào ± ... $ (khóa lãi)
    "tr_on": False,     # trailing stop
    "tr_at": 1.0,       # bắt đầu trailing khi lãi đạt ... R
    "tr_dist": 0.5,     # SL bám cách giá ... R
    "legs": 10,         # tối đa số lệnh cùng chiều (tín hiệu cùng chiều thì vào thêm)
    "rev": True,        # tín hiệu ngược chiều: đóng hết lệnh đang mở rồi đảo chiều
    "min_sl": 0.0,      # bỏ tín hiệu có SL gần hơn ... $ (0 = không xét)
    "max_sl": 0.0,      # bỏ tín hiệu có SL xa hơn ... $ (0 = không xét)
    "mart": "off",      # lệnh trước dính SL thì lệnh sau tăng lot: off | x (nhân) | add (cộng)
    "mart_v": 2.0,      # nhân ×... hoặc cộng +... lot
    "mart_max": 3,      # tăng tối đa ... lần liên tiếp, lệnh thắng thì về lot gốc
    "max_spr": 0.0,     # bỏ tín hiệu khi spread lớn hơn ... $ (0 = không xét)
    "sess_on": False,
    "sess": "1400-2300",
}
NUM = {"fast": (2, 100, int), "slow": (3, 200, int), "minw": (0, 5, float), "slope": (1, 50, int), "deep": (0, 5, float),
       "wait": (1, 50, int), "gap": (1, 500, int), "slbuf": (0, 5, float), "rr": (0.1, 20, float),
       "half_r": (0.1, 10, float), "half_pct": (1, 99, int), "be_off": (0, 50, float), "tr_at": (0.1, 20, float),
       "tr_dist": (0.05, 20, float), "legs": (1, 10, int), "min_sl": (0, 100, float), "max_sl": (0, 500, float),
       "mart_v": (0.01, 10, float), "mart_max": (0, 10, int), "max_spr": (0, 50, float)}
ENUM = {"conf": ("touch", "rej", "brk"), "mg": ("pine", "half"), "mart": ("off", "x", "add")}
BOOL = ("tr_on", "rev", "sess_on")


def bot_params(over=None, base=None):
    """Gộp + kiểm tra cài đặt. Trả (dict, None) hoặc (None, lỗi)."""
    d = dict(BOT_DEF)
    d.update({k: v for k, v in (base or {}).items() if k in BOT_DEF})
    for k, v in (over or {}).items():
        if k in NUM:
            lo, hi, typ = NUM[k]
            try:
                v = typ(v)
            except (TypeError, ValueError):
                return None, f"Cloud PB: {k} không hợp lệ."
            if not lo <= v <= hi:
                return None, f"Cloud PB: {k} phải từ {lo} đến {hi}."
            d[k] = v
        elif k in ENUM:
            if v not in ENUM[k]:
                return None, f"Cloud PB: {k} không hợp lệ."
            d[k] = v
        elif k in BOOL:
            d[k] = bool(v)
        elif k == "sess":
            d[k] = str(v).replace(" ", "").replace(":", "")
    if d["slow"] <= d["fast"]:
        return None, "Cloud PB: EMA chậm phải lớn hơn EMA nhanh."
    if d["max_sl"] and d["min_sl"] > d["max_sl"]:
        return None, "Cloud PB: SL tối thiểu phải nhỏ hơn SL tối đa."
    return d, None


def mart_lot(lot, d, step_n, vstep=0.01):
    """Lot lệnh mới sau step_n lệnh dính SL liên tiếp."""
    n = min(step_n, d["mart_max"]) if d["mart"] != "off" else 0
    v = lot * d["mart_v"] ** n if d["mart"] == "x" else lot + d["mart_v"] * n if d["mart"] == "add" else lot
    return max(vstep, round(round(v / vstep) * vstep, 2))
