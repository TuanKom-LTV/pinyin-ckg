from datetime import datetime, timezone
from fastapi import APIRouter
from app.services.mt5_service import mt5_service

router = APIRouter(prefix="/api/v1/market", tags=["Thị trường"])

MARKETS = [
    {
        "ma": "XAUUSD",
        "ten": "Vàng",
        "gia_du_phong": 2650.00,
        "don_vi": "USD"
    },
    {
        "ma": "EURUSD",
        "ten": "Euro / Đô la Mỹ",
        "gia_du_phong": 1.0850,
        "don_vi": "USD"
    },
    {
        "ma": "GBPUSD",
        "ten": "Bảng Anh / Đô la Mỹ",
        "gia_du_phong": 1.2700,
        "don_vi": "USD"
    },
    {
        "ma": "GBPJPY",
        "ten": "Bảng Anh / Yên Nhật",
        "gia_du_phong": 200.000,
        "don_vi": "JPY"
    }
]


def read_market_item(item):
    """Đọc giá một mã; phải gọi bên trong mt5_service.session()."""
    result = {
        "ma": item["ma"],
        "ten": item["ten"],
        "gia": item["gia_du_phong"],
        "don_vi": item["don_vi"],
        "bid": None,
        "ask": None,
        "spread": None,
        "thoi_gian": None,
        "trang_thai": "du_lieu_minh_hoa",
        "ghi_chu": "Đang sử dụng giá dự phòng, chưa có dữ liệu MT5 hợp lệ."
    }

    try:

        tick = mt5_service.get_tick(item["ma"])

        if tick.get("trang_thai") != "co_du_lieu":
            result["ghi_chu"] = tick.get(
                "ghi_chu",
                "MT5 chưa cung cấp giá hợp lệ."
            )
            return result

        bid = tick.get("bid")
        ask = tick.get("ask")

        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return result

        tick_time = datetime.fromisoformat(tick["thoi_gian"])
        do_tre = max(
            0,
            int((datetime.now(timezone.utc) - tick_time).total_seconds())
        )
        du_lieu_cu = do_tre > 120

        result.update({
            "gia": round((bid + ask) / 2, 3 if item["ma"].endswith("JPY") else (2 if item["ma"] == "XAUUSD" else 5)),
            "bid": bid,
            "ask": ask,
            "spread": tick.get("spread"),
            "thoi_gian": tick.get("thoi_gian"),
            "do_tre_giay": do_tre,
            "trang_thai": "du_lieu_mt5_cu" if du_lieu_cu else "du_lieu_mt5",
            "ghi_chu": (
                "Dữ liệu MT5 đã cũ, không đại diện cho giá hiện tại."
                if du_lieu_cu
                else "Giá đọc trực tiếp từ MT5, chế độ chỉ đọc."
            )
        })

        return result

    except Exception as e:
        result["ghi_chu"] = f"Không đọc được dữ liệu MT5: {str(e)}"
        return result



def read_all_markets():
    try:
        with mt5_service.session():
            return [read_market_item(item) for item in MARKETS]
    except Exception as e:
        # Không mở được MT5: trả giá dự phòng, đánh dấu rõ là minh họa.
        data = []
        for item in MARKETS:
            data.append({
                "ma": item["ma"],
                "ten": item["ten"],
                "gia": item["gia_du_phong"],
                "don_vi": item["don_vi"],
                "bid": None,
                "ask": None,
                "spread": None,
                "thoi_gian": None,
                "trang_thai": "du_lieu_minh_hoa",
                "ghi_chu": f"Không kết nối được MT5: {e}"
            })
        return data


@router.get("/overview")
def market_overview():
    try:
        with mt5_service.session():
            gold = read_market_item(MARKETS[0])
    except Exception as e:
        gold = read_all_markets()[0]
        gold["ghi_chu"] = f"Không kết nối được MT5: {e}"

    return {
        "trang_thai": gold["trang_thai"],
        "san_pham": gold["ma"],
        "gia": gold["gia"],
        "bid": gold["bid"],
        "ask": gold["ask"],
        "spread": gold["spread"],
        "thoi_gian": gold["thoi_gian"],
        "don_vi": gold["don_vi"],
        "bien_dong": None,
        "phan_tram": None,
        "ghi_chu": gold["ghi_chu"]
    }


@router.get("/symbols")
def get_symbols():
    data = read_all_markets()

    return {
        "tong_so": len(data),
        "du_lieu": data
    }


@router.get("/status")
def market_status():
    """Trạng thái kết nối nguồn dữ liệu thị trường (MT5, chỉ đọc)."""
    kiem_tra_luc = datetime.now(timezone.utc).isoformat()

    try:
        with mt5_service.session():
            info = mt5_service.get_status()
            items = [read_market_item(item) for item in MARKETS]
    except Exception as e:
        return {
            "trang_thai": "mat_ket_noi",
            "nhan": "Mất kết nối MT5",
            "ghi_chu": f"Không mở được terminal MT5: {e}",
            "kiem_tra_luc": kiem_tra_luc,
            "so_ma_co_gia": 0,
            "tong_so_ma": len(MARKETS),
        }

    co_gia = [x for x in items if x["trang_thai"].startswith("du_lieu_mt5")]
    gia_moi = [x for x in items if x["trang_thai"] == "du_lieu_mt5"]
    do_tre = [x["do_tre_giay"] for x in co_gia if x.get("do_tre_giay") is not None]

    if not info["terminal_ket_noi_broker"]:
        trang_thai, nhan, ghi_chu = (
            "chua_ket_noi_broker",
            "MT5 chưa kết nối broker",
            "Terminal MT5 đang mở nhưng chưa đăng nhập/kết nối máy chủ broker.",
        )
    elif gia_moi:
        trang_thai, nhan, ghi_chu = (
            "da_ket_noi",
            "Đã kết nối MT5",
            f"Giá trực tiếp {len(gia_moi)}/{len(MARKETS)} mã.",
        )
    elif co_gia:
        trang_thai, nhan, ghi_chu = (
            "da_ket_noi_thi_truong_dong",
            "Đã kết nối MT5 · Thị trường đóng",
            "Kết nối tốt nhưng chưa có tick mới (thị trường đóng cửa hoặc tạm ngưng).",
        )
    else:
        trang_thai, nhan, ghi_chu = (
            "chua_co_gia",
            "MT5 chưa có giá",
            "Đã kết nối nhưng broker chưa cung cấp giá cho các mã theo dõi.",
        )

    return {
        "trang_thai": trang_thai,
        "nhan": nhan,
        "ghi_chu": ghi_chu,
        "kiem_tra_luc": kiem_tra_luc,
        "may_chu": info["may_chu"],
        "cong_ty": info["cong_ty"],
        "ping_ms": info["ping_ms"],
        "so_ma_co_gia": len(co_gia),
        "so_ma_gia_moi": len(gia_moi),
        "tong_so_ma": len(MARKETS),
        "do_tre_nho_nhat_giay": min(do_tre) if do_tre else None,
    }
