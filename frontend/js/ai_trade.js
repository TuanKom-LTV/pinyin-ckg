// Phân tích Claude (chỉ gợi ý) + giao dịch MT5 Demo có xác nhận của người dùng.
(function () {
    const $ = (id) => document.getElementById(id);
    const DIGITS = { XAUUSD: 2, EURUSD: 5, GBPUSD: 5, GBPJPY: 3 };
    const fmtPrice = (sym, v) =>
        v == null || !Number(v) ? "--" : Number(v).toFixed(DIGITS[sym] ?? 5);
    const fmtMoney = (v, cur) =>
        v == null ? "--" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + (cur ? " " + cur : "");

    async function api(url, body) {
        const res = await fetch(url, body
            ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store" }
            : { cache: "no-store" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
    }

    function showMsg(el, text, isError) {
        if (!el) return;
        el.textContent = text || "";
        el.classList.toggle("trade-msg-error", !!isError);
        el.classList.toggle("trade-msg-ok", !!text && !isError);
    }

    async function sendOrder(order) {
        return api("/api/v1/trade/order", order);
    }

    // ---------------- Phân tích Claude ----------------

    const SIGNAL_TEXT = { MUA: "MUA (BUY)", BAN: "BÁN (SELL)", CHO: "CHỜ" };
    const TREND_TEXT = { TANG: "Tăng", GIAM: "Giảm", DI_NGANG: "Đi ngang" };
    let lastAi = null;

    async function refreshAiStatus() {
        try {
            const s = await api("/api/v1/ai/status");
            const ok = s.trang_thai === "san_sang";
            if ($("ai-status")) $("ai-status").textContent = `${s.nhan} · ${s.mo_hinh}. ${s.ghi_chu}`;
            if ($("ai-provider-name")) $("ai-provider-name").textContent = s.ten_nguon ? `(${s.ten_nguon})` : "";
            if ($("ai-run-btn")) $("ai-run-btn").disabled = s.trang_thai === "chua_cau_hinh";
            const sys = $("sys-ai");
            if (sys) {
                sys.textContent = ok ? `${s.ten_nguon} · ${s.mo_hinh}` : s.nhan;
                sys.className = ok ? "conn-ok" : (s.trang_thai === "loi" ? "conn-error" : "conn-warn");
            }
            if ($("overview-ai")) $("overview-ai").textContent = ok ? "Đã kết nối" : s.nhan;
            if ($("overview-ai-note")) $("overview-ai-note").textContent = ok ? `${s.ten_nguon} · ${s.mo_hinh}` : s.ghi_chu;
        } catch (e) {
            if ($("sys-ai")) { $("sys-ai").textContent = "Không kiểm tra được"; $("sys-ai").className = "conn-error"; }
        }
    }

    async function runAi() {
        const btn = $("ai-run-btn");
        const symbol = $("analysis-symbol")?.value || "XAUUSD";
        const timeframe = $("analysis-timeframe")?.value || "M15";
        const err = $("ai-error");

        btn.disabled = true;
        btn.textContent = "AI đang phân tích...";
        err.hidden = true;
        $("ai-confirm").hidden = true;
        showMsg($("ai-trade-msg"), "");

        try {
            const r = await api("/api/v1/ai/analyze", { symbol, timeframe, lam_moi: true });
            if (!r.ok) throw new Error(r.loi);
            lastAi = r;
            const a = r.phan_tich;

            $("ai-result").hidden = false;
            const sig = $("ai-signal");
            sig.textContent = SIGNAL_TEXT[a.tin_hieu] || a.tin_hieu;
            sig.className = a.tin_hieu === "MUA" ? "positive" : a.tin_hieu === "BAN" ? "negative" : "";
            $("ai-trend").textContent = TREND_TEXT[a.xu_huong] || a.xu_huong;
            $("ai-confidence").textContent = a.do_tin_cay + "%";
            $("ai-sl").textContent = fmtPrice(r.ma, a.cat_lo);
            $("ai-tp").textContent = fmtPrice(r.ma, a.chot_loi);
            $("ai-reason").textContent = a.ly_do;
            $("ai-risk").textContent = "Rủi ro: " + a.rui_ro;
            $("ai-meta").textContent =
                `${r.ma} ${r.khung_thoi_gian} · Bid ${fmtPrice(r.ma, r.bid)} / Ask ${fmtPrice(r.ma, r.ask)} · ` +
                `${a.mo_hinh} · ${new Date(r.thoi_gian).toLocaleTimeString("vi-VN")}` +
                (r.du_lieu_cu ? " · Dữ liệu MT5 đã cũ (thị trường đóng?)" : "");

            $("ai-trade-btn").hidden = !(a.tin_hieu === "MUA" || a.tin_hieu === "BAN") || r.du_lieu_cu;
        } catch (e) {
            err.textContent = "Không phân tích được: " + e.message;
            err.hidden = false;
        } finally {
            btn.disabled = false;
            btn.textContent = "Phân tích bằng AI";
            refreshAiStatus();
        }
    }

    function openAiConfirm() {
        if (!lastAi) return;
        const a = lastAi.phan_tich;
        const side = a.tin_hieu === "MUA" ? "BUY" : "SELL";
        $("ai-confirm-text").textContent =
            `Gửi lệnh ${side} ${lastAi.ma} sang MT5 với SL ${fmtPrice(lastAi.ma, a.cat_lo)} ` +
            `và TP ${fmtPrice(lastAi.ma, a.chot_loi)}? Giá khớp là giá thị trường lúc gửi.`;
        $("ai-confirm").hidden = false;
    }

    async function confirmAiOrder() {
        const a = lastAi.phan_tich;
        const yes = $("ai-confirm-yes");
        yes.disabled = true;
        try {
            const r = await sendOrder({
                symbol: lastAi.ma,
                side: a.tin_hieu === "MUA" ? "BUY" : "SELL",
                lot: Number($("ai-lot").value),
                sl: a.cat_lo || null,
                tp: a.chot_loi || null,
                nguon: "ai"
            });
            if (!r.ok) throw new Error(r.loi);
            showMsg($("ai-trade-msg"), `Đã khớp ${r.side} ${r.lot} ${r.symbol} @ ${r.gia} (ticket ${r.ticket}).`);
            $("ai-confirm").hidden = true;
            refreshAccount();
        } catch (e) {
            showMsg($("ai-trade-msg"), "MT5 không nhận lệnh: " + e.message, true);
        } finally {
            yes.disabled = false;
        }
    }

    // ---------------- Tài khoản & lệnh MT5 ----------------

    let pendingOrder = null;

    async function refreshAccount() {
        try {
            const r = await api("/api/v1/trade/account");
            const sys = $("sys-trade");

            if (!r.ok) {
                $("mt5-acc-status").textContent = r.loi;
                $("mt5-acc-badge").textContent = "LỖI";
                $("mt5-acc-badge").className = "acc-badge acc-error";
                if (sys) { sys.textContent = "Không đọc được tài khoản"; sys.className = "conn-error"; }
                return;
            }

            const cur = r.tien_te;
            $("mt5-acc-server").textContent = r.may_chu;
            $("mt5-acc-balance").textContent = fmtMoney(r.so_du, cur);
            $("mt5-acc-equity").textContent = fmtMoney(r.von, cur);
            $("mt5-acc-free").textContent = fmtMoney(r.ky_quy_tu_do, cur);
            $("mt5-acc-badge").textContent = r.demo ? "DEMO" : "TIỀN THẬT";
            $("mt5-acc-badge").className = "acc-badge " + (r.demo ? "acc-demo" : "acc-real");
            $("mt5-acc-status").textContent = r.duoc_giao_dich
                ? `Sẵn sàng gửi lệnh · lot tối đa ${r.lot_toi_da}`
                : "Đang chặn lệnh: " + r.ly_do_chan;
            $("mt5-order-btn").disabled = !r.duoc_giao_dich;
            if ($("ai-confirm-yes")) $("ai-confirm-yes").disabled = !r.duoc_giao_dich;

            if (sys) {
                sys.textContent = r.duoc_giao_dich ? `Sẵn sàng (${r.demo ? "Demo" : "Tiền thật"})` : "Đang chặn lệnh";
                sys.className = r.duoc_giao_dich ? "conn-ok" : "conn-warn";
            }

            renderPositions(r.lenh_dang_mo, cur);
        } catch (e) {
            $("mt5-acc-status").textContent = "Không kết nối được API giao dịch.";
        }
    }

    function renderPositions(list, cur) {
        const body = $("mt5-positions-body");
        body.textContent = "";
        if (!list.length) {
            const tr = body.insertRow();
            const td = tr.insertCell();
            td.colSpan = 10;
            td.textContent = "Chưa có lệnh PinYin nào đang mở";
            return;
        }
        for (const p of list) {
            const tr = body.insertRow();
            const cells = [
                p.ticket, p.symbol, p.side, p.lot,
                fmtPrice(p.symbol, p.gia_vao), fmtPrice(p.symbol, p.gia_hien_tai),
                fmtPrice(p.symbol, p.sl), fmtPrice(p.symbol, p.tp), fmtMoney(p.lai_lo, cur)
            ];
            cells.forEach((v, i) => {
                const td = tr.insertCell();
                td.textContent = v;
                if (i === 2) td.className = p.side === "BUY" ? "positive" : "negative";
                if (i === 8) td.className = p.lai_lo >= 0 ? "positive" : "negative";
            });
            const td = tr.insertCell();
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "ghost small";
            btn.textContent = "Đóng";
            btn.addEventListener("click", () => closePosition(p.ticket, btn));
            td.appendChild(btn);
        }
    }

    async function closePosition(ticket, btn) {
        if (btn.dataset.armed !== "1") {
            btn.dataset.armed = "1";
            btn.textContent = "Chắc chắn?";
            btn.classList.add("danger");
            setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "Đóng"; btn.classList.remove("danger"); }, 4000);
            return;
        }
        btn.disabled = true;
        try {
            const r = await api("/api/v1/trade/close", { ticket });
            if (!r.ok) throw new Error(r.loi);
            showMsg($("mt5-trade-msg"), `Đã đóng lệnh ${ticket} @ ${r.gia}.`);
        } catch (e) {
            showMsg($("mt5-trade-msg"), "Không đóng được lệnh: " + e.message, true);
        }
        refreshAccount();
    }

    function openManualConfirm() {
        const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };
        pendingOrder = {
            symbol: $("mt5-symbol").value,
            side: $("mt5-side").value,
            lot: Number($("mt5-lot").value),
            sl: num("mt5-sl"),
            tp: num("mt5-tp"),
            nguon: "tay"
        };
        const o = pendingOrder;
        $("mt5-confirm-text").textContent =
            `Gửi lệnh ${o.side} ${o.lot} lot ${o.symbol}` +
            (o.sl ? ` · SL ${o.sl}` : "") + (o.tp ? ` · TP ${o.tp}` : "") +
            " sang MT5 theo giá thị trường?";
        $("mt5-confirm").hidden = false;
        showMsg($("mt5-trade-msg"), "");
    }

    async function confirmManualOrder() {
        if (!pendingOrder) return;
        const yes = $("mt5-confirm-yes");
        yes.disabled = true;
        try {
            const r = await sendOrder(pendingOrder);
            if (!r.ok) throw new Error(r.loi);
            showMsg($("mt5-trade-msg"), `Đã khớp ${r.side} ${r.lot} ${r.symbol} @ ${r.gia} (ticket ${r.ticket}).`);
            $("mt5-confirm").hidden = true;
            pendingOrder = null;
        } catch (e) {
            showMsg($("mt5-trade-msg"), "MT5 không nhận lệnh: " + e.message, true);
        } finally {
            yes.disabled = false;
            refreshAccount();
        }
    }

    // ---------------- Khởi tạo ----------------

    $("ai-run-btn")?.addEventListener("click", runAi);
    $("ai-trade-btn")?.addEventListener("click", openAiConfirm);
    $("ai-confirm-yes")?.addEventListener("click", confirmAiOrder);
    $("ai-confirm-no")?.addEventListener("click", () => { $("ai-confirm").hidden = true; });
    $("mt5-order-btn")?.addEventListener("click", openManualConfirm);
    $("mt5-confirm-yes")?.addEventListener("click", confirmManualOrder);
    $("mt5-confirm-no")?.addEventListener("click", () => { $("mt5-confirm").hidden = true; pendingOrder = null; });

    refreshAiStatus();
    refreshAccount();
    setInterval(refreshAccount, 5000);
    setInterval(refreshAiStatus, 30000);
})();
