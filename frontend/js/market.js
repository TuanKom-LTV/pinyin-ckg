let isLoadingMarkets = false;

async function loadMarkets() {
    if (isLoadingMarkets) return;

    const container = document.getElementById("market-list");
    if (!container) return;

    isLoadingMarkets = true;

    try {
        const response = await fetch("/api/v1/market/symbols", {
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error("Không thể tải dữ liệu thị trường");
        }

        const result = await response.json();

        container.innerHTML = result.du_lieu.map((item, index) => {
            const status = item.trang_thai || "du_lieu_minh_hoa";
            const isMT5 = status.startsWith("du_lieu_mt5");
            const isOld = status === "du_lieu_mt5_cu";

            const marketClosed =
                ["XAUUSD", "EURUSD", "GBPUSD", "GBPJPY"].includes(item.ma) &&
                [0, 6].includes(new Date().getDay());

            const price = Number(item.gia).toLocaleString("en-US", {
                minimumFractionDigits: item.ma === "XAUUSD" ? 2 : (item.ma === "GBPJPY" ? 3 : 5),
                maximumFractionDigits: item.ma === "XAUUSD" ? 2 : (item.ma === "GBPJPY" ? 3 : 5)
            });

            const statusText = marketClosed && isMT5
                ? "MT5 - Thị trường đóng cửa"
                : isOld
                    ? "MT5 - Dữ liệu đã cũ"
                    : isMT5
                        ? "MT5 - Dữ liệu mới"
                        : "Dữ liệu minh họa";

            const statusClass = marketClosed && isMT5
                ? "market-status-closed"
                : isOld
                    ? "market-status-old"
                    : isMT5
                        ? "market-status-live"
                        : "market-status-demo";

            return `
                <button
                    type="button"
                    class="market-card market-tab ${index === 0 ? "selected" : ""}"
                    data-symbol="${item.ma}"
                    aria-pressed="${index === 0 ? "true" : "false"}">

                    <span class="market-tab-name">${item.ma}</span>
                    <span class="market-tab-description">${item.ten}</span>
                    <strong>${price}</strong>
                    <span class="market-tab-unit">${item.don_vi}</span>
                    <small class="${statusClass}">${statusText}</small>
                    ${isMT5 && item.do_tre_giay != null
                        ? `<small>Độ trễ: ${Number(item.do_tre_giay).toLocaleString("vi-VN")} giây</small>`
                        : ""}
                </button>
            `;
        }).join("");

        container.querySelectorAll(".market-tab").forEach(button => {
            button.addEventListener("click", () => {
                const symbol = button.dataset.symbol;

                container.querySelectorAll(".market-tab").forEach(tab => {
                    const active = tab === button;
                    tab.classList.toggle("selected", active);
                    tab.setAttribute("aria-pressed", String(active));
                });

                const type = document.querySelector(".chart-type-btn.active")?.dataset.type || "line";
                const timeframe = document.getElementById("chart-timeframe")?.value || "M1";

                if (typeof renderMarketChart === "function") {
                    renderMarketChart(symbol, type, timeframe);
                }
            });
        });

    } catch (error) {
        console.error("Lỗi tải thị trường:", error);
    } finally {
        isLoadingMarkets = false;
    }
}

loadMarkets();

setInterval(() => {
    loadMarkets();
}, 5000);






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
