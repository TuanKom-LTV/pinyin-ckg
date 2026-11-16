"""Dịch tên cặp tiền giữa PinYin (tên chuẩn: XAUUSD, GBPUSD...) và tên thật trên sàn (XAUUSD.sc, XAUUSDm, GOLD, GBPUSD.raw...).

Cách dùng: install() một lần (mt5_service gọi sẵn). Sau đó mọi lệnh mt5.* trong PinYin vẫn dùng tên chuẩn:
  - Gọi vào MT5 (symbol_info, copy_rates_*, positions_get(symbol=), order_send({"symbol"}) ...): tên chuẩn → tên sàn.
  - Kết quả trả về (lệnh mở, lệnh chờ, deal, symbol_info): tên sàn → tên chuẩn.
Tên sàn được tự dò theo tài khoản đang đăng nhập. Ghi đè bằng .env, ví dụ:
  SYMBOL_MAP=XAUUSD=GOLD.sc,GBPUSD=GBPUSD.sc      (từng mã)
  SYMBOL_SUFFIX=.sc                                (mọi mã cùng hậu tố)
"""

import os
import re
import threading
import time
from collections import namedtuple

import MetaTrader5 as mt5

CANON = ("XAUUSD", "GBPUSD", "EURUSD", "AUDUSD", "GBPJPY", "EURJPY", "USDJPY",
         "NZDUSD", "USDCAD", "USDCHF", "XAGUSD")
ALIASES = {"XAUUSD": ("GOLD",), "XAGUSD": ("SILVER",)}
_orig = {}
_lock = threading.RLock()
_map = {"key": None, "t": 0, "fwd": {}, "rev": {}}


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _env_map():
    out = {}
    for part in os.getenv("SYMBOL_MAP", "").split(","):
        if "=" in part:
            a, b = part.split("=", 1)
            if a.strip() and b.strip():
                out[a.strip().upper()] = b.strip()
    return out


def _affix_ok(a, side):
    """Hậu tố / tiền tố hợp lệ của sàn: '.sc', '_i', 'm', 'c', '.raw', '#', 'm.'...
    Chặn chữ HOA dính liền (XAUUSDT là cặp tiền điện tử, không phải vàng)."""
    if not a:
        return True
    if len(a) > 5:
        return False
    if side == "suf":
        return not a[0].isalnum() or a.islower() or a.isdigit()
    return not a[-1].isalnum() or a.islower()


def _parts(canon, n):
    """(base, hậu tố/tiền tố) nếu tên sàn n là canon kèm hậu tố/tiền tố hợp lệ, không thì None."""
    up = n.upper()
    for base in (canon,) + ALIASES.get(canon, ()):
        if up.startswith(base) and _affix_ok(n[len(base):], "suf"):
            return base, n[len(base):]
        if up.endswith(base) and len(up) > len(base) and _affix_ok(n[:-len(base)], "pre"):
            return base, "pre:" + n[:-len(base)]
    return None


def _pick(canon, names, pref=None):
    """Chọn tên sàn hợp nhất cho 1 mã chuẩn. pref: hậu tố phổ biến của sàn (dò từ các cặp khác)."""
    if canon in names and not pref:
        return canon
    cands = []
    for n in names:
        p = _parts(canon, n)
        if p:
            base, aff = p
            cands.append((0 if pref is not None and aff == pref else 1, 0 if base == canon else 1, len(aff), n))
    if not cands:
        return None
    cands.sort()
    best = [c for c in cands if c[:3] == cands[0][:3]]
    if len(best) > 1 and "symbol_info" in _orig:   # ngang nhau: ưu tiên mã đang hiện trong Market Watch / cho giao dịch
        for c in best:
            i = _orig["symbol_info"](c[3])
            if i and i.visible and i.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED:
                return c[3]
    return cands[0][3]


def _common_affix(names):
    """Hậu tố mà đa số cặp FX của sàn đang dùng (vd 'm' ở Exness, '.sc')."""
    cnt = {}
    for c in ("GBPUSD", "EURUSD", "AUDUSD", "USDJPY", "GBPJPY", "EURJPY", "NZDUSD", "USDCAD"):
        hits = {p[1] for n in names if (p := _parts(c, n)) and p[0] == c}
        if len(hits) == 1:                     # cặp này chỉ có đúng 1 kiểu tên trên sàn
            a = hits.pop()
            cnt[a] = cnt.get(a, 0) + 1
    return max(cnt, key=cnt.get) if cnt else None


def _build():
    if _map["fwd"] and time.time() - _map["t"] < 60:     # kiểm tra lại tài khoản tối đa 1 lần/phút
        return
    a = mt5.account_info()
    key = f"{a.login}@{a.server}" if a else None
    with _lock:
        if key and _map["key"] == key and time.time() - _map.get("full", 0) < 3600:
            _map["t"] = time.time()
            return
        syms = _orig["symbols_get"]() or ()
        names = [s.name for s in syms]
        if not names:
            return
        env, suf = _env_map(), os.getenv("SYMBOL_SUFFIX", "").strip()
        pref = _common_affix(names)
        fwd = {}
        for c in CANON:
            if c in env:
                fwd[c] = env[c]
            elif suf and c + suf in names:
                fwd[c] = c + suf
            else:
                p = _pick(c, names, pref)
                if p:
                    fwd[c] = p
        _map.update(key=key, t=time.time(), full=time.time(), fwd=fwd, rev={v: k for k, v in fwd.items()})


def to_broker(sym):
    if not isinstance(sym, str):
        return sym
    s = sym.upper()
    if s not in CANON:
        return sym
    try:
        _build()
    except Exception:
        pass
    return _map["fwd"].get(s, sym)


def to_canon(name):
    return _map["rev"].get(name, name)


def mapping():
    try:
        _build()
    except Exception:
        pass
    return dict(_map["fwd"])


# ---------- Bọc các hàm MT5 ----------

_types = {}


def _fix(obj, field="symbol"):
    """Đổi tên sàn → tên chuẩn trong 1 kết quả MT5 (giữ nguyên mọi trường khác, vẫn đọc kiểu obj.ticket)."""
    if obj is None or not hasattr(obj, "_asdict"):
        return obj
    v = getattr(obj, field, None)
    c = to_canon(v) if isinstance(v, str) else v
    if c == v:
        return obj
    d = obj._asdict()
    d[field] = c
    key = (type(obj).__name__, tuple(d))
    if key not in _types:
        _types[key] = namedtuple(type(obj).__name__, d.keys())
    return _types[key](**d)


def _fix_many(res, field="symbol"):
    if res is None:
        return None
    return tuple(_fix(x, field) for x in res)


def _wrap_first_arg(name, out=None):
    f = _orig[name]

    def w(*a, **k):
        if a:
            a = (to_broker(a[0]),) + a[1:]
        elif "symbol" in k:
            k["symbol"] = to_broker(k["symbol"])
        r = f(*a, **k)
        return out(r) if out else r
    return w


def _wrap_symbol_kw(name):
    f = _orig[name]

    def w(*a, **k):
        if "symbol" in k:
            k["symbol"] = to_broker(k["symbol"])
        return _fix_many(f(*a, **k))
    return w


def _wrap_request(name):
    f = _orig[name]

    def w(req=None, **k):
        req = req if req is not None else k.pop("request", None)
        if isinstance(req, dict) and "symbol" in req:
            req = {**req, "symbol": to_broker(req["symbol"])}
        return f(req)          # MetaTrader5 chỉ nhận request truyền theo vị trí (request=... bị 10013)
    return w


def install():
    if _orig:
        return
    for n in ("symbol_select", "symbol_info", "symbol_info_tick", "copy_rates_from_pos", "copy_rates_range",
              "copy_rates_from", "copy_ticks_from", "copy_ticks_range", "positions_get", "orders_get",
              "history_deals_get", "history_orders_get", "order_send", "order_check", "symbols_get",
              "order_calc_margin", "order_calc_profit"):
        if hasattr(mt5, n):
            _orig[n] = getattr(mt5, n)
    for n in ("symbol_select", "symbol_info_tick", "copy_rates_from_pos", "copy_rates_range",
              "copy_rates_from", "copy_ticks_from", "copy_ticks_range"):
        if n in _orig:
            setattr(mt5, n, _wrap_first_arg(n))
    mt5.symbol_info = _wrap_first_arg("symbol_info", out=lambda r: _fix(r, "name"))
    for n in ("positions_get", "orders_get", "history_deals_get", "history_orders_get"):
        setattr(mt5, n, _wrap_symbol_kw(n))
    for n in ("order_send", "order_check"):
        setattr(mt5, n, _wrap_request(n))
    for n in ("order_calc_margin", "order_calc_profit"):
        if n in _orig:
            f = _orig[n]
            setattr(mt5, n, lambda action, symbol, *a, _f=f, **k: _f(action, to_broker(symbol), *a, **k))
