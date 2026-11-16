from pathlib import Path

path = Path("frontend/js/market.js")
content = path.read_text(encoding="utf-8")

if "async function loadMarketOverview()" in content:
    raise SystemExit("HAM DA TON TAI - KHONG THEM LAN NUA.")

addition = r"""

// Cap nhat the tong quan tu API MT5 (chi doc du lieu).
async function loadMarketOverview() {
    const priceElement = document.getElementById("overview-price");
    const noteElement = document.getElementById("overview-price-note");
    const statusElement = document.getElementById("overview-data-status");

    if (!priceElement || !noteElement || !statusElement) return;

    try {
        const response = await fetch("/api/v1/market/overview", {
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error("API tong quan khong phan hoi thanh cong.");
        }

        const data = await response.json();
        const status = data.trang_thai || "";

        if (
            (status === "du_lieu_mt5" || status === "du_lieu_mt5_cu") &&
            Number.isFinite(Number(data.gia)) &&
            Number(data.gia) > 0
        ) {
            priceElement.textContent = Number(data.gia).toLocaleString(
                "vi-VN",
                {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2
                }
            );

            if (status === "du_lieu_mt5_cu") {
                statusElement.textContent = "MT5 - DU LIEU DA CU";
                noteElement.textContent =
                    "Gia gan nhat, khong phai gia hien tai";
            } else {
                statusElement.textContent = "MT5 - DU LIEU MOI";
                noteElement.textContent =
                    "Bid: " + Number(data.bid).toFixed(2) +
                    " | Ask: " + Number(data.ask).toFixed(2);
            }
        } else {
            priceElement.textContent = "Chưa có dữ liệu";
            noteElement.textContent =
                data.ghi_chu || "MT5 chưa cung cấp giá hợp lệ.";
            statusElement.textContent = "CHUA CO DU LIEU MT5";
        }
    } catch (error) {
        priceElement.textContent = "Không khả dụng";
        noteElement.textContent = "Không đọc được API MT5.";
        statusElement.textContent = "LOI KET NOI";
        console.error("Loi cap nhat tong quan:", error);
    }
}

loadMarketOverview();
setInterval(loadMarketOverview, 5000);
"""

path.write_text(content + addition, encoding="utf-8")
print("DA THEM HAM CAP NHAT TONG QUAN MT5")
