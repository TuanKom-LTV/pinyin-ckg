"""Theo dõi máy chủ PinYin + tự sao lưu dữ liệu.

- beat(tên): các vòng chạy nền (bot, bot khách, copy, lá chắn) báo "còn sống" mỗi vòng.
- Mỗi 15 giây kiểm tra MT5 của máy chủ còn kết nối sàn không. Mất kết nối quá 60 giây → báo Telegram, có lại → báo.
  Vòng bot của chủ đang BẬT mà đứng quá 60 giây → báo Telegram.
- public(): cho /health (UptimeRobot): chỉ trả tình trạng chung, không lộ số tài khoản.
- owner(): cho /quan-ly: độ trễ các vòng, hàng đợi lệnh từng tài khoản, EA online, đệm API, sao lưu.
- Sao lưu: mỗi ngày (và khi bấm nút) nén data/ + .env thành zip vào thư mục sao lưu, giữ 14 bản mới nhất.
  Thư mục: BACKUP_DIR trong .env (nhiều thư mục ngăn bằng ;), không có thì <thư mục cha dự án>/PinYin_backups
  và thêm OneDrive\\PinYinBackup nếu máy có OneDrive. Bản lên OneDrive KHÔNG kèm .env (mật khẩu, khóa) trừ khi
  đặt BACKUP_ENV_CLOUD=1.
"""

import os
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
VN = timezone(timedelta(hours=7))
START = time.time()
_beats = {}
MT5 = {"ok": None, "since": time.time(), "ping": None, "checked": 0, "alerted": False, "err": ""}
BK = {"last": None, "file": None, "dirs": [], "err": "", "size": 0}
_started = False
KEEP = 14


def beat(name):
    _beats[name] = time.time()


def _tg(msg):
    try:
        from app.api.ckg_auto import tg_send
        tg_send("🖥 Máy chủ · " + msg)
    except Exception:
        pass


def _check_mt5():
    import MetaTrader5 as mt5
    from app.services.mt5_service import mt5_service
    ok, ping, err = False, None, ""
    try:
        with mt5_service.session():
            t = mt5.terminal_info()
            ok = bool(t and t.connected)
            ping = round(t.ping_last / 1000, 1) if t and t.ping_last else None
            if not ok:
                err = "MT5 không kết nối được máy chủ sàn"
    except Exception as e:
        err = str(e)
    now = time.time()
    if ok != MT5["ok"]:
        MT5["since"] = now
        if ok and MT5["alerted"]:
            _tg(f"✅ MT5 đã kết nối lại sàn (mất {int(now - MT5.get('down_at', now))} giây).")
            MT5["alerted"] = False
        if not ok:
            MT5["down_at"] = now
    MT5.update(ok=ok, ping=ping, checked=now, err=err)
    if not ok and not MT5["alerted"] and now - MT5["since"] > 60:
        MT5["alerted"] = True
        _tg(f"⚠️ MT5 trên máy chủ MẤT KẾT NỐI sàn hơn 1 phút ({err}). Bot không vào / quản lý lệnh được; "
            "SL và TP1 đã đặt trên sàn vẫn chạy. Kiểm tra mạng và MT5 của máy chủ.")


def _check_loops():
    try:
        from app.api.ckg_auto import _cfg
        on = _cfg().get("on")
    except Exception:
        on = False
    age = time.time() - _beats.get("bot", START)
    fl = MT5.setdefault("bot_alerted", False)
    if on and age > 60 and not fl:
        MT5["bot_alerted"] = True
        _tg(f"⚠️ Vòng bot của chủ đã đứng {int(age)} giây. Khởi động lại PinYin nếu không tự hết.")
    if age < 10 and fl:
        MT5["bot_alerted"] = False
        _tg("✅ Vòng bot chạy lại bình thường.")


# ---------- sao lưu ----------

def _dirs():
    env = [d.strip() for d in os.getenv("BACKUP_DIR", "").split(";") if d.strip()]
    out = [(Path(d), True) for d in env]
    if not env:
        out.append((ROOT.parent / "PinYin_backups", True))
        od = os.getenv("OneDrive") or os.getenv("OneDriveConsumer")
        if od and Path(od).exists():
            out.append((Path(od) / "PinYinBackup", os.getenv("BACKUP_ENV_CLOUD") == "1"))
    return out


def backup_now():
    """Nén data/ + .env vào từng thư mục sao lưu. Trả (ok, thông báo)."""
    stamp = datetime.now(VN).strftime("%Y%m%d_%H%M")
    done, errs, size = [], [], 0
    for d, with_env in _dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"PinYin_data_{stamp}.zip"
            with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
                for p in (ROOT / "data").rglob("*"):
                    if p.is_file() and p.suffix != ".log" and p.stat().st_size < 50 * 1024 * 1024:
                        z.write(p, p.relative_to(ROOT))
                if with_env and (ROOT / ".env").exists():
                    z.write(ROOT / ".env", ".env")
            size = f.stat().st_size
            olds = sorted(d.glob("PinYin_data_*.zip"))
            for o in olds[:-KEEP]:
                o.unlink(missing_ok=True)
            done.append(str(d) + ("" if with_env else " (không kèm .env)"))
        except Exception as e:
            errs.append(f"{d}: {e}")
    BK.update(last=time.time(), file=f"PinYin_data_{stamp}.zip", dirs=done, err="; ".join(errs), size=size)
    try:
        (ROOT / "data" / "backup_last.txt").write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass
    if errs:
        _tg("⚠️ Sao lưu dữ liệu lỗi: " + "; ".join(errs))
    return (not errs and bool(done)), ("Đã sao lưu vào: " + ", ".join(done) if done else "") + (" · Lỗi: " + "; ".join(errs) if errs else "")


def _backup_due():
    try:
        last = int((ROOT / "data" / "backup_last.txt").read_text(encoding="utf-8").strip())
    except Exception:
        last = 0
    if BK["last"] is None and last:
        BK["last"] = last
    return time.time() - last > 24 * 3600


def _loop():
    n = 0
    while True:
        try:
            _check_mt5()
            _check_loops()
            if n % 120 == 0 and _backup_due():          # mỗi 30 phút xem đã quá 1 ngày chưa sao lưu
                backup_now()
        except Exception:
            pass
        n += 1
        time.sleep(15)


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="pinyin-health").start()


# ---------- báo cáo ----------

def _ages():
    now = time.time()
    return {k: round(now - v, 1) for k, v in _beats.items()}


def public():
    mt5_ok = bool(MT5["ok"])
    ok = mt5_ok and time.time() - MT5["checked"] < 60
    return ok, {"ok": ok, "mt5": mt5_ok, "uptime_s": int(time.time() - START), "loops": _ages(),
                "time": datetime.now(VN).strftime("%d/%m/%Y %H:%M:%S")}


def owner():
    from app.api import ea
    from app.services import copytrade, apicache
    now = time.time()
    q = []
    for f in copytrade.targets():
        items = copytrade._q.get(f["id"]) or []
        s = ea._st.get(copytrade.uid(f)) or {}
        q.append({"name": f.get("name") or f["id"], "kind": "bot khách" if f.get("own") else "copy", "on": bool(f.get("on")),
                  "online": bool(s.get("acc")) and ea._online(s), "queue": len(items),
                  "oldest": round(now - items[0]["t"], 1) if items else None})
    online = sum(1 for s in ea._st.values() if s.get("acc") and ea._online(s))
    h = apicache.STATS
    return {"ok": True, "uptime_s": int(now - START), "mt5": {"ok": MT5["ok"], "ping_ms": MT5["ping"], "err": MT5["err"],
            "since_s": int(now - MT5["since"])}, "loops": _ages(), "ea_online": online, "ea_total": len(ea._st), "targets": q,
            "cache": {"hit": h["hit"], "miss": h["miss"], "rate": round(h["hit"] / max(1, h["hit"] + h["miss"]) * 100)},
            "backup": {"last": datetime.fromtimestamp(BK["last"], VN).strftime("%d/%m %H:%M") if BK["last"] else None,
                       "file": BK["file"], "dirs": BK["dirs"] or [str(d) + ("" if e else " (không kèm .env)") for d, e in _dirs()],
                       "err": BK["err"], "size_kb": round(BK["size"] / 1024)}}
