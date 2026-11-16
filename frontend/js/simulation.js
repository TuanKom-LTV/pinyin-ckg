(function () {
    "use strict";

    const INITIAL_BALANCE = 10000;
    const STORAGE_KEY = "pinyin_simulation_v1";
    const MARKET_API = "/api/v1/market/symbols";

    let state = {
        balance: INITIAL_BALANCE,
        totalPnl: 0,
        openOrders: [],
        closedOrders: [],
        nextId: 1
    };

    let marketPrices = {};
    let usdJpyRate = null;

    function getElement(id) {
        return document.getElementById(id);
    }

    function loadState() {
        try {
            const saved = localStorage.getItem(STORAGE_KEY);
            if (!saved) return;

            const parsed = JSON.parse(saved);

            if (
                typeof parsed.balance === "number" &&
                Array.isArray(parsed.openOrders) &&
                Array.isArray(parsed.closedOrders)
            ) {
                state = {
                    balance: parsed.balance,
                    totalPnl: Number(parsed.totalPnl) || 0,
                    openOrders: parsed.openOrders,
                    closedOrders: parsed.closedOrders,
                    nextId: Number(parsed.nextId) || 1
                };
            }
        } catch (error) {
            console.error("Không thể đọc dữ liệu mô phỏng:", error);
        }
    }

    function saveState() {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }

    function formatMoney(value) {
        return Number(value).toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }) + " USD";
    }

    function formatPrice(value, symbol) {
        const digits = symbol === "XAUUSD" ? 2 : (symbol === "GBPJPY" ? 3 : 5);
        return Number(value).toFixed(digits);
    }

    function getMultiplier(symbol) {
        if (symbol === "XAUUSD") return 100;

        return 100000;
    }

    function calculatePnl(order, price) {
        const difference = order.direction === "BUY"
            ? price - order.entry
            : order.entry - price;

        const pnlInQuoteCurrency =
            difference * order.volume * getMultiplier(order.symbol);

        // GBPJPY tạo P/L bằng JPY; quy đổi sang USD bằng tick USDJPY từ MT5.
        if (order.symbol === "GBPJPY") {
            if (!Number.isFinite(usdJpyRate) || usdJpyRate <= 0) return null;
            return pnlInQuoteCurrency / usdJpyRate;
        }

        return pnlInQuoteCurrency;
    }

    function showMessage(message, type) {
        const element = getElement("sim-message");
        if (!element) return;

        element.textContent = message;
        element.dataset.type = type || "info";
    }

    async function loadMarketPrices() {
        try {
            const response = await fetch(MARKET_API, { cache: "no-store" });

            if (!response.ok) {
                throw new Error("API giá không phản hồi");
            }

            const result = await response.json();
            const prices = {};

            result.du_lieu.forEach(item => {
                const price = Number(item.gia);

                if (item.ma && Number.isFinite(price) && price > 0) {
                    prices[item.ma] = price;
                }
            });

            marketPrices = prices;

            // USDJPY chỉ dùng nội bộ để quy đổi P/L GBPJPY sang đồng tiền tài khoản USD.
            usdJpyRate = null;
            try {
                const conversionResponse = await fetch(
                    "/api/v1/market/mt5/tick?symbol=USDJPY",
                    { cache: "no-store" }
                );

                if (conversionResponse.ok) {
                    const conversion = await conversionResponse.json();
                    const rate = (Number(conversion.bid) + Number(conversion.ask)) / 2;

                    if (
                        conversion.trang_thai === "co_du_lieu" &&
                        conversion.trang_thai_du_lieu === "moi" &&
                        Number.isFinite(rate) &&
                        rate > 0
                    ) {
                        usdJpyRate = rate;
                    }
                }
            } catch (conversionError) {
                console.warn("Chưa có tỷ giá USDJPY để quy đổi P/L GBPJPY:", conversionError);
            }

            render();
        } catch (error) {
            console.error("Lỗi tải giá mô phỏng:", error);
            showMessage(
                "Không tải được giá minh họa. Vui lòng kiểm tra kết nối máy chủ.",
                "error"
            );
        }
    }

    function getFloatingPnl() {
        return state.openOrders.reduce((total, order) => {
            const currentPrice = marketPrices[order.symbol];

            if (!Number.isFinite(currentPrice)) {
                return total;
            }

            return total + calculatePnl(order, currentPrice);
        }, 0);
    }

    function renderSummary() {
        const floatingPnl = getFloatingPnl();
        const equity = state.balance + floatingPnl;

        getElement("sim-balance").textContent = formatMoney(state.balance);
        getElement("sim-total-pnl").textContent = formatMoney(state.totalPnl);
        getElement("sim-open-count").textContent = state.openOrders.length;
        getElement("sim-equity").textContent = formatMoney(equity);

        const equityNote = getElement("sim-equity-note");
        if (equityNote) {
            const hasGbpJpyOrder = state.openOrders.some(
                order => order.symbol === "GBPJPY"
            );

            equityNote.textContent = !hasGbpJpyOrder
                ? "Số dư và vốn chủ sở hữu được hiển thị bằng USD."
                : (!Number.isFinite(usdJpyRate)
                    ? "Chưa có tick USDJPY mới từ MT5: P/L GBPJPY tạm thời chưa được quy đổi sang USD."
                    : "P/L GBPJPY được quy đổi từ JPY sang USD bằng tỷ giá USDJPY MT5.");
        }
    }

    function renderOpenOrders() {
        const tbody = getElement("sim-open-orders");
        if (!tbody) return;

        if (state.openOrders.length === 0) {
            tbody.innerHTML = "";
            return;
        }

        tbody.innerHTML = state.openOrders.map(order => {
            const currentPrice = marketPrices[order.symbol];
            const hasPrice = Number.isFinite(currentPrice);

            const floatingPnl = hasPrice
                ? calculatePnl(order, currentPrice)
                : null;
            const hasPnl = Number.isFinite(floatingPnl);

            const pnlClass = !hasPnl
                ? ""
                : floatingPnl >= 0 ? "positive" : "negative";

            return `
                <tr>
                    <td>${order.symbol}</td>
                    <td>${order.direction}</td>
                    <td>${Number(order.volume).toFixed(2)}</td>
                    <td>${formatPrice(order.entry, order.symbol)}</td>
                    <td>${hasPrice ? formatPrice(currentPrice, order.symbol) : "--"}</td>
                    <td class="${pnlClass}">
                        ${!hasPnl ? "--" : formatMoney(floatingPnl)}
                    </td>
                    <td>${new Date(order.createdAt).toLocaleString("vi-VN")}</td>
                    <td>
                        <button type="button"
                            class="sim-close-btn"
                            data-id="${order.id}">
                            Đóng lệnh
                        </button>
                    </td>
                </tr>
            `;
        }).join("");
    }

    function renderClosedOrders() {
        const tbody = getElement("sim-closed-orders");
        if (!tbody) return;

        const orders = state.closedOrders;
        const total = orders.length;
        const wins = orders.filter(order => Number(order.pnl) > 0).length;
        const losses = orders.filter(order => Number(order.pnl) < 0).length;
        const zero = orders.filter(order => Number(order.pnl) === 0).length;

        const totalEl = getElement("sim-history-total");
        const winEl = getElement("sim-history-win");
        const lossEl = getElement("sim-history-loss");
        const zeroEl = getElement("sim-history-zero");

        if (totalEl) totalEl.textContent = total;
        if (winEl) winEl.textContent = wins;
        if (lossEl) lossEl.textContent = losses;
        if (zeroEl) zeroEl.textContent = zero;

        if (state.closedOrders.length === 0) {
            tbody.innerHTML =
                '<tr><td colspan="6">Chưa có lịch sử lệnh</td></tr>';
            return;
        }

        tbody.innerHTML = state.closedOrders.map(order => `
            <tr>
                <td>${order.symbol}</td>
                <td>${order.direction}</td>
                <td>${Number(order.volume).toFixed(2)}</td>
                <td>${formatPrice(order.entry, order.symbol)}</td>
                <td>${formatMoney(order.pnl)}</td>
                <td>${new Date(order.closedAt).toLocaleString("vi-VN")}</td>
            </tr>
        `).join("");
    }

    function render() {
        renderSummary();
        renderOpenOrders();
        renderClosedOrders();
    }

    function openOrder() {
        const symbol = getElement("sim-symbol").value;
        const direction = getElement("sim-direction").value;
        const volume = Number(getElement("sim-volume").value);
        const entry = Number(getElement("sim-entry").value);

        if (!Number.isFinite(volume) || volume < 0.01 || volume > 10) {
            showMessage("Khối lượng phải từ 0.01 đến 10.", "error");
            return;
        }

        if (!Number.isFinite(entry) || entry <= 0) {
            showMessage("Vui lòng nhập giá mở lệnh hợp lệ.", "error");
            return;
        }

        const order = {
            id: state.nextId++,
            symbol,
            direction,
            volume,
            entry,
            createdAt: new Date().toISOString()
        };

        state.openOrders.push(order);
        saveState();
        render();

        showMessage("Đã tạo lệnh mô phỏng #" + order.id + ".", "success");
        getElement("sim-entry").value = "";
    }

    function closeOrder(id) {
        const index = state.openOrders.findIndex(order => order.id === id);
        if (index < 0) return;

        const order = state.openOrders[index];

        const currentPrice = marketPrices[order.symbol];
        const defaultPrice = Number.isFinite(currentPrice)
            ? currentPrice
            : order.entry;

        const input = window.prompt(
            "Nhập giá đóng mô phỏng cho " + order.symbol + ":",
            String(defaultPrice)
        );

        if (input === null) return;

        const exitPrice = Number(input);

        if (!Number.isFinite(exitPrice) || exitPrice <= 0) {
            showMessage("Giá đóng không hợp lệ. Lệnh vẫn đang mở.", "error");
            return;
        }

        const pnl = calculatePnl(order, exitPrice);

        if (!Number.isFinite(pnl)) {
            showMessage(
                "Chưa có tỷ giá USDJPY mới từ MT5. Không thể quy đổi P/L GBPJPY sang USD; lệnh mô phỏng vẫn được giữ.",
                "error"
            );
            return;
        }

        order.exit = exitPrice;
        order.pnl = pnl;
        order.closedAt = new Date().toISOString();

        state.balance += pnl;
        state.totalPnl += pnl;
        state.openOrders.splice(index, 1);
        state.closedOrders.unshift(order);

        saveState();
        render();

        showMessage(
            "Đã đóng lệnh #" + order.id +
            ". Kết quả mô phỏng: " + formatMoney(pnl),
            "success"
        );
    }

    function initialize() {
        if (!getElement("sim-direction")) return;

        loadState();
        render();
        function activateSimulationTab(tabName) {
            document.querySelectorAll(".sim-tab-btn").forEach(button => {
                const isActive = button.dataset.simTab === tabName;
                button.classList.toggle("active", isActive);
                button.setAttribute("aria-selected", String(isActive));
            });

            document.querySelectorAll(".sim-tab-panel").forEach(panel => {
                panel.hidden = panel.id !== "sim-tab-" + tabName;
            });
        }

        document.querySelectorAll(".sim-tab-btn").forEach(button => {
            button.addEventListener("click", () => {
                activateSimulationTab(button.dataset.simTab);
            });
        });

        activateSimulationTab("open");


        document.querySelectorAll(".sim-direction-btn").forEach(button => {
            button.addEventListener("click", () => {
                const direction = button.dataset.direction;
                getElement("sim-direction").value = direction;

                document.querySelectorAll(".sim-direction-btn").forEach(item => {
                    item.classList.toggle("active", item === button);
                });

                openOrder();

            });
        });

        getElement("sim-open-orders").addEventListener("click", event => {
            const button = event.target.closest(".sim-close-btn");
            if (!button) return;

            closeOrder(Number(button.dataset.id));
        });

        loadMarketPrices();
        window.setInterval(loadMarketPrices, 3000);

        showMessage(
            "Đã kết nối API giá minh họa. Không phải dữ liệu thị trường trực tiếp.",
            "info"
        );
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialize);
    } else {
        initialize();
    }
})();








