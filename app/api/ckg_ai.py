"""PinYin CKG · menu Phân Tích AI (Google Gemini).

- Máy chủ tự gom dữ liệu MT5 đa khung (M1, M5, M15, H1, H4, D1), tính chỉ báo và mốc CKG,
  rồi gửi cho Gemini. Key Gemini chỉ nằm trong .env (GEMINI_API_KEY), không bao giờ gửi xuống trình duyệt.
- Kết quả mỗi mã được giữ lại: người xem (mật khẩu chỉ xem) đọc được bản phân tích gần nhất miễn phí,
  chỉ chủ tài khoản mới yêu cầu phân tích mới (tốn phí API), trừ khi đặt PINYIN_VIEW_AI=1.
- Chỉ phân tích, không gửi lệnh giao dịch.
"""

import http.client
import json
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import MetaTrader5 as mt5
from dotenv import load_dotenv
from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.ckg import CKG_SYMBOLS, VN, server_offset
from app.services.mt5_service import mt5_service


load_dotenv(Path(__file__).resolve().parents[2] / ".env")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Model dự phòng khi model chính quá tải (503), hết hạn mức (429), treo, hoặc bị khóa (404).
# Không dùng gemini-2.5-*: Google đã khóa với key mới. Các bản "lite" nhanh, ít bị quá tải.
FALLBACKS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK",
    "gemini-flash-latest,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3.1-flash-lite").split(",") if m.strip()]
CALL_TIMEOUT = 40     # giây chờ tối đa cho 1 model
DEADLINE = 110        # tổng thời gian thử các model cho 1 lần phân tích
_cool: dict[str, float] = {}   # model -> thời điểm hết "tạm nghỉ"
CACHE_SECONDS = int(os.getenv("PINYIN_AI_CACHE_GIAY", "60"))
HOST = "generativelanguage.googleapis.com"
PATH = "/v1beta/models/{m}:generateContent"


class _V4Conn(http.client.HTTPSConnection):
    """Kết nối HTTPS chỉ qua IPv4: mạng máy này không đi được IPv6, Python mặc định thử IPv6 trước và bị treo."""

    def connect(self):
        addr = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        sock = socket.create_connection(addr, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

router = APIRouter(prefix="/ckg/api/ai", tags=["PinYin CKG · Phân Tích AI"])

_last: dict[str, dict] = {}      # mã -> bản phân tích gần nhất
_busy = Lock()

PIP = {"GBPUSD": 1e-4, "EURUSD": 1e-4, "AUDUSD": 1e-4, "GBPJPY": .01, "EURJPY": .01, "XAUUSD": .1}
DG = {"GBPUSD": 5, "EURUSD": 5, "AUDUSD": 5, "GBPJPY": 3, "EURJPY": 3, "XAUUSD": 2}

TFS = [("M1", mt5.TIMEFRAME_M1, 120), ("M5", mt5.TIMEFRAME_M5, 120), ("M15", mt5.TIMEFRAME_M15, 100),
       ("H1", mt5.TIMEFRAME_H1, 100), ("H4", mt5.TIMEFRAME_H4, 60), ("D1", mt5.TIMEFRAME_D1, 30)]

WPH = ["P1 Quan sát", "P2 Ưu tiên", "P3 Ưu tiên", "P4 Xác nhận", "P5 Ưu tiên", "P6 Xác nhận", "P7 Quan sát"]
WPH_CUM = [5, 9, 13, 17, 21, 25, 30]     # số nến H4 cộng dồn của 7 pha
CYCLES = [(4, 10), (10, 14), (14, 17), (17, 20), (20, 23), (23, 28)]

SYSTEM = """Bạn là cố vấn giao dịch thực chiến của hệ thống PinYin CKG (Chu Kỳ Giá), trả lời bằng tiếng Việt.
Phương pháp CKG của người dùng:
- Hướng chính lấy theo nến W (tuần) và nến D1 gần nhất; chỉ vào lệnh thuận W. Nến D1 "mộng" là nến ≥ 90 pip, bóng ngắn.
- Tuần chia 7 pha WPH7 tính từ T2 04:00 giờ VN (mỗi pha 4-5 nến H4): P1 Quan sát, P2-P3 Ưu tiên, P4 Xác nhận, P5 Ưu tiên, P6 Xác nhận, P7 Quan sát.
- Ngày chia 6 chu kỳ giờ VN: CK1 04-10h, CK2 10-14h, CK3 14-17h, CK4 17-20h, CK5 20-23h, CK6 23-04h.
- Khung giờ vào lệnh: Bước 1 12:00-13:30 (cần ~20-25 pip đi ngược hướng chính từ giá mở D1), Bước 2 18:00-19:59 (≤ 55 pip từ lúc cắt lệnh trước; 60-70 pip trở lên thì bỏ, chờ sau 23:00).
- Tín hiệu kết thúc lực trên M5: loạt 3-7 nến ngược chiều tổng 10-15 pip rồi xuất hiện nến đảo.
- Với XAUUSD: dùng EMA9/EMA21 trên M1; giá vào lệnh cách EMA 0.2 giá (giá dưới EMA thì SELL tại EMA-0.2, giá trên EMA thì BUY tại EMA+0.2); SL theo đáy/đỉnh 4 nến; quá 3 giờ mà hòa vốn/dương ít thì chốt.
- Quản lý vốn: lot nhỏ, gấp lot khi đảo theo cấp số nhân chỉ khi đúng kịch bản.
Yêu cầu:
- Viết văn bản thường, KHÔNG dùng LaTeX hay ký hiệu $...$ (viết ≥, ≤, × trực tiếp).
- Chỉ dựa trên dữ liệu được cung cấp, không bịa số. Nếu dữ liệu cũ (thị trường nghỉ) phải nói rõ.
- Trình bày ngắn gọn, thực chiến, đúng các mục sau (dùng tiêu đề ##):
## Tổng quan
## Xu hướng đa khung
## Vị trí trong chu kỳ CKG
## Kịch bản ưu tiên
## Kịch bản thay thế
## Điểm vào · SL · TP
## Rủi ro & lưu ý
- Ở "Điểm vào · SL · TP" ghi giá cụ thể theo số chữ số thập phân của mã, kèm R:R.
- Kết thúc bằng một dòng: **Kết luận: BUY / SELL / ĐỨNG NGOÀI** kèm độ tin cậy (thấp/trung bình/cao).
- Đây là phân tích tham khảo, không phải lời khuyên đầu tư; không hứa lợi nhuận."""


class AiIn(BaseModel):
    sym: str
    lam_moi: bool = False


# ---------- Chỉ báo ----------

def _ema(a, n):
    k, e, out = 2 / (n + 1), a[0], []
    for x in a:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def _atr(rows, n=14):
    a, out = 0.0, []
    for i, r in enumerate(rows):
        tr = r["h"] - r["l"] if i == 0 else max(r["h"] - r["l"], abs(r["h"] - rows[i - 1]["c"]), abs(r["l"] - rows[i - 1]["c"]))
        a = (a * i + tr) / (i + 1) if i < n else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def _rsi(a, n=14):
    g = l = 0.0
    for i in range(1, len(a)):
        d = a[i] - a[i - 1]
        if i <= n:
            g += max(d, 0) / n; l += max(-d, 0) / n
        else:
            g = (g * (n - 1) + max(d, 0)) / n; l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def _tf_summary(sym, tf, rates, off):
    dg, pip = DG[sym], PIP[sym]
    rows = [{"t": int(r["time"]) - off, "o": float(r["open"]), "h": float(r["high"]),
             "l": float(r["low"]), "c": float(r["close"])} for r in rates]
    cl = [r["c"] for r in rows]
    e9, e21, at = _ema(cl, 9), _ema(cl, 21), _atr(rows)
    last = rows[-1]
    slope9 = e9[-1] - e9[-4] if len(e9) > 4 else 0
    trend = ("TĂNG" if cl[-1] > e9[-1] > e21[-1] else "GIẢM" if cl[-1] < e9[-1] < e21[-1] else "ĐAN XEN")
    cross = None
    for i in range(len(cl) - 1, max(0, len(cl) - 30), -1):
        if (e9[i - 1] - e21[i - 1]) * (e9[i] - e21[i]) < 0:
            cross = {"cach_so_nen": len(cl) - 1 - i, "loai": "BUY (EMA9 cắt lên EMA21)" if e9[i] > e21[i] else "SELL (EMA9 cắt xuống EMA21)"}
            break
    recent = rows[-20:]
    return {
        "khung": tf,
        "gia_dong": round(last["c"], dg),
        "ema9": round(e9[-1], dg), "ema21": round(e21[-1], dg),
        "cach_ema9_pip": round((cl[-1] - e9[-1]) / pip, 1),
        "do_doc_ema9": "lên" if slope9 > 0 else "xuống" if slope9 < 0 else "ngang",
        "atr14": round(at[-1], dg), "atr14_pip": round(at[-1] / pip, 1),
        "rsi14": round(_rsi(cl), 1),
        "xu_huong": trend,
        "cat_ema_gan_nhat": cross,
        "dinh_20_nen": round(max(r["h"] for r in recent), dg),
        "day_20_nen": round(min(r["l"] for r in recent), dg),
        "nen_cuoi": [[datetime.fromtimestamp(r["t"], VN).strftime("%d/%m %H:%M"),
                      round(r["o"], dg), round(r["h"], dg), round(r["l"], dg), round(r["c"], dg)] for r in rows[-8:]],
    }


def _ckg_context(now_vn):
    monday = (now_vn - timedelta(days=now_vn.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    hours = (now_vn - monday).total_seconds() / 3600
    idx = int((hours - 4) // 4)
    phase = None
    if 0 <= idx < 30:
        phase = next(i for i, c in enumerate(WPH_CUM) if idx < c)
    h = now_vn.hour + now_vn.minute / 60
    hh = h if h >= 4 else h + 24
    cyc = next(i for i, (a, b) in enumerate(CYCLES) if a <= hh < b)
    win = "Bước 1 (12:00-13:30)" if 12 <= h < 13.5 else "Bước 2 (18:00-19:59)" if 18 <= h < 20 else "ngoài khung giờ vào lệnh"
    return {
        "gio_vn": now_vn.strftime("%A %d/%m/%Y %H:%M"),
        "pha_tuan_WPH7": (WPH[phase] + f" (nến H4 #{idx + 1}/30)") if phase is not None else "ngoài tuần giao dịch",
        "chu_ky_ngay": f"CK{cyc + 1} ({CYCLES[cyc][0] % 24:02d}:00-{CYCLES[cyc][1] % 24:02d}:00)",
        "khung_gio_vao_lenh": win,
    }


def _collect(sym):
    off, _ = server_offset()
    if not mt5.symbol_select(sym, True):
        raise RuntimeError(f"Broker không có mã {sym}.")
    dg, pip = DG[sym], PIP[sym]
    data = {"ma": sym, "pip": pip, "so_le": dg}
    tick = mt5.symbol_info_tick(sym)
    if tick and tick.time > 0:
        age = int(time.time() - (tick.time - off))
        data["gia"] = {"bid": tick.bid, "ask": tick.ask, "spread_pip": round((tick.ask - tick.bid) / pip, 1),
                       "tick_cach_day_giay": age, "thi_truong": "đang chạy" if age < 300 else "NGHỈ / dữ liệu cũ"}
    tfs = {}
    for name, tf, n in TFS:
        r = mt5.copy_rates_from_pos(sym, tf, 0, n)
        if r is not None and len(r) > 25:
            tfs[name] = _tf_summary(sym, name, r, off)
    data["khung"] = tfs

    d1 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 0, 2)
    w1 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_W1, 0, 2)
    if d1 is not None and len(d1) == 2:
        p, c = d1[0], d1[1]
        rg = (p["high"] - p["low"]) / pip
        uw = (p["high"] - max(p["open"], p["close"])) / pip
        lw = (min(p["open"], p["close"]) - p["low"]) / pip
        data["D1"] = {"nen_truoc": {"o": round(float(p["open"]), dg), "h": round(float(p["high"]), dg), "l": round(float(p["low"]), dg),
                                    "c": round(float(p["close"]), dg), "mau": "xanh" if p["close"] >= p["open"] else "đỏ",
                                    "bien_do_pip": round(rg), "bong_tren_pip": round(uw), "bong_duoi_pip": round(lw)},
                      "gia_mo_hom_nay": round(float(c["open"]), dg)}
    if w1 is not None and len(w1) == 2:
        p = w1[0]
        data["W1_tuan_truoc"] = {"o": round(float(p["open"]), dg), "c": round(float(p["close"]), dg),
                                 "mau": "xanh (BUY)" if p["close"] >= p["open"] else "đỏ (SELL)"}

    m1 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 1, 10)   # 10 nến M1 đã đóng
    if m1 is not None and len(m1):
        up = int(sum(1 for r in m1 if r["close"] > r["open"])); dn = int(sum(1 for r in m1 if r["close"] < r["open"]))
        data["thong_ke_10_nen_M1"] = {"xanh": up, "do": dn, "nghieng": "TĂNG" if up > dn else "GIẢM" if dn > up else "cân bằng",
                                      "tong_bien_do": round(float(sum(r["high"] - r["low"] for r in m1)), dg),
                                      "thay_doi_rong": round(float(m1[-1]["close"] - m1[0]["open"]), dg)}
    data["CKG"] = _ckg_context(datetime.now(VN))
    return data


def _gemini(payload):
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("Chưa đặt GEMINI_API_KEY trong .env.")
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text":
            "Dữ liệu MT5 hiện tại (JSON):\n" + json.dumps(payload, ensure_ascii=False) +
            "\n\nHãy phân tích theo phương pháp CKG và đưa ra kịch bản giao dịch."}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192},
    }
    models = [MODEL] + [m for m in FALLBACKS if m != MODEL]
    now = time.time()
    # Model đang bị "tạm nghỉ" (quá tải / hết hạn mức / treo) xếp xuống cuối, model khỏe lên trước
    order = [m for m in models if _cool.get(m, 0) <= now] + \
            sorted([m for m in models if _cool.get(m, 0) > now], key=lambda m: _cool[m])
    t0, errs = time.time(), []
    for model in order:
        if time.time() - t0 > DEADLINE:
            break
        try:
            text, usage = _call(model, key, body)
            _cool.pop(model, None)
            return text, usage, model
        except _Skip as e:
            _cool[model] = time.time() + e.cool
            errs.append(str(e))
    quota = any("429" in e for e in errs)
    raise RuntimeError(
        ("Các model Gemini đang quá tải hoặc hết hạn mức miễn phí" + (" (key đã dùng hết quota)" if quota else "")
         + ", thử lại sau vài phút.") + " Chi tiết: " + " | ".join(e[:140] for e in errs[-3:]))


class _Skip(RuntimeError):
    """Bỏ qua model này, thử model tiếp theo; tạm nghỉ model trong `cool` giây."""

    def __init__(self, msg, cool):
        super().__init__(msg)
        self.cool = cool


def _call(model, key, body):
    conn = _V4Conn(HOST, timeout=CALL_TIMEOUT)
    try:
        conn.request("POST", PATH.format(m=model), body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        r = conn.getresponse()
        raw = r.read().decode("utf-8", "ignore")
    except (OSError, http.client.HTTPException) as e:
        raise _Skip(f"Gemini ({model}) không phản hồi: {e}", 300)
    finally:
        conn.close()
    if r.status != 200:
        msg = raw
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:
            pass
        err = f"Gemini ({model}) báo lỗi {r.status}: {msg[:200]}"
        if r.status == 429:
            raise _Skip(err, 1800)       # hết hạn mức: nghỉ 30 phút
        if r.status in (500, 502, 503, 504):
            raise _Skip(err, 300)        # quá tải: nghỉ 5 phút
        if r.status == 404:
            raise _Skip(err, 86400)      # model bị khóa/không còn: nghỉ 1 ngày
        raise RuntimeError(err)
    js = json.loads(raw)
    cands = js.get("candidates") or []
    text = "".join(p.get("text", "") for c in cands[:1] for p in c.get("content", {}).get("parts", []))
    if not text:
        raise RuntimeError("Gemini không trả về nội dung (có thể bị bộ lọc an toàn chặn).")
    return text, js.get("usageMetadata", {})


# ---------- Đường dẫn ----------

@router.get("/last")
def last(sym: str = "GBPUSD"):
    r = _last.get(sym.upper().strip())
    return {"ok": True, "co": bool(r), **(r or {})}


@router.post("")
def analyze(body: AiIn, request: Request):
    sym = body.sym.upper().strip()
    if sym not in CKG_SYMBOLS:
        return {"ok": False, "loi": f"Mã {sym} chưa được hỗ trợ."}
    cached = _last.get(sym)
    if cached and not body.lam_moi and time.time() - cached["ts"] < CACHE_SECONDS:
        return {"ok": True, "co": True, "tu_bo_nho": True, **cached}
    if not _busy.acquire(timeout=1):
        return {"ok": False, "loi": "AI đang phân tích một yêu cầu khác, thử lại sau vài giây."}
    try:
        with mt5_service.session():
            payload = _collect(sym)
        text, usage, used = _gemini(payload)
        rec = {"sym": sym, "ts": time.time(), "text": text, "model": used,
               "gio": datetime.now(VN).strftime("%H:%M:%S %d/%m"),
               "tokens": usage.get("totalTokenCount")}
        _last[sym] = rec
        return {"ok": True, "co": True, **rec}
    except Exception as e:
        return {"ok": False, "loi": str(e)}
    finally:
        _busy.release()
