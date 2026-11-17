"""Chiến lược "PinYin HA Pro - DCA" — chuyển từ Pine Script (pine/PinYin_HA_Pro_DCA.pine, bản đã sửa F1–F4).

Phần này chỉ TÍNH TÍN HIỆU và các mức trên từng nến đã đóng (giống TradingView, biến var giữ qua các nến):
  - Tín hiệu gốc HA Pro (HA + VWAP + TMA + EMA pullback), bộ lọc chống nhiễu tách BUY/SELL [F4]
  - Đảo chiều mới ĐC*: chạm vùng (TMA / VWAP ±σ) → ghi đáy/đỉnh → nến xác nhận + lọc chất lượng
    (phân kỳ RSI / nến từ chối), chống nhiễu cooldown + reset về TMA Mid
  - VWAP ± σ, cờ vwapReady [F1], ATR(14), swing N nến, EMA21 (trail)
Phần quản lý chiến dịch DCA (mở / thêm lệnh / TP1-TP2 / BE / thoát) nằm ở bot (app/api/ckg_auto.py),
vì phải làm với lệnh MT5 thật.

Đầu vào: nến đã đóng [(t_utc, o, h, l, c, volume, day_key), ...] theo thứ tự thời gian.
"""

import math

from app.services.ha_pro import to_ha, _sma, _ema, _atr

DEFAULT = {
    # tín hiệu gốc
    "len": 40, "atr_len": 100, "atr_mult": 2.2, "ema_fast": 9, "ema_slow": 21, "max_wait": 30,
    "range_limit": 10, "sep_filter": True, "candles": "std",
    # VWAP
    "stretch_mult": 2.0, "min_vwap_bars": 10,
    # ĐC*
    "rev_on": True, "rev_both": False, "arm_src": "both", "arm_max": 12, "trig": "either", "back_in": True,
    "quality": "or", "qual_all": True, "use_div": True, "rsi_len": 14, "div_l": 5, "div_r": 2,
    "div_min": 4, "div_max": 60, "use_rej": True, "wick": 1.5, "rev_cool": 20, "rev_reset": True,
    # DCA (dùng ở bot)
    "atr_s": 14, "swing_n": 20,
}


def _rsi(c, n):
    """ta.rsi (RMA của tăng/giảm)."""
    out, ag, al = [None] * len(c), None, None
    ups, dns = [], []
    for i in range(1, len(c)):
        ch = c[i] - c[i - 1]
        u, d = max(ch, 0.0), max(-ch, 0.0)
        if ag is None:
            ups.append(u)
            dns.append(d)
            if len(ups) == n:
                ag, al = sum(ups) / n, sum(dns) / n
            else:
                continue
        else:
            ag = (ag * (n - 1) + u) / n
            al = (al * (n - 1) + d) / n
        out[i] = 100.0 if al == 0 else (0.0 if ag == 0 else 100 - 100 / (1 + ag / al))
    return out


def _pivot(a, left, right, low):
    """ta.pivotlow / pivothigh: giá trị pivot báo ở nến i (pivot nằm ở i - right)."""
    out = [None] * len(a)
    for i in range(left + right, len(a)):
        k = i - right
        v = a[k]
        if v is None:
            continue
        ok = True
        for j in range(k - left, i + 1):
            if j == k:
                continue
            w = a[j]
            if w is None or (low and (w < v if j < k else w <= v)) or (not low and (w > v if j < k else w >= v)):
                ok = False
                break
        if ok:
            out[i] = v
    return out


def compute(bars, p=None):
    """Trả list theo từng nến (None nếu chưa đủ dữ liệu):
    {"t", "c", "go": 1/-1/0, "rev": bool, "ext": đáy/đỉnh ĐC*, "vwap", "sd", "ready", "up", "lo",
     "upx", "lox", "ema21", "atr", "swl", "swh"}"""
    p = {**DEFAULT, **(p or {})}
    on_ha = p["candles"] == "ha"
    if on_ha:
        bars = to_ha(bars)
    n = len(bars)
    out = [None] * n
    if n < p["atr_len"] + p["len"] + 5:
        return out
    o = [b[1] for b in bars]
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    c = [b[4] for b in bars]

    # [0] VWAP + σ theo ngày
    vwap, sd, vbars = [None] * n, [None] * n, [0] * n
    cpv = cpv2 = cv = 0.0
    day, cnt = None, 0
    for i, b in enumerate(bars):
        if b[6] != day:
            day, cpv, cpv2, cv, cnt = b[6], 0.0, 0.0, 0.0, 0
        src = (b[2] + b[3] + b[4]) / 3
        cpv += src * b[5]
        cpv2 += src * src * b[5]
        cv += b[5]
        cnt += 1
        vbars[i] = cnt
        if cv:
            vwap[i] = cpv / cv
            sd[i] = math.sqrt(max(cpv2 / cv - vwap[i] * vwap[i], 0.0))

    # [1] TMA
    half = p["len"]
    mid = _sma(_sma(c, math.floor(half / 2)), math.ceil(half / 2))
    atrL = _atr(h, l, c, p["atr_len"])
    up = [m + a * p["atr_mult"] if m is not None and a is not None else None for m, a in zip(mid, atrL)]
    lo = [m - a * p["atr_mult"] if m is not None and a is not None else None for m, a in zip(mid, atrL)]
    e9, e21 = _ema(c, p["ema_fast"]), _ema(c, p["ema_slow"])
    atrS = _atr(h, l, c, p["atr_s"])
    rsi = _rsi(c, p["rsi_len"])
    plD = _pivot(l, p["div_l"], p["div_r"], True)
    phD = _pivot(h, p["div_l"], p["div_r"], False)

    # Heikin Ashi (ticker.heikinashi)
    hao, hac = [0.0] * n, [0.0] * n
    for i in range(n):
        if on_ha:
            hao[i], hac[i] = o[i], c[i]
            continue
        hac[i] = (o[i] + h[i] + l[i] + c[i]) / 4
        hao[i] = (o[i] + c[i]) / 2 if i == 0 else (hao[i - 1] + hac[i - 1]) / 2
    eps = 1e-9 * max(1.0, abs(c[-1]))

    rl, sw = p["range_limit"], p["swing_n"]
    # bộ lọc chống nhiễu
    last_px = last_bar = 0
    lb_px = lb_bar = ls_px = ls_bar = 0
    # máy trạng thái
    s_stage = s_bar = b_stage = b_bar = 0
    # pivot RSI
    pvL = pvLR = pvLB = pvH = pvHR = pvHB = None
    # ĐC* BUY / SELL
    armB, extB, extBBar, extBRsi, refBLow, refBRsi, refBBar, rejB, strB = False, None, None, None, None, None, None, False, False
    armS, extS, extSBar, extSRsi, refSHigh, refSRsi, refSBar, rejS, strS = False, None, None, None, None, None, None, False, False
    lastRB = lastRS = None
    bResOK = sResOK = True

    def rej(i, bull):
        rng = h[i] - l[i]
        bd = abs(c[i] - o[i])
        wk = (min(o[i], c[i]) - l[i]) if bull else (h[i] - max(o[i], c[i]))
        return rng > 0 and wk >= bd * p["wick"] and wk >= rng * 0.4

    def qual(dv, rj):
        if p["quality"] == "none" or not (p["use_div"] or p["use_rej"]):
            return True
        if p["quality"] == "or":
            return (p["use_div"] and dv) or (p["use_rej"] and rj)
        return (not p["use_div"] or dv) and (not p["use_rej"] or rj)

    for i in range(n):
        # pivot (luôn cập nhật, kể cả khi chưa đủ dữ liệu khác)
        if plD[i] is not None:
            pvL, pvLR, pvLB = plD[i], rsi[i - p["div_r"]], i - p["div_r"]
        if phD[i] is not None:
            pvH, pvHR, pvHB = phD[i], rsi[i - p["div_r"]], i - p["div_r"]
        if vwap[i] is None or mid[i] is None or up[i] is None or atrS[i] is None or rsi[i] is None:
            continue
        vw, sdi = vwap[i], sd[i]
        ready = vbars[i] >= p["min_vwap_bars"] and sdi is not None and sdi > 0
        upx = vw + sdi * p["stretch_mult"] if vbars[i] > 1 and sdi is not None else None
        lox = vw - sdi * p["stretch_mult"] if vbars[i] > 1 and sdi is not None else None

        hah, hal = max(h[i], hao[i], hac[i]), min(l[i], hao[i], hac[i])
        bull_ha = hac[i] > hao[i] and abs(hal - hao[i]) <= eps
        bear_ha = hac[i] < hao[i] and abs(hah - hao[i]) <= eps

        # ---- [4] tín hiệu gốc ----
        trend_buy = c[i] > vw and c[i] < mid[i] and bull_ha
        trend_sell = c[i] < vw and c[i] > mid[i] and bear_ha
        lo1 = lo[i - 1] if lo[i - 1] is not None else lo[i]
        up1 = up[i - 1] if up[i - 1] is not None else up[i]
        rev_buy = (l[i - 1] < lo1 or l[i] < lo[i]) and bull_ha
        rev_sell = (h[i - 1] > up1 or h[i] > up[i]) and bear_ha
        above, below = c[i] > vw, c[i] < vw
        raw_buy = (trend_buy or rev_buy) and above
        raw_sell = (trend_sell or rev_sell) and below
        if p["sep_filter"]:
            fin_buy = raw_buy and (lb_bar == 0 or i - lb_bar > rl or l[i] < lb_px)
            fin_sell = raw_sell and (ls_bar == 0 or i - ls_bar > rl or h[i] > ls_px)
        else:
            fin_buy = raw_buy and (i - last_bar > rl or l[i] < last_px or last_bar == 0)
            fin_sell = raw_sell and (i - last_bar > rl or h[i] > last_px or last_bar == 0)
        if fin_buy:
            last_px, last_bar, lb_px, lb_bar = l[i], i, l[i], i
        if fin_sell:
            last_px, last_bar, ls_px, ls_bar = h[i], i, h[i], i

        # ---- [4N] ĐC* ----
        a03 = atrL[i] * 0.3
        # BUY
        tTB = l[i] < lo[i]
        tVB = ready and lox is not None and l[i] < lox
        touchB = p["rev_on"] and (tTB if p["arm_src"] == "tma" else tVB if p["arm_src"] == "vwap" else (tTB or tVB))
        if armB and i - extBBar > p["arm_max"]:
            armB = False
        if (touchB and not armB) or (armB and l[i] < extB):
            if not armB:
                refBLow, refBRsi, refBBar, strB = pvL, pvLR, pvLB, False
            elif i - extBBar >= p["div_min"]:
                refBLow, refBRsi, refBBar = extB, extBRsi, extBBar
            armB, extB, extBBar, extBRsi, rejB = True, l[i], i, rsi[i], False
        if armB:
            if rej(i, True) and l[i] <= extB + a03:
                rejB = True
            if ready and l[i] <= lox:
                strB = True
        gB = 0 if refBBar is None or extBBar is None else extBBar - refBBar
        divB = armB and refBLow is not None and p["div_min"] <= gB <= p["div_max"] and extB < refBLow and extBRsi > refBRsi
        # SELL
        tTS = h[i] > up[i]
        tVS = ready and upx is not None and h[i] > upx
        touchS = p["rev_on"] and (tTS if p["arm_src"] == "tma" else tVS if p["arm_src"] == "vwap" else (tTS or tVS))
        if armS and i - extSBar > p["arm_max"]:
            armS = False
        if (touchS and not armS) or (armS and h[i] > extS):
            if not armS:
                refSHigh, refSRsi, refSBar, strS = pvH, pvHR, pvHB, False
            elif i - extSBar >= p["div_min"]:
                refSHigh, refSRsi, refSBar = extS, extSRsi, extSBar
            armS, extS, extSBar, extSRsi, rejS = True, h[i], i, rsi[i], False
        if armS:
            if rej(i, False) and h[i] >= extS - a03:
                rejS = True
            if ready and h[i] >= upx:
                strS = True
        gS = 0 if refSBar is None or extSBar is None else extSBar - refSBar
        divS = armS and refSHigh is not None and p["div_min"] <= gS <= p["div_max"] and extS > refSHigh and extSRsi < refSRsi

        # nến xác nhận
        if p["arm_src"] == "tma":
            bandB, bandS = lo[i], up[i]
        elif p["arm_src"] == "vwap":
            bandB, bandS = lox, upx
        else:
            bandB = min(lo[i], lox if lox is not None else lo[i])
            bandS = max(up[i], upx if upx is not None else up[i])
        backB = not p["back_in"] or (bandB is not None and c[i] > bandB)
        backS = not p["back_in"] or (bandS is not None and c[i] < bandS)
        brkB = c[i] > h[i - 1] and c[i] > o[i]
        brkS = c[i] < l[i - 1] and c[i] < o[i]
        trB = bull_ha if p["trig"] == "ha" else brkB if p["trig"] == "brk" else (bull_ha or brkB)
        trS = bear_ha if p["trig"] == "ha" else brkS if p["trig"] == "brk" else (bear_ha or brkS)
        qB, qS = qual(divB, rejB), qual(divS, rejS)
        both = p["rev_both"]
        revTB = armB and backB and trB and ((strB and qB) if below else (both and (qB if p["qual_all"] else True)))
        revTS = armS and backS and trS and ((strS and qS) if above else (both and (qS if p["qual_all"] else True)))
        if revTB and revTS:
            revTS = False
        if c[i] > mid[i]:
            bResOK = True
        if c[i] < mid[i]:
            sResOK = True
        bAll = (lastRB is None or i - lastRB > p["rev_cool"]) and (not p["rev_reset"] or bResOK)
        sAll = (lastRS is None or i - lastRS > p["rev_cool"]) and (not p["rev_reset"] or sResOK)
        if revTB and not bAll:
            revTB, armB = False, False
        if revTS and not sAll:
            revTS, armS = False, False
        extBv, extSv = extB, extS
        if revTB:
            lastRB, bResOK, armB = i, False, False
        if revTS:
            lastRS, sResOK, armS = i, False, False

        # ---- [5] máy trạng thái ----
        zl, zh = min(e9[i], e21[i]), max(e9[i], e21[i])
        eS = eB = False
        if fin_sell:
            s_stage, s_bar = 1, i
        elif s_stage > 0:
            if i - s_bar > p["max_wait"] or c[i] > vw or fin_buy:
                s_stage = 0
            elif s_stage == 1:
                if c[i] < zl:
                    s_stage = 2
            elif s_stage == 2:
                if c[i] > zh:
                    s_stage = 0
                elif h[i] >= zl and c[i] < zh and c[i] < o[i]:
                    eS, s_stage = True, 0
        if fin_buy:
            b_stage, b_bar = 1, i
        elif b_stage > 0:
            if i - b_bar > p["max_wait"] or c[i] < vw or fin_sell:
                b_stage = 0
            elif b_stage == 1:
                if c[i] > zh:
                    b_stage = 2
            elif b_stage == 2:
                if c[i] < zl:
                    b_stage = 0
                elif l[i] <= zh and c[i] > zl and c[i] > o[i]:
                    eB, b_stage = True, 0

        goB, goS = eB or revTB, eS or revTS
        if goB and goS:
            if eB:
                goS = False
            elif eS:
                goB = False
            else:
                goS = False
        isRev = (revTB and not eB) if goB else (revTS and not eS) if goS else False
        w = slice(max(0, i - sw + 1), i + 1)
        out[i] = {"t": bars[i][0], "c": c[i], "go": 1 if goB else -1 if goS else 0, "rev": isRev,
                  "ext": (extBv if goB else extSv) if isRev else None,
                  "vwap": vw, "sd": sdi, "ready": ready, "up": up[i], "lo": lo[i], "upx": upx, "lox": lox,
                  "ema21": e21[i], "atr": atrS[i], "swl": min(l[w]), "swh": max(h[w])}
    return out
