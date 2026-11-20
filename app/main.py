from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.market import router as market_router
from app.api.mt5 import router as mt5_router
from app.api.ckg import router as ckg_router
from app.api.ckg_trade import router as ckg_trade_router
from app.api.ckg_ai import router as ckg_ai_router
from app.api.ai import router as ai_router
from app.api.trade import router as trade_router
from app.api.ckg_auto import router as ckg_auto_router, start as start_auto_bot
from app.api.ckg_quick import router as ckg_quick_router, start as start_guard
from app.api.khach import router as khach_router
from app.api.ckg_news import router as ckg_news_router
from app.api.ea import router as ea_router
from app.core.auth import auth_middleware


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(
    title="PinYin AI Trading Platform",
    description="Nền tảng phân tích và giao dịch mô phỏng",
    version="1.0.0"
)

# Truy cập từ xa (qua tunnel) phải đăng nhập; mở trên máy này thì vào thẳng.
app.middleware("http")(auth_middleware)

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

app.include_router(market_router)
app.include_router(mt5_router)
app.include_router(ckg_router)
app.include_router(ckg_trade_router)
app.include_router(ckg_ai_router)
app.include_router(ai_router)
app.include_router(trade_router)
app.include_router(ckg_auto_router)
app.include_router(ckg_quick_router)
app.include_router(khach_router)
app.include_router(ckg_news_router)
app.include_router(ea_router)


@app.on_event("startup")
def _start_bot():
    # Bot tự động vàng chạy nền; cài đặt mặc định TẮT, bật trong trang CKG (mục vàng)
    start_auto_bot()
    start_guard()   # lá chắn hòa vốn / trailing của buồng lái nhanh
    from app.services import cbot
    cbot.start()    # bot riêng của khách (chủ duyệt), chỉ chạy khi có khách bật bot
    from app.services import health, copytrade
    health.start()  # theo dõi MT5 / vòng bot (báo Telegram) + tự sao lưu data/ mỗi ngày
    copytrade._start()
    from app.api import ckg_ai
    ckg_ai.start_auto()   # Phân Tích AI tự cập nhật: XAUUSD mỗi nến H1, các cặp khác mỗi nến H4


@app.on_event("startup")
async def _start_perf():
    # hàm đồng bộ (def) chạy trong threadpool 40 luồng mặc định: nhiều trang / EA chờ MT5 cùng lúc thì hết luồng,
    # request sau (kể cả /health) phải xếp hàng → nâng lên 120
    import asyncio
    from app.services import health
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = 120
    except Exception:
        pass
    asyncio.get_running_loop().create_task(health.lag_probe())


@app.get("/", include_in_schema=False)
def home():
    # Trang chính cũ (PinYin AI) không dùng nữa: địa chỉ gốc vào thẳng trang CKG.
    # File cũ vẫn giữ ở frontend/index.html, mở lại bằng /trang-cu nếu cần.
    return RedirectResponse("/ckg/", status_code=302)


@app.get("/trang-cu", include_in_schema=False)
def old_home():
    return FileResponse(
        FRONTEND_DIR / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/v1/health")
def health():
    return {
        "status": "hoat_dong",
        "ten": "PinYin AI Trading Platform",
        "phien_ban": "1.0.0"
    }


@app.get("/health", include_in_schema=False)
def health_check():
    """Cho UptimeRobot: 200 khi máy chủ + MT5 bình thường, 503 khi MT5 mất kết nối sàn."""
    from fastapi.responses import JSONResponse
    from app.services import health
    ok, data = health.public()
    return JSONResponse(data, status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})

