const chartContainer = document.getElementById("market-chart");

const chartData = {
    XAUUSD: {
        name: "Vàng",
        prices: [2642,2645,2641,2648,2650,2647,2653,2651,2656,2652,2658,2655,2660,2657,2663,2661,2666,2662,2668,2670,2667,2672,2669,2674,2671,2676,2673,2678,2675,2680,2677,2682,2679,2684,2681,2686,2683,2688,2685,2690]
    },
    EURUSD: {
        name: "Euro / Đô la Mỹ",
        prices: [1.0810,1.0820,1.0805,1.0830,1.0840,1.0825,1.0850,1.0840,1.0860,1.0850,1.0870,1.0860,1.0880,1.0870,1.0890,1.0880,1.0900,1.0890,1.0910,1.0920,1.0905,1.0925,1.0915,1.0930,1.0920,1.0940,1.0930,1.0950,1.0940,1.0960,1.0950,1.0970,1.0960,1.0980,1.0970,1.0990,1.0980,1.1000,1.0990,1.1010]
    },
    GBPUSD: {
        name: "Bảng Anh / Đô la Mỹ",
        prices: [1.2650,1.2660,1.2640,1.2670,1.2680,1.2660,1.2690,1.2680,1.2700,1.2690,1.2710,1.2700,1.2720,1.2710,1.2730,1.2720,1.2740,1.2730,1.2750,1.2760,1.2745,1.2765,1.2755,1.2770,1.2760,1.2780,1.2770,1.2790,1.2780,1.2800,1.2790,1.2810,1.2800,1.2820,1.2810,1.2830,1.2820,1.2840,1.2830,1.2850]
    },
    GBPJPY: { name: "Bảng Anh / Yên Nhật", prices: [190.10,190.25,190.05,190.40,190.55,190.30,190.65,190.50,190.80,190.70,190.95,190.85,191.10,190.90,191.20,191.05,191.35,191.15,191.45,191.30] }
};

const TIMEFRAMES = {
    M1: 60 * 1000,
    M5: 5 * 60 * 1000,
    M15: 15 * 60 * 1000,
    M30: 30 * 60 * 1000,
    H1: 60 * 60 * 1000,
    H4: 4 * 60 * 60 * 1000
};

function buildOHLC(prices) {
    return prices.map((close, index) => {
        const open = index === 0
            ? close - (prices[1] - prices[0]) * 0.5
            : prices[index - 1];

        const range = Math.abs(close - open) * 0.6 +
            Math.abs(close) * 0.0005;

        return {
            open,
            high: Math.max(open, close) + range,
            low: Math.min(open, close) - range,
            close
        };
    });
}

function buildHeikinAshi(data) {
    const result = [];

    data.forEach((candle, index) => {
        const close = (
            candle.open + candle.high +
            candle.low + candle.close
        ) / 4;

        const open = index === 0
            ? (candle.open + candle.close) / 2
            : (result[index - 1].open + result[index - 1].close) / 2;

        result.push({
            open,
            close,
            high: Math.max(candle.high, open, close),
            low: Math.min(candle.low, open, close)
        });
    });

    return result;
}

function formatPrice(value, symbol) {
    const digits = symbol === "XAUUSD" ? 2 : (symbol === "GBPJPY" ? 3 : 5);

    return value.toLocaleString("en-US", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
    });
}

function buildTimeLabels(count, timeframe) {
    const interval = TIMEFRAMES[timeframe] || TIMEFRAMES.M1;
    const now = new Date();
    const start = now.getTime() - interval * (count - 1);

    return Array.from({ length: count }, (_, index) => {
        const date = new Date(start + interval * index);

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

function renderMarketChart(
    symbol = "XAUUSD",
    type = "line",
    timeframe = "M1"
) {
    if (!chartContainer) return;

    const market = chartData[symbol];
    if (!market || !Array.isArray(market.prices) || market.prices.length < 2) {
        chartContainer.textContent = `Chưa có dữ liệu biểu đồ hợp lệ cho ${symbol}.`;
        return;
    }

    const quoteCurrency = symbol.endsWith("JPY") ? "JPY" : "USD";
    let candles = buildOHLC(market.prices);

    if (type === "heikin") {
        candles = buildHeikinAshi(candles);
    }

    const width = 1000;
    const height = 390;

    const left = 25;
    const right = 145;
    const top = 25;
    const bottom = 70;

    const plotRight = width - right;
    const plotBottom = height - bottom;
    const plotWidth = plotRight - left;
    const plotHeight = plotBottom - top;

    const allValues = candles.flatMap(c => [c.high, c.low]);
    const rawMin = Math.min(...allValues);
    const rawMax = Math.max(...allValues);
    const paddingValue = (rawMax - rawMin) * 0.12 || 1;

    const min = rawMin - paddingValue;
    const max = rawMax + paddingValue;
    const range = max - min;

    const x = index =>
        left + index * plotWidth / (candles.length - 1);

    const y = price =>
        top + ((max - price) / range) * plotHeight;

    const lastPrice = market.prices[market.prices.length - 1];
    const timeLabels = buildTimeLabels(candles.length, timeframe);

    let chartSvg = "";

    // Trục giá bên phải và đường lưới ngang
    for (let i = 0; i <= 6; i++) {
        const price = max - range * i / 6;
        const lineY = top + plotHeight * i / 6;

        chartSvg += `
            <line x1="${left}" y1="${lineY}"
                  x2="${plotRight}" y2="${lineY}"
                  stroke="#334155"
                  stroke-width="1"
                  stroke-dasharray="4 5"/>

            <text x="${plotRight + 12}" y="${lineY + 4}"
                  fill="#cbd5e1"
                  font-size="12">
                ${formatPrice(price, symbol)}
            </text>
        `;
    }

    // Trục thời gian bên dưới: ngày/tháng và giờ/phút
    const tickIndexes = [0, 5, 10, 15, candles.length - 1];

    tickIndexes.forEach(index => {
        const tickX = x(index);
        const label = timeLabels[index];

        chartSvg += `
            <line x1="${tickX}" y1="${top}"
                  x2="${tickX}" y2="${plotBottom}"
                  stroke="#26334b"
                  stroke-width="1"
                  stroke-dasharray="3 7"
                  opacity="0.45"/>

            <line x1="${tickX}" y1="${plotBottom}"
                  x2="${tickX}" y2="${plotBottom + 5}"
                  stroke="#64748b"/>

            <text x="${tickX}" y="${plotBottom + 23}"
                  fill="#cbd5e1"
                  font-size="11"
                  text-anchor="middle">
                <tspan x="${tickX}">${label.date}</tspan>
                <tspan x="${tickX}" dy="14">${label.time}</tspan>
            </text>
        `;
    });

    // Vẽ biểu đồ đường
    if (type === "line") {
        const points = market.prices.map((price, index) =>
            `${x(index)},${y(price)}`
        ).join(" ");

        chartSvg += `
            <polyline points="${points}"
                fill="none"
                stroke="#38bdf8"
                stroke-width="3"
                stroke-linecap="round"
                stroke-linejoin="round"/>
        `;
    } else {
        // Vẽ nến Nhật hoặc Heikin Ashi
        const candleWidth = Math.min(24, plotWidth / candles.length * 0.65);

        candles.forEach((candle, index) => {
            const rising = candle.close >= candle.open;
            const color = rising ? "#22c55e" : "#ef4444";

            const bodyTop = y(Math.max(candle.open, candle.close));
            const bodyBottom = y(Math.min(candle.open, candle.close));
            const bodyHeight = Math.max(2, bodyBottom - bodyTop);

            chartSvg += `
                <line x1="${x(index)}" y1="${y(candle.high)}"
                      x2="${x(index)}" y2="${y(candle.low)}"
                      stroke="${color}" stroke-width="2"/>

                <rect x="${x(index) - candleWidth / 2}"
                      y="${bodyTop}"
                      width="${candleWidth}"
                      height="${bodyHeight}"
                      fill="${color}"/>
            `;
        });
    }

    // Đường giá hiện tại và nhãn giá bên phải
    const currentY = y(lastPrice);

    chartSvg += `
        <line x1="${left}" y1="${currentY}"
              x2="${plotRight}" y2="${currentY}"
              stroke="#f59e0b"
              stroke-width="1.5"
              stroke-dasharray="5 4"/>

        <rect x="${plotRight + 5}" y="${currentY - 12}"
              width="${right - 10}" height="24"
              rx="3" fill="#f59e0b"/>

        <text x="${plotRight + right / 2}"
              y="${currentY + 4}"
              text-anchor="middle"
              fill="#111827"
              font-size="12"
              font-weight="bold">
            ${formatPrice(lastPrice, symbol)}
        </text>
    `;

    chartContainer.innerHTML = `
        <div class="chart-info">
            <div>
                <strong>${symbol}</strong>
                <span>${market.name}</span>
            </div>

            <div class="chart-controls">
                <select id="chart-symbol">
                    ${Object.keys(chartData).map(key => `
                        <option value="${key}" ${key === symbol ? "selected" : ""}>
                            ${key}
                        </option>
                    `).join("")}
                </select>

                <div class="chart-type-tabs">

    <button type="button"
            class="chart-type-btn ${type === "line" ? "active" : ""}"
            data-type="line"
            title="Biểu đồ đường">

        <svg viewBox="0 0 64 42" fill="none">
            <path d="M3 35H61" stroke="#475569" stroke-width="1"/>
            <path d="M5 29L17 23L27 27L39 10L49 17L59 5"
                  stroke="#38bdf8" stroke-width="3"
                  stroke-linecap="round" stroke-linejoin="round"/>
            <circle cx="59" cy="5" r="3" fill="#38bdf8"/>
        </svg>

        <span>Đường Line</span>
    </button>

    <button type="button"
            class="chart-type-btn ${type === "candle" ? "active" : ""}"
            data-type="candle"
            title="Biểu đồ nến Nhật">

        <svg viewBox="0 0 64 42" fill="none">
            <path d="M13 3V39M32 3V39M51 3V39"
                  stroke="#cbd5e1" stroke-width="1.8"/>
            <rect x="8" y="12" width="10" height="17"
                  rx="1" fill="#22c55e"/>
            <rect x="27" y="8" width="10" height="22"
                  rx="1" fill="#ef4444"/>
            <rect x="46" y="17" width="10" height="14"
                  rx="1" fill="#22c55e"/>
        </svg>

        <span>Nến Nhật</span>
    </button>

    <button type="button"
            class="chart-type-btn ${type === "heikin" ? "active" : ""}"
            data-type="heikin"
            title="Biểu đồ nến Heikin Ashi">

        <svg viewBox="0 0 64 42" fill="none">
            <path d="M13 3V39M32 3V39M51 3V39"
                  stroke="#cbd5e1" stroke-width="1.8"/>
            <rect x="8" y="18" width="10" height="17"
                  rx="1" fill="#22c55e"/>
            <rect x="27" y="5" width="10" height="23"
                  rx="1" fill="#22c55e"/>
            <rect x="46" y="13" width="10" height="20"
                  rx="1" fill="#ef4444"/>
        </svg>

        <span>Heikin Ashi</span>
    </button>

</div>

                <select id="chart-timeframe">
                    ${Object.keys(TIMEFRAMES).map(frame => `
                        <option value="${frame}" ${frame === timeframe ? "selected" : ""}>
                            ${frame}
                        </option>
                    `).join("")}
                </select>
            </div>
        </div>

        <div class="chart-price">
            ${formatPrice(lastPrice, symbol)} ${quoteCurrency}
        </div>

        <svg viewBox="0 0 ${width} ${height}"
             role="img"
             aria-label="Biểu đồ ${symbol}, trục giá bên phải và ngày giờ bên dưới">
            ${chartSvg}
        </svg>

        <p class="chart-message">
            Trục phải: mức giá ${quoteCurrency}. Trục dưới: ngày/tháng và giờ/phút.
            Dữ liệu minh họa, chưa phải dữ liệu thị trường thực tế.
        </p>
    `;

    // Đồng bộ tab thị trường đang được chọn
    document.querySelectorAll("#market-list .market-tab").forEach(tab => {
        const active = tab.dataset.symbol === symbol;
        tab.classList.toggle("selected", active);
        tab.setAttribute("aria-pressed", String(active));
    });

    document.getElementById("chart-symbol").addEventListener("change", event => {
        renderMarketChart(event.target.value, type, timeframe);
    });

    document.querySelectorAll(".chart-type-btn").forEach(button => {
        button.addEventListener("click", () => {
            renderMarketChart(symbol, button.dataset.type, timeframe);
        });
    });

    document.getElementById("chart-timeframe").addEventListener("change", event => {
        renderMarketChart(symbol, type, event.target.value);
    });
}

renderMarketChart();
