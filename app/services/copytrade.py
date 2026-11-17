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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FILE = ROOT / "data" / "copy_accounts.json"
_lock = threading.RLock()
_log = None                      # hàm ghi nhật ký của bot (ckg_auto.log)
LOT_MODES = ("same", "fixed", "mult")


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
    return "copy:" + f["id"]


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
             "key": "PC-" + secrets.token_hex(8).upper(), "on": False, "lot_mode": "same", "lot": 1.0}
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
            for k in ("name", "on", "lot_mode", "lot"):
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


def _run(f, line, what):
    """Gửi 1 lệnh tới EA của tài khoản f, chờ kết quả (luồng riêng, không chặn bot)."""
    from app.api import ea
    from app.api.ckg_trade import allow_real
    u = uid(f)
    s = ea._st.get(u)
    name = f"[{f['name']}]"
    if not s or not s.get("acc") or not ea._online(s):
        _say(f"{name} EA offline, không {what}.", "err")
        return
    if not s["acc"]["demo"] and not allow_real():
        _say(f"{name} là tài khoản THẬT nhưng đang chặn lệnh thật (nút 🔒 Lệnh THẬT), không {what}.", "err")
        return
    r = ea._send(u, line)
    if r.get("ok"):
        _say(f"{name} #{s['acc']['login']}: {r.get('msg', 'xong')}", "open" if line.startswith("ORDER") else "close")
    else:
        _say(f"{name} #{s['acc']['login']}: không {what}: {r.get('loi')}", "err")


def _each(make_line, what):
    for f in load():
        if not f.get("on"):
            continue
        line = make_line(f)
        if line:
            threading.Thread(target=_run, args=(f, line, what), daemon=True, name="pinyin-copy").start()


def copy_open(direction, master_lot, sl_dist):
    side = "BUY" if direction > 0 else "SELL"
    d = max(0.0, float(sl_dist))
    _each(lambda f: f"ORDER|{side}|{_lot(f, master_lot):.2f}|now|{d:.2f}|0|0|M1", f"vào {side}")


def copy_part(frac, why):
    if not (0 < frac <= 1):
        return
    _each(lambda f: f"PART|{frac:.4f}", f"chốt {why}")


def copy_beavg(off):
    _each(lambda f: f"BEAVG|{float(off):.2f}", "dời SL hòa vốn")


def copy_close(why):
    _each(lambda f: "CLOSE|0", f"đóng lệnh ({why})")


def status():
    from app.api import ea
    out = []
    for f in load():
        s = ea._st.get(uid(f)) or {}
        a = s.get("acc")
        out.append({**f, "online": bool(a) and ea._online(s), "ver": s.get("ver"), "old": ea._old(s.get("ver")) if s.get("ver") else None,
                    "acc": {k: a.get(k) for k in ("login", "server", "currency", "equity", "balance", "demo", "algo")} if a else None,
                    "npos": len(s.get("pos") or []), "pnl": round(sum(p["profit"] for p in (s.get("pos") or [])), 2)})
    return out
