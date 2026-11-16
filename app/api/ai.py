"""Phân tích AI trên dữ liệu MT5 (chỉ gợi ý, không tự đặt lệnh).

Hỗ trợ 2 nguồn AI, chọn bằng file .env ở thư mục dự án:
  PINYIN_AI_PROVIDER=gemini | claude  (bỏ trống: tự chọn theo key đang có)

  Gemini (Google, có gói miễn phí):
    GEMINI_API_KEY=...
    PINYIN_GEMINI_MODEL=gemini-3.5-flash         tùy chọn
  Claude (Anthropic, trả phí):
    ANTHROPIC_API_KEY=sk-ant-...
    PINYIN_AI_MODEL=claude-opus-5-5              tùy chọn

  PINYIN_AI_CACHE_GIAY=60   dùng lại kết quả cùng mã/khung trong N giây
"""

import json
import os
import threading
import time
import http.client
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter
from pydantic import BaseModel

from app.services.mt5_service import mt5_service


load_dotenv(Path(__file__).resolve().parents[2] / ".env")

MODEL = os.getenv("PINYIN_AI_MODEL", "claude-opus-5-5")
GEMINI_MODEL = os.getenv("PINYIN_GEMINI_MODEL", "gemini-3.5-flash")
# Dùng khi mô hình chính quá tải/hết hạn mức miễn phí.
GEMINI_BACKUP_MODEL = os.getenv("PINYIN_GEMINI_BACKUP_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CACHE_SECONDS = int(os.getenv("PINYIN_AI_CACHE_GIAY", "60"))
SYMBOLS = {"XAUUSD", "EURUSD", "GBPUSD", "GBPJPY"}
TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4"}
HIGHER_TF = {"M1": "M15", "M5": "H1", "M15": "H1", "M30": "H4", "H1": "H4", "H4": "H4"}

router = APIRouter(prefix="/api/v1/ai", tags=["Mô hình AI"])

_client = None
_client_lock = threading.Lock()
_cache = {}
_last_error = None
_gemini_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gemini")


SYSTEM_PROMPT = """Bạn là chuyên viên phân tích kỹ thuật forex/vàng cho nền tảng PinYin.
Bạn nhận dữ liệu nến và chỉ báo đọc trực tiếp từ MetaTrader 5, rồi đưa ra nhận định ngắn gọn bằng tiếng Việt.

Quy tắc:
- Chỉ dựa trên dữ liệu được cung cấp; không bịa tin tức hay số liệu.
- tin_hieu là MUA, BAN hoặc CHO. Chọn CHO khi tín hiệu mâu thuẫn, biến động bất thường,
  hoặc dữ liệu đã cũ (thị trường đóng cửa) - khi đó không đề xuất giá vào lệnh.
- Khi tin_hieu là MUA/BAN: cat_lo và chot_loi phải là mức giá cụ thể, hợp lý theo ATR
  (MUA: cat_lo < giá hiện tại < chot_loi; BAN: chot_loi < giá hiện tại < cat_lo).
  Khi tin_hieu là CHO: đặt cat_lo và chot_loi là 0.
- do_tin_cay từ 0 đến 100, phản ánh trung thực mức độ đồng thuận của các chỉ báo.
- ly_do: 2-4 câu, nêu các chỉ báo chính dẫn tới kết luận.
- rui_ro: 1-2 câu về điều có thể làm nhận định sai.
Đây là gợi ý tham khảo; người dùng tự quyết định có đặt lệnh hay không."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "xu_huong": {"type": "string", "enum": ["TANG", "GIAM", "DI_NGANG"]},
        "tin_hieu": {"type": "string", "enum": ["MUA", "BAN", "CHO"]},
        "do_tin_cay": {"type": "integer"},
        "cat_lo": {"type": "number"},
        "chot_loi": {"type": "number"},
        "ly_do": {"type": "string"},
        "rui_ro": {"type": "string"},
    },
    "required": ["xu_huong", "tin_hieu", "do_tin_cay", "cat_lo", "chot_loi", "ly_do", "rui_ro"],
    "additionalProperties": False,
}


class AnalyzeIn(BaseModel):
    symbol: str = "XAUUSD"
    timeframe: str = "M15"
    lam_moi: bool = False   # bỏ qua cache


# ---------- Chỉ báo ----------

def _ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def _atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _vwap(candles):
    pv = vol = 0.0
    for c in candles:
        v = c["tick_volume"] or 1
        pv += (c["high"] + c["low"] + c["close"]) / 3 * v
        vol += v
    return pv / vol if vol else None


def _indicators(candles, digits):
    closes = [c["close"] for c in candles]
    r = lambda x: None if x is None else round(x, digits)
    return {
        "gia_dong_cua": r(closes[-1]),
        "ema9": r(_ema(closes, 9)[-1]),
        "ema21": r(_ema(closes, 21)[-1]),
        "ema50": r(_ema(closes, 50)[-1]),
        "rsi14": None if (x := _rsi(closes)) is None else round(x, 1),
        "atr14": r(_atr(candles)),
        "vwap": r(_vwap(candles[-50:])),
        "cao_nhat_50_nen": r(max(c["high"] for c in candles[-50:])),
        "thap_nhat_50_nen": r(min(c["low"] for c in candles[-50:])),
    }


def _digits(symbol):
    return 3 if symbol.endswith("JPY") else (2 if symbol == "XAUUSD" else 5)


def _market_snapshot(symbol, timeframe):
    digits = _digits(symbol)
    htf = HIGHER_TF[timeframe]
    with mt5_service.session():
        tick = mt5_service.get_tick(symbol)
        main = mt5_service.get_candles(symbol, timeframe, 200)["du_lieu"]
        higher = mt5_service.get_candles(symbol, htf, 200)["du_lieu"]

    if len(main) < 60:
        raise RuntimeError(f"MT5 chỉ trả về {len(main)} nến {symbol} {timeframe}, chưa đủ để phân tích.")

    last_time = datetime.fromisoformat(main[-1]["thoi_gian"])
    age = int((datetime.now(timezone.utc) - last_time).total_seconds())

    return {
        "ma": symbol,
        "khung_thoi_gian": timeframe,
        "so_chu_so_thap_phan": digits,
        "bid": tick.get("bid"),
        "ask": tick.get("ask"),
        "do_tre_du_lieu_giay": age,
        "du_lieu_cu": age > 3 * 3600,
        "chi_bao": _indicators(main, digits),
        f"chi_bao_khung_{htf}": _indicators(higher, digits),
        "30_nen_gan_nhat": [
            [c["thoi_gian"][5:16], round(c["open"], digits), round(c["high"], digits),
             round(c["low"], digits), round(c["close"], digits), c["tick_volume"]]
            for c in main[-30:]
        ],
    }


# ---------- Chọn nguồn AI ----------

def _provider():
    p = os.getenv("PINYIN_AI_PROVIDER", "").strip().lower()
    if p in ("gemini", "claude"):
        return p
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    return "claude"


def _provider_key():
    return os.getenv("GEMINI_API_KEY") if _provider() == "gemini" else os.getenv("ANTHROPIC_API_KEY")


def _model_name():
    return GEMINI_MODEL if _provider() == "gemini" else MODEL


class AIError(Exception):
    """Lỗi đã dịch sang tiếng Việt để hiện cho người dùng."""


# ---------- Gemini (REST) ----------

class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """Kết nối HTTPS chỉ qua IPv4.

    Trên máy này IPv6 không ra được Internet: Python thử lần lượt từng địa chỉ IPv6
    của Google (mỗi địa chỉ chờ hết timeout) trước khi dùng IPv4, gây treo rất lâu.
    """

    def connect(self):
        ip = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        sock = socket.create_connection((ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4HTTPSConnection, req, context=self._context)


_gemini_opener = urllib.request.build_opener(_IPv4HTTPSHandler())


def _gemini_call(model, snapshot, timeout):
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": "Dữ liệu MT5 (định dạng nến: thời gian UTC MM-DD HH:MM, open, high, low, close, tick_volume):\n"
                               + json.dumps(snapshot, ensure_ascii=False)}],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": OUTPUT_SCHEMA,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    req = urllib.request.Request(
        GEMINI_URL.format(model=model),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "x-goog-api-key": os.getenv("GEMINI_API_KEY", ""),
        },
        method="POST",
    )
    with _gemini_opener.open(req, timeout=timeout) as res:
        data = json.loads(res.read().decode("utf-8"))

    cand = (data.get("candidates") or [{}])[0]
    reason = cand.get("finishReason")
    if reason == "SAFETY" or data.get("promptFeedback", {}).get("blockReason"):
        raise AIError("Gemini từ chối phân tích yêu cầu này.")
    if reason == "MAX_TOKENS":
        raise AIError("Phản hồi của Gemini bị cắt ngắn, hãy thử lại.")
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    if not text:
        raise AIError("Gemini không trả về nội dung.")
    result = json.loads(text)
    result["mo_hinh"] = data.get("modelVersion", model)
    return result


def _gemini_error(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        if e.code in (400, 403) and "API key" in msg:
            return "Gemini API key không hợp lệ hoặc đã bị khóa.", True
        if e.code == 429:
            return "Gemini hết hạn mức miễn phí tạm thời, thử lại sau ít phút.", False
        if e.code == 404:
            return f"Mô hình Gemini không còn khả dụng: {msg[:150]}", True
        return f"Gemini API lỗi {e.code}: {msg[:200]}", False
    if isinstance(e, (TimeoutError, FutureTimeout, urllib.error.URLError)):
        return "Gemini không phản hồi kịp (quá thời gian chờ).", False
    return f"Lỗi phân tích Gemini: {e}", False


def _ask_gemini(snapshot):
    """Thử mô hình chính, nếu quá tải/hết hạn mức thì chuyển sang mô hình dự phòng."""
    last = None
    for model, deadline in ((GEMINI_MODEL, 30), (GEMINI_BACKUP_MODEL, 25)):
        # Giới hạn thời gian cứng: Google đôi khi giữ kết nối rất lâu,
        # timeout của urllib chỉ áp dụng cho từng lần đọc socket.
        future = _gemini_pool.submit(_gemini_call, model, snapshot, deadline)
        try:
            return future.result(timeout=deadline)
        except AIError:
            raise
        except Exception as e:
            msg, fatal = _gemini_error(e)
            if fatal and "API key" in msg:
                raise AIError(msg)
            last = msg
    raise AIError(last)


# ---------- Claude ----------

def _get_client():
    global _client
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    with _client_lock:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic(timeout=90.0)
        return _client


def _ask_claude(snapshot):
    import anthropic

    client = _get_client()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": "Dữ liệu MT5 (định dạng nến: thời gian UTC MM-DD HH:MM, open, high, low, close, tick_volume):\n"
                       + json.dumps(snapshot, ensure_ascii=False),
        }],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude từ chối phân tích yêu cầu này.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Phản hồi của Claude bị cắt ngắn, hãy thử lại.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError("Claude không trả về nội dung.")
    result = json.loads(text)
    result["mo_hinh"] = response.model
    return result


def _ask_ai(snapshot):
    """Gọi nguồn AI đang cấu hình; mọi lỗi được đổi thành AIError tiếng Việt."""
    if _provider() == "gemini":
        result = _ask_gemini(snapshot)
    else:
        import anthropic
        try:
            result = _ask_claude(snapshot)
        except anthropic.AuthenticationError:
            raise AIError("Claude API key không hợp lệ.")
        except anthropic.PermissionDeniedError:
            raise AIError("Claude API key không có quyền dùng mô hình này.")
        except anthropic.RateLimitError:
            raise AIError("Claude đang giới hạn tần suất, thử lại sau ít phút.")
        except anthropic.APIConnectionError:
            raise AIError("Không kết nối được Claude API (kiểm tra mạng).")
        except anthropic.APIStatusError as e:
            raise AIError(f"Claude API lỗi {e.status_code}: {e.message}")
        except RuntimeError as e:
            raise AIError(str(e))
    result["do_tin_cay"] = max(0, min(100, int(result.get("do_tin_cay", 0))))
    return result


# ---------- Đường dẫn ----------

@router.get("/status")
def ai_status():
    provider = _provider()
    ten = "Gemini" if provider == "gemini" else "Claude"
    key_name = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
    base = {"nguon": provider, "ten_nguon": ten, "mo_hinh": _model_name()}
    if not _provider_key():
        return {**base, "trang_thai": "chua_cau_hinh", "nhan": "Chưa cấu hình API key",
                "ghi_chu": f"Thêm {key_name} vào file .env rồi khởi động lại FastAPI."}
    if _last_error:
        return {**base, "trang_thai": "loi", "nhan": f"{ten} báo lỗi", "ghi_chu": _last_error}
    return {**base, "trang_thai": "san_sang", "nhan": f"{ten} sẵn sàng",
            "ghi_chu": "Phân tích chỉ mang tính gợi ý; lệnh chỉ được gửi khi bạn xác nhận."}


@router.post("/analyze")
def analyze(req: AnalyzeIn):
    global _last_error

    symbol = req.symbol.upper().strip()
    timeframe = req.timeframe.upper().strip()
    if symbol not in SYMBOLS:
        return {"ok": False, "loi": f"Mã {symbol} chưa được hỗ trợ."}
    if timeframe not in TIMEFRAMES:
        return {"ok": False, "loi": "Khung thời gian hỗ trợ: M1, M5, M15, M30, H1, H4."}
    if not _provider_key():
        return {"ok": False, "loi": "Chưa cấu hình API key cho mô hình AI trong file .env."}

    key = (symbol, timeframe)
    cached = _cache.get(key)
    if cached and not req.lam_moi and time.time() - cached["ts"] < CACHE_SECONDS:
        return {**cached["data"], "tu_cache": True}

    try:
        snapshot = _market_snapshot(symbol, timeframe)
    except Exception as e:
        return {"ok": False, "loi": f"Không đọc được dữ liệu MT5: {e}"}

    try:
        result = _ask_ai(snapshot)
        _last_error = None
    except AIError as e:
        msg = str(e)
        if "key" in msg.lower():
            _last_error = msg
        return {"ok": False, "loi": msg}
    except Exception as e:
        return {"ok": False, "loi": f"Lỗi phân tích AI: {e}"}

    data = {
        "ok": True,
        "nguon": _provider(),
        "ma": symbol,
        "khung_thoi_gian": timeframe,
        "bid": snapshot["bid"],
        "ask": snapshot["ask"],
        "du_lieu_cu": snapshot["du_lieu_cu"],
        "chi_bao": snapshot["chi_bao"],
        "phan_tich": result,
        "thoi_gian": datetime.now(timezone.utc).isoformat(),
        "tu_cache": False,
    }
    _cache[key] = {"ts": time.time(), "data": data}
    return data
