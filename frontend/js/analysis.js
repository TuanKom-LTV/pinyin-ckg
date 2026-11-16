function calculateEMA(prices, period) {
    if (!Array.isArray(prices) || prices.length < period) return null;

    const multiplier = 2 / (period + 1);

    let ema = prices
        .slice(0, period)
        .reduce((sum, value) => sum + value, 0) / period;

    for (let i = period; i < prices.length; i++) {
        ema = (prices[i] - ema) * multiplier + ema;
    }

    return ema;
}

function buildAnalysisCandles(prices, timeframe, symbol) {
    const candles = buildOHLC(prices);
    const interval = TIMEFRAMES[timeframe] || TIMEFRAMES.M1;
    const now = Date.now();

    return candles.map((candle, index) => {
        const volumeBase = symbol === "XAUUSD" ? 500 : 100000;

        const volume = volumeBase +
            ((index * 7919) % Math.max(1, Math.floor(volumeBase * 0.35)));

        return {
            ...candle,
            time: new Date(now - interval * (candles.length - 1 - index)),
            volume
        };
    });
}

function calculateATR(candles, period = 14) {
    if (!Array.isArray(candles) || candles.length <= period) return null;

    const trueRanges = [];

    for (let i = 0; i < candles.length; i++) {
        const current = candles[i];

        if (i === 0) {
            trueRanges.push(current.high - current.low);
            continue;
        }

        const previousClose = candles[i - 1].close;

        const trueRange = Math.max(
            current.high - current.low,
            Math.abs(current.high - previousClose),
            Math.abs(current.low - previousClose)
        );

        trueRanges.push(trueRange);
    }

    let atr = trueRanges
        .slice(0, period)
        .reduce((sum, value) => sum + value, 0) / period;

    for (let i = period; i < trueRanges.length; i++) {
        atr = (atr * (period - 1) + trueRanges[i]) / period;
    }

    return atr;
}

function calculateDailyVWAP(candles) {
    if (!Array.isArray(candles) || candles.length === 0) return null;

    let currentDay = null;
    let cumulativePriceVolume = 0;
    let cumulativeVolume = 0;
    let latestVWAP = null;

    for (const candle of candles) {
        const date = candle.time;
        const dayKey = [
            date.getFullYear(),
            date.getMonth(),
            date.getDate()
        ].join("-");

        if (dayKey !== currentDay) {
            currentDay = dayKey;
            cumulativePriceVolume = 0;
            cumulativeVolume = 0;
        }

        const typicalPrice = (
            candle.high + candle.low + candle.close
        ) / 3;

        cumulativePriceVolume += typicalPrice * candle.volume;
        cumulativeVolume += candle.volume;

        if (cumulativeVolume > 0) {
            latestVWAP = cumulativePriceVolume / cumulativeVolume;
        }
    }

    return latestVWAP;
}

function formatIndicator(value, symbol) {
    if (value === null || !Number.isFinite(value)) return "--";

    const digits = symbol === "XAUUSD" ? 2 : (symbol === "GBPJPY" ? 3 : 5);

    return value.toLocaleString("en-US", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
    });
}

function runMarketAnalysis() {
    const symbol = document.getElementById("analysis-symbol")?.value || "XAUUSD";
    const timeframe = document.getElementById("analysis-timeframe")?.value || "M1";

    const reasonElement = document.getElementById("analysis-reason");

    if (
        typeof chartData === "undefined" ||
        typeof buildOHLC !== "function" ||
        typeof TIMEFRAMES === "undefined" ||
        !chartData[symbol]
    ) {
        if (reasonElement) {
            reasonElement.textContent = "Không tìm thấy dữ liệu mô phỏng cần thiết.";
        }
        return;
    }

    const prices = chartData[symbol].prices;
    const candles = buildAnalysisCandles(prices, timeframe, symbol);

    const ema9 = calculateEMA(prices, 9);
    const ema21 = calculateEMA(prices, 21);
    const atr14 = calculateATR(candles, 14);
    const dailyVWAP = calculateDailyVWAP(candles);
    const latestPrice = prices[prices.length - 1];

    let trend = "Đi ngang";
    let signal = "WAIT";
    let reason = "Các điều kiện xu hướng chưa đồng thuận.";

    if (ema9 !== null && ema21 !== null && dailyVWAP !== null) {
        if (latestPrice > dailyVWAP && ema9 > ema21) {
            trend = "Tăng";
            signal = "BUY";
            reason = "Giá nằm trên Daily VWAP và EMA9 nằm trên EMA21.";
        } else if (latestPrice < dailyVWAP && ema9 < ema21) {
            trend = "Giảm";
            signal = "SELL";
            reason = "Giá nằm dưới Daily VWAP và EMA9 nằm dưới EMA21.";
        }
    }

    const isGold = symbol === "XAUUSD";
    const isStorm = isGold && atr14 !== null && atr14 >= 3;

    let volatilityStatus;
    let volatilityDetail;

    if (atr14 === null) {
        volatilityStatus = "Chưa đủ dữ liệu";
        volatilityDetail = "Cần thêm dữ liệu nến";
    } else if (isGold && isStorm) {
        volatilityStatus = "BÃO GIÁ";
        volatilityDetail = "ATR XAUUSD ≥ 3.00";
    } else if (isGold) {
        volatilityStatus = "Bình thường";
        volatilityDetail = "ATR XAUUSD < 3.00";
    } else {
        volatilityStatus = "Đang theo dõi";
        volatilityDetail = "Chưa thiết lập ngưỡng cho mã này";
    }

    const trendElement = document.getElementById("analysis-trend");
    const trendDetailElement = document.getElementById("analysis-trend-detail");
    const signalElement = document.getElementById("analysis-signal");
    const signalDetailElement = document.getElementById("analysis-signal-detail");
    const strengthElement = document.getElementById("analysis-strength");
    const strengthDetailElement = document.getElementById("analysis-strength-detail");

    if (trendElement) trendElement.textContent = trend;
    if (trendDetailElement) trendDetailElement.textContent = `${symbol} - ${timeframe}`;

    if (signalElement) signalElement.textContent = signal;
    if (signalDetailElement) {
        signalDetailElement.textContent = "Kết quả theo quy tắc mô phỏng";
    }

    if (strengthElement) strengthElement.textContent = volatilityStatus;
    if (strengthDetailElement) strengthDetailElement.textContent = volatilityDetail;

    const ema9Element = document.getElementById("analysis-ema9");
    const ema21Element = document.getElementById("analysis-ema21");
    const atrElement = document.getElementById("analysis-atr");
    const vwapElement = document.getElementById("analysis-vwap");

    if (ema9Element) ema9Element.textContent = formatIndicator(ema9, symbol);
    if (ema21Element) ema21Element.textContent = formatIndicator(ema21, symbol);
    if (atrElement) atrElement.textContent = formatIndicator(atr14, symbol);
    if (vwapElement) vwapElement.textContent = formatIndicator(dailyVWAP, symbol);

    if (reasonElement) {
        let message = reason;

        if (isGold && isStorm) {
            message += " Cảnh báo bão giá: cần thận trọng, giảm khối lượng trong mô phỏng và tính lại khoảng dừng lỗ theo biến động.";
        } else if (isGold && atr14 !== null) {
            message += " ATR XAUUSD hiện dưới ngưỡng cảnh báo 3.00.";
        } else if (!isGold) {
            message += " Mã này chưa áp dụng ngưỡng cảnh báo ATR 3.00.";
        }

        message += " Dữ liệu OHLC và volume hiện là dữ liệu mô phỏng.";

        reasonElement.textContent = message;
    }
}

// Sự kiện được đăng ký chung ở cuối file
// ===== PHAN TICH CHI TIET 4 CHI BAO =====

function updateInsightStatus(id, text, type = "") {
    const element = document.getElementById(id);
    if (!element) return;

    element.textContent = text;
    element.classList.remove("positive", "negative", "warning");

    if (type) {
        element.classList.add(type);
    }
}

function updateInsightText(id, text) {
    const element = document.getElementById(id);
    if (element) element.textContent = text;
}

function updateIndicatorInsight() {
    const symbol = document.getElementById("analysis-symbol")?.value || "XAUUSD";
    const timeframe = document.getElementById("analysis-timeframe")?.value || "M1";

    if (
        typeof chartData === "undefined" ||
        !chartData[symbol] ||
        typeof buildOHLC !== "function"
    ) return;

    const prices = chartData[symbol].prices;
    const candles = buildAnalysisCandles(prices, timeframe, symbol);

    const latestPrice = prices[prices.length - 1];

    const ema9 = calculateEMA(prices, 9);
    const ema21 = calculateEMA(prices, 21);

    const previousEma9 = calculateEMA(prices.slice(0, -1), 9);
    const previousEma21 = calculateEMA(prices.slice(0, -1), 21);

    const atr14 = calculateATR(candles, 14);
    const dailyVWAP = calculateDailyVWAP(candles);

    const priceText = formatIndicator(latestPrice, symbol);

    const ema9Text = formatIndicator(ema9, symbol);
    const ema21Text = formatIndicator(ema21, symbol);
    const atrText = formatIndicator(atr14, symbol);
    const vwapText = formatIndicator(dailyVWAP, symbol);

    // ===== EMA 9 =====

    updateInsightText("analysis-ema9", ema9Text);
    updateInsightText("ema9-price", priceText);

    if (ema9 !== null) {
        const above = latestPrice > ema9;
        const below = latestPrice < ema9;

        const position = above ? "Trên EMA 9" :
            below ? "Dưới EMA 9" : "Sát EMA 9";

        const slope = previousEma9 === null ? "Chưa đủ dữ liệu" :
            ema9 > previousEma9 ? "Đang hướng lên" :
            ema9 < previousEma9 ? "Đang hướng xuống" : "Đi ngang";

        updateInsightText("ema9-position", position);
        updateInsightText("ema9-slope", slope);

        if (above) {
            updateInsightStatus("ema9-status", "Giá trên EMA 9", "positive");
            updateInsightText(
                "ema9-analysis",
                "Giá đang nằm trên EMA 9. Xu hướng ngắn hạn đang nghiêng về phía tăng. Trong mô phỏng, theo dõi khả năng giá hồi về kiểm tra EMA 9 và phản ứng của nến tại khu vực này."
            );
        } else if (below) {
            updateInsightStatus("ema9-status", "Giá dưới EMA 9", "negative");
            updateInsightText(
                "ema9-analysis",
                "Giá đang nằm dưới EMA 9. Xu hướng ngắn hạn đang nghiêng về phía giảm. Trong mô phỏng, theo dõi khả năng giá hồi lên kiểm tra EMA 9 và phản ứng của nến tại khu vực này."
            );
        } else {
            updateInsightStatus("ema9-status", "Giá sát EMA 9", "warning");
            updateInsightText(
                "ema9-analysis",
                "Giá đang ở gần EMA 9. Chưa có khoảng cách rõ ràng để xác định trạng thái. Cần quan sát thêm diễn biến giá."
            );
        }
    }

    // ===== EMA 21 =====

    updateInsightText("analysis-ema21", ema21Text);
    updateInsightText("ema21-price", priceText);

    if (ema21 !== null) {
        const above = latestPrice > ema21;
        const below = latestPrice < ema21;

        const position = above ? "Trên EMA 21" :
            below ? "Dưới EMA 21" : "Sát EMA 21";

        const slope = previousEma21 === null ? "Chưa đủ dữ liệu" :
            ema21 > previousEma21 ? "Đang hướng lên" :
            ema21 < previousEma21 ? "Đang hướng xuống" : "Đi ngang";

        updateInsightText("ema21-position", position);
        updateInsightText("ema21-slope", slope);

        if (above) {
            updateInsightStatus("ema21-status", "Giá trên EMA 21", "positive");
            updateInsightText(
                "ema21-analysis",
                "Giá đang nằm trên EMA 21. Cấu trúc trung hạn đang nghiêng về phía tăng. Theo dõi độ dốc EMA 21 và khả năng giá duy trì phía trên đường trung bình."
            );
        } else if (below) {
            updateInsightStatus("ema21-status", "Giá dưới EMA 21", "negative");
            updateInsightText(
                "ema21-analysis",
                "Giá đang nằm dưới EMA 21. Cấu trúc trung hạn đang nghiêng về phía giảm. Theo dõi độ dốc EMA 21 và khả năng giá duy trì phía dưới đường trung bình."
            );
        } else {
            updateInsightStatus("ema21-status", "Giá sát EMA 21", "warning");
            updateInsightText(
                "ema21-analysis",
                "Giá đang ở gần EMA 21. Xu hướng trung hạn chưa thể hiện sự phân tách rõ ràng."
            );
        }
    }

    // ===== ATR 14 =====

    updateInsightText("analysis-atr", atrText);
    updateInsightText("atr-symbol", symbol);

    if (atr14 !== null) {
        const isGold = symbol === "XAUUSD";
        const isStorm = isGold && atr14 >= 3;

        if (isGold) {
            updateInsightText(
                "atr-volatility",
                isStorm ? "Bão giá" : "Bình thường"
            );

            if (isStorm) {
                updateInsightStatus("atr-status", "Biến động cao", "warning");
                updateInsightText(
                    "atr-analysis",
                    "ATR XAUUSD đạt hoặc vượt 3.00. Đây là trạng thái biến động cao theo ngưỡng mô phỏng đã thiết lập. Cần thận trọng khi đánh giá dữ liệu, đồng thời kiểm tra lại giả định khối lượng và khoảng dừng lỗ trong môi trường mô phỏng."
                );
            } else {
                updateInsightStatus("atr-status", "Bình thường", "positive");
                updateInsightText(
                    "atr-analysis",
                    "ATR XAUUSD đang dưới ngưỡng 3.00. Biên độ biến động chưa chạm ngưỡng cảnh báo của hệ thống mô phỏng."
                );
            }
        } else {
            updateInsightText("atr-volatility", "Chưa có ngưỡng");
            updateInsightStatus("atr-status", "Đang theo dõi", "");
            updateInsightText(
                "atr-analysis",
                "ATR phản ánh biên độ biến động của mã này. Hệ thống hiện chỉ hiển thị giá trị, chưa áp dụng ngưỡng phân loại biến động cho mã giao dịch này."
            );
        }
    }

    // ===== DAILY VWAP =====

    updateInsightText("analysis-vwap", vwapText);
    updateInsightText("vwap-price", priceText);

    if (dailyVWAP !== null) {
        const above = latestPrice > dailyVWAP;
        const below = latestPrice < dailyVWAP;

        const position = above ? "Trên VWAP" :
            below ? "Dưới VWAP" : "Sát VWAP";

        const distance = latestPrice - dailyVWAP;
        const distancePercent = dailyVWAP !== 0
            ? (distance / dailyVWAP) * 100
            : 0;

        updateInsightText("vwap-position", position);

        updateInsightText(
            "vwap-distance",
            `${formatIndicator(Math.abs(distance), symbol)} (${Math.abs(distancePercent).toFixed(3)}%)`
        );

        if (above) {
            updateInsightStatus("vwap-status", "Giá trên VWAP", "positive");
            updateInsightText(
                "vwap-analysis",
                "Giá đang nằm trên Daily VWAP. Trong mô phỏng, trạng thái này cho thấy giá hiện cao hơn mức giá bình quân gia quyền theo khối lượng của dữ liệu trong ngày. Theo dõi khả năng giá quay lại kiểm tra VWAP."
            );
        } else if (below) {
            updateInsightStatus("vwap-status", "Giá dưới VWAP", "negative");
            updateInsightText(
                "vwap-analysis",
                "Giá đang nằm dưới Daily VWAP. Trong mô phỏng, trạng thái này cho thấy giá hiện thấp hơn mức giá bình quân gia quyền theo khối lượng của dữ liệu trong ngày. Theo dõi khả năng giá hồi lên kiểm tra VWAP."
            );
        } else {
            updateInsightStatus("vwap-status", "Giá sát VWAP", "warning");
            updateInsightText(
                "vwap-analysis",
                "Giá đang ở gần Daily VWAP. Chưa có sự chênh lệch rõ ràng giữa giá hiện tại và giá bình quân gia quyền."
            );
        }
    }

    // Thong tin nguon du lieu
    updateInsightText(
        "ema9-slope",
        document.getElementById("ema9-slope")?.textContent || "--"
    );
}

// Chạy sau khi hàm phân tích cũ cập nhật kết quả



// ===== LUONG PHAN TICH THONG NHAT =====
document.getElementById("run-analysis-btn")?.addEventListener(
    "click",
    () => {
        runMarketAnalysis();
        updateIndicatorInsight();
    }
);
