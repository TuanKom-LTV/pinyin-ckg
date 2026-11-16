(() => {
    "use strict";

    const API_BASE = "/api/v1/market/mt5";
    const REFRESH_INTERVAL = 5000;

    const SUPPORTED_SYMBOLS = [
        "XAUUSD",
        "EURUSD",
        "GBPUSD",
        "GBPJPY"
    ];

    const SUPPORTED_TIMEFRAMES = [
        "M1", "M5", "M15", "M30", "H1", "H4"
    ];

    const originalRender = renderMarketChart;
    const originalBuildOHLC = buildOHLC;
    const originalBuildTimeLabels = buildTimeLabels;

    const originalPrices = {};
    Object.keys(chartData).forEach(symbol => {
        originalPrices[symbol] = [...chartData[symbol].prices];
    });

    let mt5Candles = null;
    let mt5DataKey = "";
    let currentSymbol = "XAUUSD";
    let currentTimeframe = "M1";
    let currentType = "line";
    let refreshTimer = null;
    let isLoading = false;

    function getPriceDigits(symbol) {
        if (symbol === "XAUUSD") return 2;
        if (symbol === "GBPJPY") return 3;
        return 5;
    }

    function formatPrice(value, digits = 2) {
        return Number(value).toLocaleString("en-US", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        });
    }

    function showStatus(message, warning = false) {
        let element = document.getElementById("mt5-data-status");

        if (!element) {
            element = document.createElement("div");
            element.id = "mt5-data-status";

            element.style.cssText = [
                "margin:10px 0",
                "padding:10px 14px",
                "border-radius:8px",
                "font-size:13px",
                "background:#1e293b",
                "color:#e2e8f0"
            ].join(";");

            const container = document.getElementById("market-chart");

            if (container && container.parentNode) {
                container.parentNode.insertBefore(element, container);
            }
        }

        const marketClosed = message.includes("Thị trường đóng cửa");

        element.textContent = message;
        element.style.border = marketClosed
            ? "1px solid #64748b"
            : (warning ? "1px solid #f59e0b" : "1px solid #22c55e");

        const chartLabel = document.getElementById("chart-data-status");

        if (chartLabel) {
            chartLabel.textContent = marketClosed
                ? "MT5 - Thị trường đóng cửa"
                : (warning
                    ? "MT5 - Không lấy được dữ liệu"
                    : (message.includes("đã cũ")
                        ? "MT5 - Dữ liệu đã cũ"
                        : "MT5 - Dữ liệu mới"));

            chartLabel.style.color = marketClosed
                ? "#94a3b8"
                : (warning ? "#f59e0b" : "#22c55e");
        }
    }

    async function fetchJSON(url) {
        const response = await fetch(url, {
            cache: "no-store"
        });

        if (!response.ok) {
            let detail = `API HTTP ${response.status}`;

            try {
                const body = await response.json();
                if (body.detail) detail = body.detail;
            } catch (_) {}

            throw new Error(detail);
        }

        return response.json();
    }

    function resetMockPrices() {
        Object.keys(originalPrices).forEach(symbol => {
            if (chartData[symbol]) {
                chartData[symbol].prices = [...originalPrices[symbol]];
            }
        });
    }

    buildOHLC = function(prices) {
        if (
            mt5Candles &&
            mt5DataKey === `${currentSymbol}_${currentTimeframe}` &&
            chartData[currentSymbol] &&
            prices === chartData[currentSymbol].prices
        ) {
            return mt5Candles.map(candle => ({
                open: Number(candle.open),
                high: Number(candle.high),
                low: Number(candle.low),
                close: Number(candle.close)
            }));
        }

        return originalBuildOHLC(prices);
    };

    buildTimeLabels = function(count, timeframe) {
        if (
            mt5Candles &&
            mt5DataKey === `${currentSymbol}_${currentTimeframe}` &&
            mt5Candles.length === count
        ) {
            return mt5Candles.map(candle => {
                const date = new Date(candle.thoi_gian);

                const day = String(date.getDate()).padStart(2, "0");
                const month = String(date.getMonth() + 1).padStart(2, "0");
                const hours = String(date.getHours()).padStart(2, "0");
                const minutes = String(date.getMinutes()).padStart(2, "0");

                return {
                    date: `${day}/${month}`,
                    time: `${hours}:${minutes}`
                };
            });
        }

        return originalBuildTimeLabels(count, timeframe);
    };

    async function updateMT5Chart() {
        if (isLoading) return;

        if (!SUPPORTED_SYMBOLS.includes(currentSymbol)) return;
        if (!SUPPORTED_TIMEFRAMES.includes(currentTimeframe)) return;

        const symbol = currentSymbol;
        const timeframe = currentTimeframe;
        const dataKey = `${symbol}_${timeframe}`;

        isLoading = true;

        try {
            const [tick, candleData] = await Promise.all([
                fetchJSON(`${API_BASE}/tick?symbol=${symbol}`),
                fetchJSON(
                    `${API_BASE}/candles?symbol=${symbol}&timeframe=${timeframe}&count=100`
                )
            ]);

            if (
                symbol !== currentSymbol ||
                timeframe !== currentTimeframe
            ) {
                return;
            }

            const candles = candleData.du_lieu;

            if (!Array.isArray(candles) || candles.length < 2) {
                throw new Error("Không đủ dữ liệu nến MT5");
            }

            for (const candle of candles) {
                const values = [
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close
                ].map(Number);

                if (
                    values.some(value => !Number.isFinite(value)) ||
                    values.some(value => value <= 0) ||
                    values[1] < Math.max(values[0], values[3]) ||
                    values[2] > Math.min(values[0], values[3])
                ) {
                    throw new Error("Dữ liệu OHLC không hợp lệ");
                }
            }

            const market = chartData && chartData[symbol];
            if (!market) {
                throw new Error(
                    `Thiếu cấu hình ${symbol} trong chart.js. Hãy tải lại bộ giao diện mới.`
                );
            }

            mt5Candles = candles;
            mt5DataKey = dataKey;
            market.prices = candles.map(candle => Number(candle.close));

            const tickValid =
                tick.trang_thai === "co_du_lieu" &&
                Number(tick.bid) > 0 &&
                Number(tick.ask) > 0;

            const candleOld =
                candleData.trang_thai_du_lieu !== "moi";

            const tickOld =
                tickValid &&
                tick.trang_thai_du_lieu !== "moi";

            const oldData = candleOld || tickOld;

            let status = [
                "Nguồn: MT5",
                `Mã: ${symbol}`,
                `Khung: ${timeframe}`,
                `Nến: ${candles.length}`
            ];

            if (tickValid) {
                status.push(`Bid: ${formatPrice(tick.bid, getPriceDigits(symbol))}`);
                status.push(`Ask: ${formatPrice(tick.ask, getPriceDigits(symbol))}`);
                status.push(`Spread: ${formatPrice(tick.spread, getPriceDigits(symbol))}`);
            } else {
                status.push("Chưa có tick giá hợp lệ");
            }

            const marketClosed =
                ["XAUUSD", "EURUSD", "GBPUSD", "GBPJPY"].includes(symbol) &&
                [0, 6].includes(new Date().getDay());

            status.push(
                marketClosed
                    ? "Thị trường đóng cửa - dữ liệu phiên gần nhất"
                    : (oldData
                        ? "Dữ liệu phiên gần nhất - đã cũ"
                        : "Dữ liệu mới")
            );

            status.push("Chế độ: Chỉ đọc");

            showStatus(
                status.join(" | "),
                marketClosed ? false : (oldData || !tickValid)
            );

            originalRender(symbol, currentType, timeframe);

            const priceElement = document.querySelector(".chart-price");

            if (priceElement) {
                priceElement.textContent =
                    `${formatPrice(candles[candles.length - 1].close, getPriceDigits(symbol))} ${symbol === "GBPJPY" ? "JPY" : "USD"}`;
            }

        } catch (error) {
            console.error("Lỗi cập nhật biểu đồ MT5:", error);

            if (
                mt5Candles &&
                mt5DataKey === dataKey
            ) {
                originalRender(symbol, currentType, timeframe);

                showStatus(
                    `${symbol} ${timeframe}: Không cập nhật được MT5. Đang giữ dữ liệu đã tải trước đó. ${error.message}`,
                    true
                );
            } else {
                mt5Candles = null;
                mt5DataKey = "";

                resetMockPrices();
                originalRender(symbol, currentType, timeframe);

                showStatus(
                    `${symbol} ${timeframe}: Chưa lấy được dữ liệu MT5. Biểu đồ đang dùng dữ liệu minh họa. ${error.message}`,
                    true
                );
            }
        } finally {
            isLoading = false;
        }
    }

    renderMarketChart = function(
        symbol = "XAUUSD",
        type = "line",
        timeframe = "M1"
    ) {
        currentSymbol = symbol;
        currentType = type;
        currentTimeframe = timeframe;

        if (
            SUPPORTED_SYMBOLS.includes(symbol) &&
            SUPPORTED_TIMEFRAMES.includes(timeframe)
        ) {
            updateMT5Chart();
            return;
        }

        mt5Candles = null;
        mt5DataKey = "";

        resetMockPrices();
        originalRender(symbol, type, timeframe);

        showStatus(
            `${symbol} ${timeframe}: mã hoặc khung thời gian chưa được kết nối MT5.`,
            true
        );
    };

    function startMT5Chart() {
        if (refreshTimer) {
            clearInterval(refreshTimer);
        }

        const symbol =
            document.getElementById("chart-symbol")?.value || "XAUUSD";

        const timeframe =
            document.getElementById("chart-timeframe")?.value || "M1";

        const type =
            document.querySelector(".chart-type-btn.active")
                ?.dataset.type || "line";

        renderMarketChart(symbol, type, timeframe);

        refreshTimer = setInterval(() => {
            updateMT5Chart();
        }, REFRESH_INTERVAL);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", startMT5Chart);
    } else {
        startMT5Chart();
    }
})();


