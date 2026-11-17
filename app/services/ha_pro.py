"""Chiến lược "PinYin HA Pro" — chuyển nguyên từ code Pine Script (TradingView) của chủ sang Python.

Đầu vào: nến đã đóng [(t_utc, o, h, l, c, volume, day_key), ...] theo thứ tự thời gian.
Tính lần lượt từng nến giống TradingView (biến var giữ giá trị qua các nến):
  [0] VWAP neo theo ngày (Session)          [1] TMA logic = SMA(SMA(close,20),20), dải ± 2.2 × ATR(100)
  [3] EMA 9/21, vùng EMA = [min, max]       [4] tín hiệu gốc: nến Heikin Ashi + VWAP + TMA (thuận / đảo chiều)
  [5] máy trạng thái SETUP → phá vùng EMA → hồi về vùng EMA + nến đóng đúng chiều → VÀO LỆNH
  [6B] SL = đỉnh/đáy cao/thấp nhất N nến ± buffer, TP1..TP4 = R:R 1:1 → 1:4
"""

import math

DEFAULT = {"len": 40, "atr_len": 100, "atr_mult": 2.2, "ema_fast": 9, "ema_slow": 21, "max_wait": 30,
           "range_limit": 10, "sl_buffer": 2.0, "sl_lookback": 20}


def _sma(a, n):
    out, s = [None] * len(a), 0.0
    q = []
    for i, v in enumerate(a):
        if v is None:
            q, s = [], 0.0
            continue
        q.append(v)
        s += v
        if len(q) > n:
            s -= q.pop(0)
        if len(q) == n:
            out[i] = s / n
    return out


def _ema(a, n):
    k, out, e = 2 / (n + 1), [], None
    for v in a:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def _atr(h, l, c, n):
    """ta.atr = RMA của True Range (giống Pine)."""
    out, rma, trs = [None] * len(c), None, []
    for i in range(len(c)):
        tr = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        if rma is None:
            trs.append(tr)
            if len(trs) == n:
                rma = sum(trs) / n
        else:
            rma = (rma * (n - 1) + tr) / n
        out[i] = rma
    return out


def analyze(bars, p=None):
    """Trả danh sách lệnh vào: [{"i", "t", "side", "kind", "e", "sl", "tp":[tp1..tp4]}] (theo thứ tự nến)."""
    p = {**DEFAULT, **(p or {})}
    n = len(bars)
    if n < p["atr_len"] + p["len"] + 5:
        return []
    o = [b[1] for b in bars]
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    c = [b[4] for b in bars]

    # [0] VWAP theo ngày, nguồn hlc3, khối lượng tick
    vwap, cpv, cv, day = [], 0.0, 0.0, None
    for b in bars:
        if b[6] != day:
            day, cpv, cv = b[6], 0.0, 0.0
        src = (b[2] + b[3] + b[4]) / 3
        cpv += src * b[5]
        cv += b[5]
        vwap.append(cpv / cv if cv else None)

    # [1] TMA logic
    half = p["len"]
    mid = _sma(_sma(c, math.floor(half / 2)), math.ceil(half / 2))
    atr = _atr(h, l, c, p["atr_len"])
    up = [m + a * p["atr_mult"] if m is not None and a is not None else None for m, a in zip(mid, atr)]
    lo = [m - a * p["atr_mult"] if m is not None and a is not None else None for m, a in zip(mid, atr)]

    # [3] EMA
    e9, e21 = _ema(c, p["ema_fast"]), _ema(c, p["ema_slow"])

    # Heikin Ashi
    hao, hac = [0.0] * n, [0.0] * n
    for i in range(n):
        hac[i] = (o[i] + h[i] + l[i] + c[i]) / 4
        hao[i] = (o[i] + c[i]) / 2 if i == 0 else (hao[i - 1] + hac[i - 1]) / 2
    eps = 1e-9 * max(1.0, abs(c[-1]))

    last_px, last_bar = 0.0, 0
    s_stage = s_bar = b_stage = b_bar = 0
    s_rev = b_rev = False
    out = []
    lb = p["sl_lookback"]
    for i in range(n):
        if vwap[i] is None or mid[i] is None or up[i] is None:
            continue
        hah, hal = max(h[i], hao[i], hac[i]), min(l[i], hao[i], hac[i])
        bull_ha = hac[i] > hao[i] and abs(hal - hao[i]) <= eps
        bear_ha = hac[i] < hao[i] and abs(hah - hao[i]) <= eps
        trend_buy = c[i] > vwap[i] and c[i] < mid[i] and bull_ha
        trend_sell = c[i] < vwap[i] and c[i] > mid[i] and bear_ha
        lo1 = lo[i - 1] if i > 0 and lo[i - 1] is not None else lo[i]
        up1 = up[i - 1] if i > 0 and up[i - 1] is not None else up[i]
        rev_buy = (l[i - 1] < lo1 if i > 0 else False) or l[i] < lo[i]
        rev_buy = rev_buy and bull_ha
        rev_sell = ((h[i - 1] > up1 if i > 0 else False) or h[i] > up[i]) and bear_ha
        raw_buy = (trend_buy or rev_buy) and c[i] > vwap[i]
        raw_sell = (trend_sell or rev_sell) and c[i] < vwap[i]
        fin_buy = raw_buy and (i - last_bar > p["range_limit"] or l[i] < last_px or last_bar == 0)
        fin_sell = raw_sell and (i - last_bar > p["range_limit"] or h[i] > last_px or last_bar == 0)
        if fin_buy:
            last_px, last_bar = l[i], i
        if fin_sell:
            last_px, last_bar = h[i], i

        zl, zh = min(e9[i], e21[i]), max(e9[i], e21[i])
        entry_sell = entry_buy = False
        # ---- SELL ----
        if fin_sell:
            s_stage, s_bar, s_rev = 1, i, rev_sell
        elif s_stage > 0:
            if i - s_bar > p["max_wait"] or c[i] > vwap[i] or fin_buy:
                s_stage = 0
            elif s_stage == 1:
                if c[i] < zl:
                    s_stage = 2
            elif s_stage == 2:
                if c[i] > zh:
                    s_stage = 0
                elif h[i] >= zl and c[i] < zh and c[i] < o[i]:
                    entry_sell, s_stage = True, 0
        # ---- BUY ----
        if fin_buy:
            b_stage, b_bar, b_rev = 1, i, rev_buy
        elif b_stage > 0:
            if i - b_bar > p["max_wait"] or c[i] < vwap[i] or fin_sell:
                b_stage = 0
            elif b_stage == 1:
                if c[i] > zh:
                    b_stage = 2
            elif b_stage == 2:
                if c[i] < zl:
                    b_stage = 0
                elif l[i] <= zh and c[i] > zl and c[i] > o[i]:
                    entry_buy, b_stage = True, 0

        if entry_sell or entry_buy:
            d = -1 if entry_sell else 1
            w = slice(max(0, i - lb + 1), i + 1)
            sl = max(h[w]) + p["sl_buffer"] if entry_sell else min(l[w]) - p["sl_buffer"]
            risk = abs(sl - c[i])
            out.append({"i": i, "t": bars[i][0], "side": "SELL" if entry_sell else "BUY",
                        "kind": ("ĐẢO CHIỀU" if (s_rev if entry_sell else b_rev) else "THUẬN XU HƯỚNG"),
                        "e": c[i], "sl": sl, "tp": [c[i] + d * risk * k for k in (1, 2, 3, 4)]})
    return out
