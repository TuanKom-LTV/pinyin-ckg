import MetaTrader5 as mt5
from app.services import symbols as _symbols
_symbols.install()   # tên chuẩn (XAUUSD) ↔ tên trên sàn (XAUUSD.sc, XAUUSDm, GOLD...)
from datetime import datetime, timezone
from contextlib import contextmanager
from threading import RLock


import os
DEFAULT_MT5_PATH = r"C:\Program Files\MetaTrader 5 - Copy\terminal64.exe"


def mt5_path():
    """terminal64.exe: lấy MT5_PATH trong .env (máy VPS), không có thì dùng mặc định của máy nhà."""
    return os.getenv("MT5_PATH") or DEFAULT_MT5_PATH

INIT_TIMEOUT_MS = 15000   # chờ MT5 kết nối tối đa 15 giây
LOCK_WAIT = 20            # chờ phiên MT5 khác xong tối đa 20 giây


class MT5Service:
    """Dịch vụ kết nối và đọc dữ liệu MT5, chỉ đọc."""

    SYMBOLS = {
        "XAUUSD",
        "EURUSD",
        "GBPUSD",
        "GBPJPY",
        "USDJPY",  # Mã nội bộ để quy đổi P/L GBPJPY sang USD trong mô phỏng.
    }

    TIMEFRAMES = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
    }

    def __init__(self):
        self.connected = False
        self._lock = RLock()

    @contextmanager
    def session(self):
        """Giữ một phiên MT5 độc quyền cho toàn bộ thao tác đọc.

        Không chờ khóa vô hạn: nếu một lệnh MT5 khác bị kẹt, request này báo lỗi
        sau LOCK_WAIT giây thay vì xếp hàng mãi làm treo cả máy chủ.
        """
        if not self._lock.acquire(timeout=LOCK_WAIT):
            raise RuntimeError("MT5 đang bận (lệnh trước chưa xong), thử lại sau vài giây.")
        try:
            self.connect()
            try:
                yield self
            finally:
                self.disconnect()
        finally:
            self._lock.release()

    def connect(self):
        if self.connected:
            return True

        # timeout: không để lệnh kết nối treo mãi khi MT5 đang khởi động / hiện hộp thoại
        if not mt5.initialize(path=mt5_path(), timeout=INIT_TIMEOUT_MS):
            raise RuntimeError(
                f"Không thể kết nối MT5: {mt5.last_error()}"
            )

        self.connected = True
        return True

    def disconnect(self):
        if self.connected:
            mt5.shutdown()
            self.connected = False

    def get_status(self):
        """Trạng thái terminal MT5 và kết nối máy chủ broker (chỉ đọc)."""
        self.connect()

        terminal = mt5.terminal_info()
        account = mt5.account_info()

        return {
            "terminal_ket_noi_broker": bool(terminal and terminal.connected),
            "ten_terminal": terminal.name if terminal else None,
            "may_chu": account.server if account else None,
            "cong_ty": account.company if account else None,
            "ping_ms": (
                round(terminal.ping_last / 1000, 1)
                if terminal and terminal.ping_last else None
            ),
        }

    def validate_symbol(self, symbol):
        symbol = symbol.upper().strip()

        if symbol not in self.SYMBOLS:
            raise ValueError(
                f"Mã {symbol} chưa được hỗ trợ."
            )

        return symbol

    def select_symbol(self, symbol):
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"Không thể bật mã {symbol}: {mt5.last_error()}"
            )

        info = mt5.symbol_info(symbol)

        if info is None:
            raise RuntimeError(
                f"Không tìm thấy thông tin mã {symbol}."
            )

        return info

    def get_tick(self, symbol="XAUUSD"):
        self.connect()

        symbol = self.validate_symbol(symbol)
        self.select_symbol(symbol)

        tick = mt5.symbol_info_tick(symbol)

        if tick is None:
            raise RuntimeError(
                f"Không đọc được tick {symbol}: {mt5.last_error()}"
            )

        if tick.time <= 0 or tick.bid <= 0 or tick.ask <= 0:
            return {
                "ma": symbol,
                "bid": None,
                "ask": None,
                "spread": None,
                "thoi_gian": None,
                "nguon": "MT5",
                "trang_thai": "chua_co_gia",
                "ghi_chu": "MT5 chưa cung cấp tick hợp lệ."
            }

        return {
            "ma": symbol,
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "spread": round(float(tick.ask - tick.bid), 3 if symbol.endswith("JPY") else 5),
            "thoi_gian": datetime.fromtimestamp(
                tick.time, timezone.utc
            ).isoformat(),
            "nguon": "MT5",
            "trang_thai": "co_du_lieu"
        }

    def get_candles(self, symbol="XAUUSD", timeframe="M1", count=100):
        self.connect()

        symbol = self.validate_symbol(symbol)
        timeframe = timeframe.upper().strip()

        if timeframe not in self.TIMEFRAMES:
            raise ValueError(
                "Khung thời gian hỗ trợ: M1, M5, M15, M30, H1, H4."
            )

        if count < 1 or count > 1000:
            raise ValueError(
                "Số nến phải từ 1 đến 1000."
            )

        self.select_symbol(symbol)

        rates = mt5.copy_rates_from_pos(
            symbol,
            self.TIMEFRAMES[timeframe],
            0,
            count
        )

        if rates is None:
            raise RuntimeError(
                f"Không đọc được nến {symbol} {timeframe}: "
                f"{mt5.last_error()}"
            )

        candles = []

        for candle in rates:
            candles.append({
                "thoi_gian": datetime.fromtimestamp(
                    int(candle["time"]), timezone.utc
                ).isoformat(),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "tick_volume": int(candle["tick_volume"]),
            })

        return {
            "ma": symbol,
            "khung_thoi_gian": timeframe,
            "so_nen": len(candles),
            "du_lieu": candles,
            "nguon": "MT5",
            "che_do": "chi_doc",
            "trang_thai": (
                "co_du_lieu" if candles else "chua_co_du_lieu"
            )
        }


mt5_service = MT5Service()
