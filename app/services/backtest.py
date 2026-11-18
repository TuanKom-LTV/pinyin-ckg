"""Backtest lịch sử cho bot tự động: chạy lại ĐÚNG luật vào/thoát lệnh của bot trên nến MT5 đã qua.

Chiến lược: PinYin HA Pro DCA (run_dca) và PinYin EMA Cloud Pullback (run_cpb).
Tín hiệu dùng chung code với bot (ha_pro.analyze, ha_pro_dca.compute). Phần quản lý lệnh là bản sao
của ckg_auto (_step / _dca_live / _dca_bar) nhưng chạy trên lệnh giả, không đụng MT5.

Giả định mô phỏng (thận trọng):
  - Nến MT5 là giá Bid; giá Ask = Bid + spread của từng nến. BUY vào ở Ask, đóng ở Bid; SELL ngược lại.
  - Bot vào/thoát chủ động ở giá đóng cửa của nến vừa đóng (bot chạy ngay khi có nến mới).
  - Trong 1 nến: chạm SL được xét TRƯỚC chạm TP (nến quét cả 2 thì tính lỗ). Giá mở cửa nhảy qua SL/TP
    thì khớp ở giá mở cửa. SL hòa vốn / trailing đặt trong nến chỉ có hiệu lực từ nến sau.
  - Phí: commission mỗi lot (khứ hồi), tính theo khối lượng mỗi lần đóng.
"""

import math
from datetime import datetime, timedelta, timezone

from app.services.ha_pro_dca import dca_signals, dca_gap, dca_leg_lot, dca_units, dca_sl

VN = timezone(timedelta(hours=7))
TIERS = [(100, 3, .01), (300, 5, .01), (600, 3, .02), (900, 5, .02), (1000, 3, .03), (1200, 5, .03)]
EPS = 1e-9


def _tier(eq):
    b = None
    for x in TIERS:
        if eq >= x[0]:
            b = x
    return b


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


class Sim:
    """Sổ lệnh giả: số dư, lệnh mở, lịch sử đóng, đường vốn."""

    def __init__(self, bars, spr, cap0, vpu, comm, step=0.01, vmin=0.01):
        self.bars, self.spr, self.vpu, self.comm = bars, spr, vpu, comm
        self.bal = cap0
        self.step, self.vmin = step, vmin
        self.pos, self.trades, self.eq = [], [], []
        self.nid = 0

    def open(self, d, lot, px, sl, tp, k, camp=None, tag=""):
        self.nid += 1
        p = {"id": self.nid, "d": d, "lot": round(lot, 2), "e": px, "sl": sl, "tp": tp, "k": k, "camp": camp,
             "half": False, "tag": tag}
        self.pos.append(p)
        return p

    def close(self, p, vol, px, k, why):
        vol = round(min(vol, p["lot"]), 2)
        pnl = p["d"] * (px - p["e"]) * vol * self.vpu - self.comm * vol
        self.bal += pnl
        self.trades.append({"id": p["id"], "camp": p["camp"], "side": "BUY" if p["d"] > 0 else "SELL", "lot": vol,
                            "e": p["e"], "x": px, "k0": p["k"], "k1": k, "pnl": pnl, "why": why, "tag": p["tag"]})
        p["lot"] = round(p["lot"] - vol, 2)
        if p["lot"] < self.vmin - EPS:
            self.pos.remove(p)

    def exit_px(self, p, k, px_bid):
        """Giá đóng lệnh tại giá Bid cho trước: BUY bán ở Bid, SELL mua ở Ask."""
        return px_bid if p["d"] > 0 else px_bid + self.spr[k]

    def mark(self, k):
        c = self.bars[k][4]
        fl = sum(p["d"] * (self.exit_px(p, k, c) - p["e"]) * p["lot"] * self.vpu for p in self.pos)
        self.eq.append((self.bars[k][0], self.bal + fl))

    def stop_hit(self, p, k, level):
        """Giá chạm mức dừng (SL) trong nến k? Trả giá khớp hoặc None."""
        t, o, h, l = self.bars[k][:4]
        s = self.spr[k]
        if p["d"] > 0:
            return (o if o <= level else level) if l <= level else None
        return (o + s if o + s >= level else level) if h + s >= level else None

    def limit_hit(self, p, k, level):
        """Giá chạm mức chốt lời (TP) trong nến k? Trả giá khớp hoặc None."""
        t, o, h, l = self.bars[k][:4]
        s = self.spr[k]
        if p["d"] > 0:
            return (o if o >= level else level) if h >= level else None
        return (o + s if o + s <= level else level) if l + s <= level else None


# ---------------- PinYin HA Pro DCA (bản sao luật của ckg_auto._dca_*) ----------------

def run_dca(sim, c, lot_fixed, warm):
    bars, n = sim.bars, len(sim.bars)
    d = c["dca"]
    sig = dca_signals(bars, d)
    E = {"dir": 0}

    def reset():
        E.clear()
        E["dir"] = 0

    def avg():
        v = sum(p["lot"] for p in sim.pos)
        return sum(p["e"] * p["lot"] for p in sim.pos) / v if v else None

    def cap_legs():
        if d["risk"] > 0:
            return d["legs"]
        b = _tier(sim.bal)
        return min(d["legs"], b[1] if b else 1)

    def levels(sg):
        a = avg()
        if a is None or E.get("sl") is None or E.get("tp1"):
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

    def close_part(pct, px_bid, k, why):
        want = math.floor(E["vol_tp"] * pct / 100 / sim.step + EPS) * sim.step
        if want < sim.vmin - EPS:
            return
        left = round(want, 2)
        for p in sorted(list(sim.pos), key=lambda x: x["id"]):
            if left < sim.vmin - EPS:
                break
            v = min(p["lot"], left)
            if EPS < p["lot"] - v < sim.vmin - EPS:
                v = p["lot"]
            v = round(math.floor(v / sim.step + EPS) * sim.step, 2)
            if v < sim.vmin - EPS:
                continue
            sim.close(p, v, sim.exit_px(p, k, px_bid), k, why)
            left = round(left - v, 2)

    def close_all(k, why):
        cl = bars[k][4]
        for p in list(sim.pos):
            sim.close(p, p["lot"], sim.exit_px(p, k, cl), k, why)

    def open_leg(k, dr, sl, sg, first):
        cl, s = bars[k][4], sim.spr[k]
        px = cl + s if dr > 0 else cl
        if dr * (px - sl) <= 0:
            return
        if first:
            if d["risk"] > 0:
                loss1 = abs(px - sl) * sim.vpu
                lot = math.floor(sim.bal * d["risk"] / 100 / (dca_units(d, d["legs"]) * loss1) / sim.step + EPS) * sim.step
                if lot < sim.vmin - EPS:
                    return
            elif lot_fixed:
                lot = lot_fixed
            else:
                b = _tier(sim.bal)
                if not b:
                    return
                lot = b[2]
        else:
            lot = round(round(dca_leg_lot(E["lot"], d, E["legs"] + 1) / sim.step) * sim.step, 2)
        nleg = 1 if first else E["legs"] + 1
        sim.open(dr, lot, px, sl, 0.0, k, camp=E.get("cid", k) if not first else k, tag=f"DCA{nleg}")
        if first:
            E.clear()
            E.update({"dir": dr, "rev": bool(sg["rev"]), "sl": sl, "legs": 1, "lot": round(lot, 2), "last_px": sg["c"],
                      "last_k": k, "start_k": k, "tp1": False, "tp2": False, "tp1px": None, "tp2px": None,
                      "vol_tp": 0.0, "cid": k})
        else:
            E.update({"legs": nleg, "last_px": sg["c"], "last_k": k})
        levels(sg)

    tp2p = max(0, min(d["tp2p"], 100 - d["tp1p"]))
    for k in range(warm, n):
        h, l = bars[k][2], bars[k][3]
        # ---- trong nến (bot: _dca_live mỗi 2 giây + SL của sàn) ----
        if E["dir"]:
            dr = E["dir"]
            hit_sl = False
            for p in list(sim.pos):
                if p["k"] < k and p["sl"]:
                    x = sim.stop_hit(p, k, p["sl"])
                    if x is not None:
                        sim.close(p, p["lot"], x, k, "SL hòa vốn" if E.get("tp1") else "SL chung")
                        hit_sl = True
            if not sim.pos:
                reset()
            elif not hit_sl:
                old = [p for p in sim.pos if p["k"] < k]
                if not E["tp1"] and E["tp1px"] and old:
                    probe = {"d": dr}
                    if sim.limit_hit(probe, k, E["tp1px"]) is not None:
                        a0 = avg()
                        E["tp1"], E["vol_tp"] = True, round(sum(p["lot"] for p in sim.pos), 2)
                        close_part(d["tp1p"], E["tp1px"] if dr > 0 else E["tp1px"] - sim.spr[k], k, "TP1")
                        if d["be"] and sim.pos:
                            be = a0 + dr * d["be_off"]
                            for p in sim.pos:
                                if p["sl"] == 0 or dr * (be - p["sl"]) > 0:
                                    p["sl"] = be
                        if tp2p == 0:
                            E["tp2"] = True
                if E["dir"] and E["tp1"] and not E["tp2"] and tp2p > 0 and E["tp2px"] and sim.pos:
                    if sim.limit_hit({"d": dr}, k, E["tp2px"]) is not None:
                        E["tp2"] = True
                        close_part(tp2p, E["tp2px"] if dr > 0 else E["tp2px"] - sim.spr[k], k, "TP2")
                if not sim.pos:
                    reset()
        # ---- nến k đóng (bot: _dca_bar) ----
        sg = sig[k]
        if sg is None:
            sim.mark(k)
            continue
        dr = E["dir"]
        go = sg["go"]
        closed = want_rev = False
        if dr:
            cl, vw, sd = sg["c"], sg["vwap"], sg["sd"]
            opp = go == -dr
            soft = d["soft"] and not E["rev"] and sg["ready"] and (cl < vw - d["soft_sig"] * sd if dr > 0 else cl > vw + d["soft_sig"] * sd)
            tout = d["time"] > 0 and not E["tp1"] and k - E["start_k"] >= d["time"]
            tr_on = E["tp1"] if d["trail_after"] == 1 else E["tp2"]
            trail = d["trail"] and tr_on and (cl < sg["ema21"] if dr > 0 else cl > sg["ema21"])
            why = "tín hiệu ngược" if opp else "SL mềm VWAP" if soft else "hết giờ" if tout else "trail EMA21" if trail else ""
            if why:
                close_all(k, why)
                reset()
                closed = not (opp and d["rev"])
                want_rev = opp and d["rev"]
                dr = 0
        if go and not _in_sess(d, sg["t"]):
            go = 0
        if not dr and not closed and go:
            if not ((go > 0 and d["dir"] == "sell") or (go < 0 and d["dir"] == "buy")):
                cl, vw, sd = sg["c"], sg["vwap"], sg["sd"]
                stretch_ok = sg["rev"] or not sg["ready"] or (cl <= vw + d["stretch"] * sd if go > 0 else cl >= vw - d["stretch"] * sd)
                if stretch_ok:
                    sl, _how = dca_sl(d, sg, go)
                    open_leg(k, go, sl, sg, True)
        elif dr and go == dr and not E["tp1"] and E["legs"] < cap_legs():
            gp = dca_gap(d, sg["atr"])
            gap_ok = (sg["c"] <= E["last_px"] - gp if dr > 0 else sg["c"] >= E["last_px"] + gp) \
                and k - E["last_k"] >= d["min_bars"]
            struct_ok = (sg["c"] > E["sl"] if dr > 0 else sg["c"] < E["sl"]) and (E["rev"] or (sg["c"] > sg["vwap"] if dr > 0 else sg["c"] < sg["vwap"]))
            if gap_ok and struct_ok:
                sl_add, fol = E["sl"], False
                if d.get("sl_follow", True):
                    ns, _how = dca_sl(d, sg, dr)
                    if dr * (sg["c"] - ns) > 0:
                        sl_add, fol = ns, True
                n0 = E["legs"]
                open_leg(k, dr, sl_add, sg, False)
                if fol and E["legs"] > n0:
                    E["sl"] = sl_add
                    for p in sim.pos:                     # SL dời có hiệu lực từ nến sau (vòng sau xét chạm SL)
                        p["sl"] = sl_add
        if E["dir"]:
            levels(sg)
        sim.mark(k)


# ---------------- Thống kê ----------------

# ---------------- PinYin EMA Cloud Pullback (mỗi lệnh độc lập: SL/TP riêng) ----------------

def run_cpb(sim, d, lot, warm):
    """Luật: tín hiệu ở nến đóng → vào lệnh giá đóng nến. Cùng chiều: vào thêm tới d["legs"] lệnh.
    Ngược chiều: d["rev"] → đóng hết lệnh đang mở rồi đảo chiều; không thì bỏ tín hiệu.
    Quản lý: pine = 1 SL + 1 TP; half = chốt half_pct% ở half_r R rồi dời SL hòa vốn. Trailing (R) tùy chọn.
    Tăng lot sau lệnh dính SL lỗ (mart), lệnh có lãi thì về lot gốc."""
    from app.services import cloud_pb
    bars = sim.bars
    sig = cloud_pb.entries(bars, d)
    st = {"n": 0}

    def close(p, vol, px, k, why):
        b0 = sim.bal
        sim.close(p, vol, px, k, why)
        p["pnl"] += sim.bal - b0
        if p not in sim.pos:                                    # lệnh đã đóng hết
            if p["pnl"] > 0:
                st["n"] = 0
            elif why == "SL" and p["pnl"] < 0:
                st["n"] += 1

    for k in range(warm, len(bars)):
        for p in sim.pos:                                       # SL dời ở nến trước có hiệu lực từ nến này
            if "sl_new" in p:
                p["sl"], p["slw"] = p.pop("sl_new"), p.pop("slw_new")
        for p in list(sim.pos):
            if p["k"] >= k:
                continue
            x = sim.stop_hit(p, k, p["sl"])
            if x is not None:
                close(p, p["lot"], x, k, p["slw"])
                continue
            if d["mg"] == "half" and not p["half"]:
                x = sim.limit_hit(p, k, p["e"] + p["d"] * d["half_r"] * p["R"])
                if x is not None:
                    p["half"] = True
                    vol = math.floor(p["lot0"] * d["half_pct"] / 100 / sim.step + EPS) * sim.step
                    if sim.vmin - EPS <= vol < p["lot"] - EPS:
                        close(p, vol, x, k, f"chốt {d['half_pct']}%")
                    be = p["e"] + p["d"] * d["be_off"]
                    if p["d"] * (be - p["sl"]) > 0:
                        p["sl_new"], p["slw_new"] = be, "hòa vốn"
            x = sim.limit_hit(p, k, p["tp"])
            if x is not None:
                close(p, p["lot"], x, k, "TP")
        c = bars[k][4]
        if d["be_on"]:                                          # lá chắn hòa vốn (theo R)
            for p in sim.pos:
                if not p.get("be") and p["d"] * (sim.exit_px(p, k, c) - p["e"]) >= d["be_at"] * p["R"]:
                    p["be"] = True
                    be = p["e"] + p["d"] * d["be_off"]
                    if p["d"] * (be - p.get("sl_new", p["sl"])) > 0:
                        p["sl_new"], p["slw_new"] = be, "hòa vốn"
        if d["tr_on"]:
            for p in sim.pos:
                px = sim.exit_px(p, k, c)
                if p["d"] * (px - p["e"]) >= d["tr_at"] * p["R"]:
                    ns = px - p["d"] * d["tr_dist"] * p["R"]
                    cur = p.get("sl_new", p["sl"])
                    if p["d"] * (ns - cur) > 0:
                        p["sl_new"], p["slw_new"] = ns, "trailing"
        for s in sig.get(k, ()):
            if not _in_sess(d, bars[k][0]) or (d["max_spr"] and sim.spr[k] > d["max_spr"]):
                continue
            if (d["min_sl"] and s["R"] < d["min_sl"]) or (d["max_sl"] and s["R"] > d["max_sl"]):
                continue
            go = s["go"]
            if s.get("hold") and any(p["d"] == go for p in sim.pos):
                continue                                        # hold: đã có lệnh theo màu mây rồi
            opp = [p for p in sim.pos if p["d"] != go]
            if opp:
                if not d["rev"]:
                    continue
                for p in opp:
                    close(p, p["lot"], sim.exit_px(p, k, c), k, "đảo chiều")
            if sum(1 for p in sim.pos if p["d"] == go) >= d["legs"]:
                continue
            fill = c + sim.spr[k] if go > 0 else c
            R = go * (fill - s["sl"])
            if R <= 0:
                continue
            lt = cloud_pb.mart_lot(lot, d, st["n"], sim.step)
            p = sim.open(go, lt, fill, s["sl"], fill + go * d["rr"] * R, k, tag=f"x{lt}" if lt != lot else "")
            p.update(R=R, lot0=lt, pnl=0.0, slw="SL")
        sim.mark(k)


def run_zr(sim, d, lot, warm, cap0):
    """Không SL, nhân đôi đảo chiều (xem cloud_pb mg = zr). Trả thống kê riêng (bậc gấp thếp, lot lớn nhất, cháy)."""
    from app.services import cloud_pb
    bars, vpu = sim.bars, sim.vpu
    res = cloud_pb.compute(bars, d)
    E2 = res["e2"]
    ent = cloud_pb.entries(bars, d, res)                # tín hiệu theo "Kiểu vào lệnh" (hồi chạm mây / mây đổi màu / cứ xanh-đỏ)
    camp, last, trig_used, dead = 0, None, False, False
    info = {"steps": {}, "max_lot": 0.0, "min_eq": cap0, "dead_at": None}
    floor_eq = cap0 * d["so_pct"] / 100

    def lvl(target, k):
        """Giá Bid mà tổng lãi/lỗ các lệnh = target. Trả (giá, N) với N = khối lượng ròng (BUY − SELL)."""
        N = sum(p["d"] * p["lot"] for p in sim.pos)
        C = sum(-p["d"] * p["lot"] * p["e"] - (p["lot"] * sim.spr[k] if p["d"] < 0 else 0) for p in sim.pos)
        return ((target / vpu - C) / N if abs(N) > EPS else None), N

    def pnl(bid, k):
        return sum(p["d"] * (sim.exit_px(p, k, bid) - p["e"]) * p["lot"] * vpu for p in sim.pos)

    def close_all(bid, k, why):
        n = len({p["id"] for p in sim.pos})
        for p in list(sim.pos):
            sim.close(p, p["lot"], sim.exit_px(p, k, bid), k, why)
        info["steps"][n] = info["steps"].get(n, 0) + 1

    for k in range(warm, len(bars)):
        t, o, h, l, c = bars[k][:5]
        if sim.pos and min(p["k"] for p in sim.pos) < k:
            N = sum(p["d"] * p["lot"] for p in sim.pos)
            adv = l if N > 0 else h                         # giá bất lợi nhất trong nến (xét trước, thận trọng)
            eq_adv = sim.bal + pnl(adv, k)
            info["min_eq"] = min(info["min_eq"], eq_adv)
            if eq_adv <= floor_eq:
                # cháy: đóng ở giá làm vốn chạm mức cháy (hoặc giá mở cửa nếu nhảy qua)
                P, N = lvl(floor_eq - sim.bal, k)
                px = adv if P is None else (min(o, max(P, l)) if N > 0 else max(o, min(P, h)))
                close_all(px, k, "CHÁY TÀI KHOẢN")
                dead, info["dead_at"] = True, datetime.fromtimestamp(t, VN).strftime("%d/%m/%Y %H:%M")
                sim.mark(k)
                break
            target = d["zr_tp"] * sim.pos[0]["lot"] * vpu if len(sim.pos) == 1 else d["zr_be"]
            P, N = lvl(target, k)
            if P is not None:
                if N > 0 and h >= P:
                    close_all(max(P, o), k, "chốt lời" if len(sim.pos) == 1 else "hòa vốn cả cụm")
                elif N < 0 and l <= P:
                    close_all(min(P, o), k, "chốt lời" if len(sim.pos) == 1 else "hòa vốn cả cụm")
        sg = ent[k][0]["go"] if k in ent else 0
        if not sim.pos:
            last, trig_used = None, False
            if sg and _in_sess(d, t):
                camp += 1
                g = sg
                p = sim.open(g, lot, c + sim.spr[k] if g > 0 else c, 0, 0, k, camp=camp)
                last = p
        elif last is not None and len(sim.pos) < d["zr_max"]:
            g = -last["d"]
            hit = sg == g
            if not hit and d["zr_trig"] == "ema" and not trig_used and E2[k] is not None:
                hit = (c < E2[k]) if last["d"] > 0 else (c > E2[k])
            if hit:
                lt = max(sim.vmin, round(round(last["lot"] * d["zr_mult"] / sim.step) * sim.step, 2))
                p = sim.open(g, lt, c + sim.spr[k] if g > 0 else c, 0, 0, k, camp=camp)
                last, trig_used = p, False
                info["max_lot"] = max(info["max_lot"], lt)
            elif d["zr_trig"] == "ema" and E2[k] is not None and ((c < E2[k]) if last["d"] > 0 else (c > E2[k])):
                trig_used = True
        info["max_lot"] = max(info["max_lot"], max((p["lot"] for p in sim.pos), default=0))
        sim.mark(k)
    info["dead"] = dead
    return info


def stats(sim, cap0, by_camp):
    tr = sim.trades
    eq = [v for _, v in sim.eq] or [cap0]
    peak, mdd, mddp = cap0, 0.0, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak - v > mdd:
            mdd, mddp = peak - v, (peak - v) / peak * 100 if peak > 0 else 0
    groups = {}
    for t in tr:
        key = t["camp"] if by_camp else t["id"]
        g = groups.setdefault(key, {"pnl": 0.0, "k0": t["k0"], "k1": t["k1"], "side": t["side"], "legs": set(), "why": t["why"], "lot": 0.0})
        g["pnl"] += t["pnl"]
        g["k1"] = max(g["k1"], t["k1"])
        g["legs"].add(t["id"])
        g["why"] = t["why"]
        g["lot"] += t["lot"]
    gs = sorted(groups.values(), key=lambda g: g["k0"])
    wins = [g["pnl"] for g in gs if g["pnl"] > 0]
    loss = [g["pnl"] for g in gs if g["pnl"] <= 0]
    streak = cur = 0
    for g in gs:
        cur = cur + 1 if g["pnl"] <= 0 else 0
        streak = max(streak, cur)
    month = {}
    for g in gs:
        m = datetime.fromtimestamp(sim.bars[g["k1"]][0], VN).strftime("%Y-%m")
        x = month.setdefault(m, {"m": m, "n": 0, "pnl": 0.0, "w": 0})
        x["n"] += 1
        x["pnl"] += g["pnl"]
        x["w"] += g["pnl"] > 0
    why = {}
    for t in tr:
        w = why.setdefault(t["why"], {"why": t["why"], "n": 0, "pnl": 0.0})
        w["n"] += 1
        w["pnl"] += t["pnl"]
    net = sim.bal - cap0
    gw, gl = sum(wins), -sum(loss)
    bars = sim.bars
    tm = lambda k: datetime.fromtimestamp(bars[k][0], VN).strftime("%d/%m %H:%M")
    step = max(1, len(sim.eq) // 800)
    return {
        "net": round(net, 2), "net_pct": round(net / cap0 * 100, 2) if cap0 else 0, "end": round(sim.bal, 2),
        "n": len(gs), "n_orders": len({t["id"] for t in tr}), "win": len(wins), "winrate": round(len(wins) / len(gs) * 100, 1) if gs else 0,
        "pf": round(gw / gl, 2) if gl >= 0.01 else (None if not gw else 999), "avg_win": round(gw / len(wins), 2) if wins else 0,
        "avg_loss": round(-gl / len(loss), 2) if loss else 0, "best": round(max(wins), 2) if wins else 0,
        "worst": round(min(loss), 2) if loss else 0, "mdd": round(mdd, 2), "mdd_pct": round(mddp, 1), "streak": streak,
        "open_left": len(sim.pos),
        "months": [{**v, "pnl": round(v["pnl"], 2)} for v in sorted(month.values(), key=lambda v: v["m"])],
        "why": sorted(({**v, "pnl": round(v["pnl"], 2)} for v in why.values()), key=lambda v: -v["n"]),
        "list": [{"t0": tm(g["k0"]), "t1": tm(g["k1"]), "side": g["side"], "legs": len(g["legs"]), "lot": round(g["lot"], 2),
                  "pnl": round(g["pnl"], 2), "why": g["why"]} for g in gs[-300:]][::-1],
        "curve": [[t, round(v, 2)] for t, v in sim.eq[::step]],
    }


def run(bars, spr, cfg, lot, cap0, vpu, comm=0.0, warm=300, step=0.01, vmin=0.01):
    """Chạy 1 lần backtest. bars: nến đã đóng (Bid), spr: spread (đơn vị giá) từng nến."""
    sim = Sim(bars, spr, cap0, vpu, comm, step, vmin)
    warm = min(warm, max(0, len(bars) - 1))
    cpb = cfg.get("strategy") == "cpb"
    zr = None
    if cpb and cfg["cpb"].get("mg") == "zr":
        zr = run_zr(sim, cfg["cpb"], lot or 0.01, warm, cap0)
    elif cpb:
        run_cpb(sim, cfg["cpb"], lot or 0.01, warm)
    else:
        run_dca(sim, cfg, lot, warm)
    # lệnh còn mở cuối dữ liệu: tính theo giá đóng cửa cuối (ghi lý do "hết dữ liệu")
    k = len(bars) - 1
    left = len(sim.pos)
    for p in list(sim.pos):
        sim.close(p, p["lot"], sim.exit_px(p, k, bars[k][4]), k, "còn mở (hết dữ liệu)")
    out = stats(sim, cap0, by_camp=not cpb or zr is not None)
    if zr is not None:
        out["zr"] = {**zr, "min_eq": round(zr["min_eq"], 2), "max_lot": round(zr["max_lot"], 2),
                     "steps": [{"n": n, "count": v} for n, v in sorted(zr["steps"].items())]}
    out["open_left"] = left
    return out
