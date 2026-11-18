"""Tài khoản vào lệnh (copy lệnh bot sang MT5 sàn khác qua EA PinYinBridge).

Bot tính tín hiệu và giao dịch trên MT5 của máy chủ (tài khoản tín hiệu, nên dùng Demo). Mỗi hành động của bot
được chép sang các tài khoản đã gắn EA PinYinBridge với mã "PC-...":
  - mở lệnh / thêm lệnh DCA  → ORDER ngay, SL theo KHOẢNG CÁCH giá (giá 2 sàn lệch nhau nên không chép số giá)
  - chốt TP1 / TP2 từng phần → PART|tỉ lệ (theo khối lượng EA đang có)
  - dời SL hòa vốn sau TP1   → BEAVG|cộng thêm (giá vào trung bình của chính tài khoản đó)
  - đóng chiến dịch          → CLOSE
Danh sách lưu ở data/copy_accounts.json (riêng máy chủ, không lên GitHub). Không lưu mật khẩu MT5.
"""

import json
import secrets
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FILE = ROOT / "data" / "copy_accounts.json"
_lock = threading.RLock()
_log = None                      # hàm ghi nhật ký của bot (ckg_auto.log)
LOT_MODES = ("same", "fixed", "mult")
PLAN = {"dist": 0.0, "be": True, "off": 0.0}      # kế hoạch TP1 của Bot 1 (giữ tên cũ)
PLAN_SRC = {"main": PLAN}                           # bot nguồn → kế hoạch TP1 của chiến dịch DCA đang chạy
MASTER = {"login": None, "server": None}         # tài khoản tín hiệu (MT5 của máy chủ), bot cập nhật mỗi vòng


def set_master(login, server):
    MASTER["login"], MASTER["server"] = str(login), server


def _same(acc):
    return bool(acc) and MASTER["login"] is not None and str(acc.get("login")) == MASTER["login"]     # khoảng TP1 hiện tại của chiến dịch (gửi cho EA mỗi giây)


def set_plan(dist, be=None, off=None, src="main"):
    P = PLAN_SRC.setdefault(src, {"dist": 0.0, "be": True, "off": 0.0})
    P["dist"] = round(max(0.0, float(dist or 0)), 2)
    if be is not None:
        P["be"] = bool(be)
    if off is not None:
        P["off"] = float(off)


def plan_line(src="main"):
    P = PLAN_SRC.get(src) or {"dist": 0.0, "be": True, "off": 0.0}
    return "Q|%.2f|%d|%.2f" % (P["dist"], P["be"], P["off"])


def srcs_of(f):
    """Các bot mà tài khoản copy đang theo (mặc định Bot 1)."""
    x = f.get("srcs")
    if isinstance(x, list):                           # có thể rỗng: đang không copy bot nào
        return [str(v) for v in x]
    return [f.get("src") or "main"]


def src_of(f):
    x = srcs_of(f)
    return x[0] if x else "main"


def tag_of(src):
    """Nhãn lệnh trên EA: Bot 1 không nhãn (tương thích EA cũ), bot khác "#<id>"."""
    return "" if src == "main" else src


def dca_src_of(f):
    """Bot DCA (duy nhất) mà tài khoản theo — chỉ bot này dùng TP1 đặt sẵn trên sàn của EA."""
    from app.api import ckg_auto
    return next((s for s in srcs_of(f) if ckg_auto.strategy_of(s) == "dca"), None)


# ---- đích gửi lệnh ngoài danh sách copy: bot riêng của từng khách (app/services/cbot.py) ----
EXTRA = {}              # id → {"id","uid","name","on","own","scope":"B","log":fn,"dir_fn":fn,"ttl"}
PLANS = {}              # uid khách → (khoảng TP1, dời hòa vốn, cộng thêm)


def targets():
    return load() + list(EXTRA.values())


def set_plan_for(u, dist, be=True, off=0.0):
    PLANS[u] = (round(max(0.0, float(dist or 0)), 2), bool(be), float(off or 0))


def plan_for(u):
    """Dòng Q gửi EA mỗi lần đồng bộ: khoảng TP1 để EA đặt TP sẵn trên sàn."""
    if u in PLANS:
        d, be, off = PLANS[u]
        return "Q|%.2f|%d|%.2f" % (d, be, off)
    if u.startswith("copy:"):
        f = next((x for x in load() if uid(x) == u), None)
        src = dca_src_of(f) if f else "main"
        if not src:
            return "Q|0.00|1|0.00"                       # không theo bot DCA nào: không tách TP1
        q = plan_line(src)
        return q + ("|" + tag_of(src) if tag_of(src) else "")
    return None


def enqueue(f, line, what, extra=None):
    _enqueue(f, line, what, extra)


def _sayf(f, msg, kind="info", tele=True):
    if f.get("log"):
        try:
            f["log"](msg, kind)
        except Exception:
            pass
    else:
        try:                                         # ghi đúng tên bot mà tài khoản này copy theo (Telegram + nhật ký bot đó)
            from app.api import ckg_auto
            ctx = ckg_auto._ctx_of(src_of(f))
            if ctx:
                with ckg_auto._use(ctx):
                    _say(msg, kind, tele)
                return
        except Exception:
            pass
        _say(msg, kind, tele)


def note(u, text):
    """Thông báo EA tự gửi lên (vd: sàn đã chốt TP1, EA dời hòa vốn)."""
    f = next((x for x in targets() if uid(x) == u), None)
    if f:
        _sayf(f, f"[{f['name']}] {text}", "close")


def set_logger(fn):
    global _log
    _log = fn


def _say(msg, kind="info", tele=True):
    if _log:
        try:
            _log(msg, kind, tele=tele)
        except Exception:
            pass


def load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save(lst):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(lst, ensure_ascii=False, indent=1), encoding="utf-8")


def uid(f):
    return f.get("uid") or "copy:" + f["id"]


def by_key(key):
    key = (key or "").strip()
    if not key.startswith("PC-"):
        return None
    return next((f for f in load() if secrets.compare_digest(f["key"], key)), None)


def add(name):
    with _lock:
        lst = load()
        if len(lst) >= 10:
            raise ValueError("Tối đa 10 tài khoản vào lệnh.")
        f = {"id": secrets.token_hex(4), "name": (name or "").strip()[:40] or f"Tài khoản {len(lst) + 1}",
             "key": "PC-" + secrets.token_hex(8).upper(), "on": False, "lot_mode": "same", "lot": 1.0, "ttl": 60, "slip": 0}
        lst.append(f)
        save(lst)
        return f


def update(fid, **kw):
    with _lock:
        lst = load()
        f = next((x for x in lst if x["id"] == fid), None)
        if not f:
            raise ValueError("Không tìm thấy tài khoản.")
        if kw.get("delete"):
            lst = [x for x in lst if x["id"] != fid]
        else:
            if kw.get("new_key"):
                f["key"] = "PC-" + secrets.token_hex(8).upper()
            for k in ("name", "on", "lot_mode", "lot", "ttl", "slip", "real", "src", "srcs"):
                if kw.get(k) is not None:
                    f[k] = kw[k]
        save(lst)
        return f


def _lot(f, master_lot):
    m = f.get("lot_mode", "same")
    if m == "fixed":
        v = float(f.get("lot") or 0.01)
    elif m == "mult":
        v = master_lot * float(f.get("lot") or 1)
    else:
        v = master_lot
    return max(0.01, round(v, 2))


ORDER_TTL = 60          # lệnh VÀO chờ EA tối đa 60 giây, quá thì bỏ (không vào lệnh trễ)
KEEP_TTL = 6 * 3600     # lệnh ĐÓNG / chốt / dời SL: giữ tới khi EA online lại (tối đa 6 giờ)
OFF_WARN = 20           # EA mất kết nối quá 20 giây khi đang có lệnh → báo Telegram
_q = {}                 # id tài khoản → hàng đợi [{"line", "what", "t"}]
_flag = {}              # id → trạng thái báo offline / đối chiếu
_worker = None
_senders = {}           # id → luồng gửi lệnh riêng của tài khoản đó (các tài khoản gửi SONG SONG)


def _sender(fid):
    """Gửi lần lượt hàng đợi của 1 tài khoản (giữ đúng thứ tự lệnh), không chờ tài khoản khác."""
    try:
        while True:
            f = next((x for x in targets() if x["id"] == fid), None)
            with _lock:
                q = _q.get(fid) or []
                c = q[0] if q else None
            if not f or not c:
                return
            if _deliver(f, c):
                with _lock:
                    if _q.get(fid) and _q[fid][0] is c:
                        _q[fid].pop(0)
            else:
                time.sleep(1)                       # EA chưa sẵn sàng: thử lại sau
    except Exception as e:
        _say(f"Lỗi gửi lệnh copy: {e}", "err", tele=False)
    finally:
        with _lock:
            _senders.pop(fid, None)


def _kick(fid):
    with _lock:
        if _senders.get(fid) or not _q.get(fid):
            return
        t = threading.Thread(target=_sender, args=(fid,), daemon=True, name="pinyin-send-" + fid)
        _senders[fid] = t
    t.start()


def _enqueue(f, line, what, extra=None):
    with _lock:
        q = _q.setdefault(f["id"], [])
        if line == "CLOSE|0" and any(c["line"] == "CLOSE|0" and c.get("tag", "") == (extra or {}).get("tag", "") for c in q):
            return
        q.append({"line": line, "what": what, "t": time.time(), **(extra or {})})
    _start()
    _kick(f["id"])


def _each(make_line, what, extra=None, src="main", dca=False):
    """Gửi cho mọi tài khoản đang theo bot src (dca=True: chỉ tài khoản lấy bot này làm bot DCA)."""
    for f in load():
        if not f.get("on") or src not in srcs_of(f) or (dca and dca_src_of(f) != src):
            continue
        extra = {**(extra or {}), "tag": tag_of(src)}
        line = make_line(f)
        if line:
            _enqueue(f, line, what, extra)


def _ver_ok(s, need):
    try:
        return tuple(int(x) for x in (s.get("ver") or "0").split(".")) >= need
    except Exception:
        return False


def _master_px(side):
    """Giá hiện tại trên tài khoản tín hiệu (để xét trượt giá trước khi vào lệnh copy trễ)."""
    try:
        import MetaTrader5 as mt5
        from app.services.mt5_service import mt5_service
        with mt5_service.session():
            t = mt5.symbol_info_tick("XAUUSD")
        return (t.ask if side == "BUY" else t.bid) if t and t.bid > 0 else None
    except Exception:
        return None


def _deliver(f, c):
    """Gửi 1 lệnh trong hàng đợi. Trả True nếu xong (bỏ khỏi hàng đợi), False nếu cần thử lại sau."""
    from app.api import ea
    from app.api.ckg_trade import allow_real
    u, name = uid(f), f"[{f['name']}]"
    s = ea._st.get(u)
    order = c["line"].startswith(("ORDER", "DORD"))
    age = time.time() - c["t"]
    if not s or not s.get("acc") or not ea._online(s):
        if age > (ORDER_TTL if order else KEEP_TTL):
            _sayf(f, f"{name} EA offline quá {int(age)} giây, bỏ lệnh: {c['what']}.", "err")
            return True
        return False
    own = f.get("own")                                  # bot của khách: tài khoản của chính khách
    if not own and _same(s["acc"]):
        _sayf(f, f"{name} là CHÍNH tài khoản tín hiệu #{s['acc']['login']} — không copy sang chính nó (sẽ gấp đôi lệnh). Gắn EA vào tài khoản khác.", "err")
        return True
    if not own and not s["acc"]["demo"] and not (f.get("real") or allow_real()):
        _sayf(f, f"{name} là tài khoản THẬT nhưng chưa cho phép lệnh thật (bấm 🔒 Cho phép lệnh THẬT trên thẻ tài khoản này ở mục Tài khoản vào lệnh), không {c['what']}.", "err")
        return True
    ttl = int(f.get("ttl") or ORDER_TTL)
    if order and age > ttl:
        _sayf(f, f"{name} lệnh vào đã chờ {int(age)} giây (quá {ttl} giây), bỏ để không vào lệnh trễ.", "err")
        return True
    line = c["line"]
    if order and float(f.get("slip") or 0) > 0 and c.get("px") and age > 2:
        now = _master_px(c["side"])
        moved = None if now is None else (now - c["px"]) * (1 if c["side"] == "BUY" else -1)
        if moved is not None and moved > float(f["slip"]):
            _sayf(f, f"{name} giá đã chạy ngược {moved:.2f}$ (> {f['slip']}$) sau {int(age)} giây, bỏ lệnh vào.", "err")
            return True
    if f.get("scope") == "B":                           # lệnh bot của khách: chỉ áp lên lệnh bot (EA 1.6+)
        if not _ver_ok(s, (1, 6)):
            _sayf(f, f"{name} EA bản {s.get('ver') or 'cũ'} chưa hỗ trợ bot, cần EA 1.6. Bỏ lệnh: {c['what']}.", "err")
            return True
        line += "|B"
    elif c.get("old_line") and not _ver_ok(s, (1, 5)):      # EA cũ chưa có TP1 đặt sẵn trên sàn
        line = c["old_line"]
    if c.get("tag"):                                    # lệnh của bot khác Bot 1: chỉ áp lên lệnh mang nhãn bot đó
        if not _ver_ok(s, (1, 7)):
            _sayf(f, f"{name} EA bản {s.get('ver') or 'cũ'} chưa copy được nhiều bot, cần EA 1.7. Bỏ lệnh: {c['what']}.", "err")
            return True
        line += "|T:" + c["tag"]
    r = ea._send(u, line, wait=(ttl - age) if order else None)   # lệnh vào: EA không nhận kịp trong hạn thì hủy
    login = s["acc"]["login"]
    if r.get("ok"):
        _sayf(f, f"{name} #{login}: {r.get('msg', 'xong')}" + (f" (trễ {int(age)} giây)" if age > 5 else ""), "open" if order else "close")
        return True
    loi = str(r.get("loi") or "")
    if "đã hủy" in loi or "offline" in loi:            # EA chưa nhận được lệnh: thử lại (lệnh vào thì theo hạn 60 giây)
        return False
    _sayf(f, f"{name} #{login}: không {c['what']}: {loi}", "err")
    return True


def _reconcile(f, s):
    """Tài khoản tín hiệu không còn chiến dịch / khác chiều mà tài khoản vào lệnh vẫn còn lệnh EA → đóng."""
    from app.api import ckg_auto
    pos = s.get("pos") or []
    if f.get("scope") == "B":
        pos = [p for p in pos if p.get("bot")]           # chỉ lệnh bot, không đụng lệnh khách vào tay
    if not pos or _q.get(f["id"]):
        return
    if not f.get("dir_fn"):
        groups = {}
        for p in pos:
            groups.setdefault(p.get("tag") or "", []).append(p)
        follow = {tag_of(x): x for x in srcs_of(f)}
        for tg, ps in groups.items():
            src = follow.get(tg)
            if src is None:
                dirs, why = set(), "tài khoản không còn copy bot này"
            else:
                dirs = ckg_auto.copy_dirs(src)      # chiều các lệnh đang mở của bot nguồn (None = chưa rõ: không đụng)
                if dirs is None:
                    continue
                why = "bot không còn lệnh" if not dirs else "khác chiều lệnh của bot"
            bad = not dirs or any((1 if p["t"] == "BUY" else -1) not in dirs for p in ps)
            fl = _flag.setdefault(f["id"] + "#" + tg, {})
            if bad and time.time() - fl.get("rec", 0) > 30 and time.time() - fl.setdefault("seen", time.time()) > 10:
                fl["rec"] = time.time()
                bn = ckg_auto.bot_name(src or tg or "main")
                _sayf(f, f"[{f['name']}] còn {len(ps)} lệnh copy của [{bn}] nhưng {why} → tự đóng.", "err")
                _enqueue(f, "CLOSE|0", "đóng lệnh lệch", {"tag": tg})
            if not bad:
                fl["seen"] = time.time()
        return
    d = f["dir_fn"]()
    dirs = {d} if d else set()
    bad = not dirs or any((1 if p["t"] == "BUY" else -1) not in dirs for p in pos)
    fl = _flag.setdefault(f["id"], {})
    if bad and time.time() - fl.get("rec", 0) > 30 and time.time() - fl.setdefault("seen", time.time()) > 10:
        fl["rec"] = time.time()
        why = "tài khoản tín hiệu không còn lệnh" if d == 0 else "khác chiều tài khoản tín hiệu"
        if f.get("dir_fn"):
            why = "bot không còn chiến dịch" if d == 0 else "khác chiều chiến dịch của bot"
        _sayf(f, f"[{f['name']}] còn {len(pos)} lệnh bot nhưng {why} → tự đóng.", "err")
        _enqueue(f, "CLOSE|0", "đóng lệnh lệch")
    if not bad:
        fl["seen"] = time.time()


def _master_refresh():
    """Đọc tài khoản tín hiệu (MT5 máy chủ) mỗi 30 giây, kể cả khi bot đang tắt."""
    try:
        import MetaTrader5 as mt5
        from app.services.mt5_service import mt5_service
        with mt5_service.session():
            a = mt5.account_info()
        if a:
            set_master(a.login, a.server)
    except Exception:
        pass


def _loop():
    from app.api import ea
    last_m = 0
    while True:
        try:
            if time.time() - last_m > 30 and load():
                last_m = time.time()
                _master_refresh()
            for f in targets():
                fid, u = f["id"], uid(f)
                s = ea._st.get(u) or {}
                on = bool(s.get("acc")) and ea._online(s)
                fl = _flag.setdefault(fid, {})
                # báo mất kết nối khi đang có lệnh
                if not on and s.get("pos") and f.get("on") and time.time() - s.get("last", 0) > OFF_WARN and not fl.get("off"):
                    fl["off"] = True
                    _sayf(f, f"⚠️ [{f['name']}] EA MẤT KẾT NỐI khi đang có {len(s['pos'])} lệnh. Kiểm tra MT5 / mạng của máy chạy EA (SL trên MT5 vẫn chạy).", "err")
                if on and fl.get("off"):
                    fl["off"] = False
                    _sayf(f, f"[{f['name']}] EA đã kết nối lại.", "info")
                _kick(fid)                                  # hàng đợi còn lệnh mà chưa có luồng gửi: chạy lại
                if on and f.get("on") and (f.get("own") or not _same(s.get("acc"))):
                    _reconcile(f, s)
            # tài khoản đã xóa: bỏ hàng đợi
            ids = {f["id"] for f in targets()}
            with _lock:
                for k in [k for k in _q if k not in ids]:
                    _q.pop(k, None)
        except Exception as e:
            _say(f"Lỗi copy lệnh: {e}", "err", tele=False)
        from app.services.health import beat
        beat("copy")
        time.sleep(1)


def _start():
    global _worker
    with _lock:
        if _worker is None:
            _worker = threading.Thread(target=_loop, daemon=True, name="pinyin-copy")
            _worker.start()


def copy_open(direction, master_lot, sl_dist, pct=0, entry_px=None, src="main"):
    side = "BUY" if direction > 0 else "SELL"
    d = max(0.0, float(sl_dist))
    for f in load():
        if f.get("on") and dca_src_of(f) == src:
            lot = _lot(f, master_lot)
            _enqueue(f, f"DORD|{side}|{lot:.2f}|{d:.2f}|{float(pct or 0):.0f}", f"vào {side}",
                     {"old_line": f"ORDER|{side}|{lot:.2f}|now|{d:.2f}|0|0|M1", "side": side, "px": entry_px, "tag": tag_of(src)})


def copy_part(frac, why, src="main"):
    if not (0 < frac <= 1):
        return
    if why == "TP1":       # EA 1.5: đóng phần "PinYin TP1" (sàn có thể đã tự chốt trước) — EA cũ: chốt theo tỉ lệ
        _each(lambda f: f"TP1PART|{frac:.4f}", "chốt TP1", {"old_line": f"PART|{frac:.4f}"}, src=src, dca=True)
    else:
        _each(lambda f: f"PART|{frac:.4f}", f"chốt {why}", src=src)


def copy_beavg(off, src="main"):
    _each(lambda f: f"BEAVG|{float(off):.2f}", "dời SL hòa vốn", src=src)


def copy_close(why, src="main"):
    _each(lambda f: "CLOSE|0", f"đóng lệnh ({why})", src=src)


def copy_order(direction, master_lot, sl_dist, tp_dist, entry_px=None, src="main"):
    """Lệnh thị trường kèm SL/TP theo khoảng cách $ từ giá khớp (0 = không đặt) — dùng cho Cloud PB."""
    side = "BUY" if direction > 0 else "SELL"
    s, t = max(0.0, float(sl_dist or 0)), max(0.0, float(tp_dist or 0))
    for f in load():
        if f.get("on") and src in srcs_of(f):
            lot = _lot(f, master_lot)
            _enqueue(f, f"ORDER|{side}|{lot:.2f}|now|{s:.2f}|{t:.2f}|0|M1", f"vào {side}", {"side": side, "px": entry_px, "tag": tag_of(src)})


def status():
    from app.api import ea
    out = []
    for f in load():
        s = ea._st.get(uid(f)) or {}
        a = s.get("acc")
        out.append({**f, "src": src_of(f), "srcs": srcs_of(f), "online": bool(a) and ea._online(s), "ver": s.get("ver"), "old": ea._old(s.get("ver")) if s.get("ver") else None,
                    "acc": {k: a.get(k) for k in ("login", "server", "currency", "equity", "balance", "demo", "algo")} if a else None,
                    "same": _same(a), "npos": len(s.get("pos") or []), "queue": len(_q.get(f["id"]) or []), "pnl": round(sum(p["profit"] for p in (s.get("pos") or [])), 2)})
    return out
