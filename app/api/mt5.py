from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from app.services.mt5_service import mt5_service


router = APIRouter(
    prefix="/api/v1/market/mt5",
    tags=["Dữ liệu MT5"]
)


@router.get("/tick")
def get_mt5_tick(symbol: str = "XAUUSD"):
    try:
        with mt5_service.session():
            data = mt5_service.get_tick(symbol)

        if not data.get("thoi_gian"):
            data["do_tre_giay"] = None
            data["trang_thai_du_lieu"] = "khong_co"
            return data

        tick_time = datetime.fromisoformat(data["thoi_gian"])
        age = max(
            0,
            int((datetime.now(timezone.utc) - tick_time).total_seconds())
        )

        data["do_tre_giay"] = age
        data["trang_thai_du_lieu"] = (
            "moi" if age <= 120 else "cu"
        )

        return data

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/candles")
def get_mt5_candles(
    symbol: str = "XAUUSD",
    timeframe: str = "M1",
    count: int = Query(default=100, ge=1, le=1000)
):
    try:
        with mt5_service.session():
            data = mt5_service.get_candles(
                symbol=symbol,
                timeframe=timeframe,
                count=count
            )

        if data["du_lieu"]:
            latest_time = datetime.fromisoformat(
                data["du_lieu"][-1]["thoi_gian"]
            )
            age = max(
                0,
                int((datetime.now(timezone.utc) - latest_time).total_seconds())
            )
        else:
            age = None

        data["do_tre_giay"] = age
        data["trang_thai_du_lieu"] = (
            "moi" if age is not None and age <= 120 else "cu"
        )

        return data

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
