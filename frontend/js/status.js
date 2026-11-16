// Trạng thái kết nối hệ thống: backend + nguồn dữ liệu thị trường MT5 (chỉ đọc).
(function () {
    const STATUS_CLASS = {
        da_ket_noi: "conn-ok",
        da_ket_noi_thi_truong_dong: "conn-closed",
        chua_co_gia: "conn-warn",
        chua_ket_noi_broker: "conn-warn",
        mat_ket_noi: "conn-error"
    };

    const SOURCE_TEXT = {
        da_ket_noi: "Dữ liệu MT5 trực tiếp",
        da_ket_noi_thi_truong_dong: "Dữ liệu MT5 · Thị trường đóng cửa",
        chua_co_gia: "MT5 chưa có giá",
        chua_ket_noi_broker: "MT5 chưa kết nối broker",
        mat_ket_noi: "Dữ liệu minh họa (mất kết nối MT5)"
    };

    function setText(id, text, cls) {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = text;
        if (cls !== undefined) el.className = cls;
    }

    function setDot(cls, text) {
        const dot = document.getElementById("sidebar-status-dot");
        if (dot) dot.className = "status-dot " + cls;
        setText("sidebar-status-text", text);
    }

    async function checkBackend() {
        try {
            const res = await fetch("/api/v1/health", { cache: "no-store" });
            if (!res.ok) throw new Error("HTTP " + res.status);
            setText("sys-backend", "Hoạt động", "conn-ok");
            return true;
        } catch (error) {
            setText("sys-backend", "Không phản hồi", "conn-error");
            return false;
        }
    }

    async function checkMarket() {
        const res = await fetch("/api/v1/market/status", { cache: "no-store" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
    }

    let busy = false;

    async function refresh() {
        if (busy) return;
        busy = true;

        try {
            const backendOk = await checkBackend();

            if (!backendOk) {
                setText("sys-market", "Không xác định", "conn-error");
                setText("sys-market-note", "Không kết nối được backend PinYin.");
                setText("market-list-source", "Mất kết nối backend", "conn-error");
                setText("footer-data-source", "Mất kết nối backend");
                setDot("conn-error", "Mất kết nối backend");
                return;
            }

            const data = await checkMarket();
            const cls = STATUS_CLASS[data.trang_thai] || "conn-warn";
            const source = SOURCE_TEXT[data.trang_thai] || data.nhan;

            setText("sys-market", data.nhan, cls);

            const server = data.may_chu
                ? data.may_chu + (data.ping_ms != null ? ` · ${data.ping_ms} ms` : "")
                : "--";
            setText("sys-mt5-server", server);

            setText(
                "sys-mt5-symbols",
                data.trang_thai === "mat_ket_noi"
                    ? "--"
                    : `${data.so_ma_co_gia}/${data.tong_so_ma}` +
                      (data.so_ma_gia_moi ? ` (${data.so_ma_gia_moi} giá mới)` : "")
            );

            const checkedAt = new Date(data.kiem_tra_luc).toLocaleTimeString("vi-VN");
            setText("sys-market-note", `${data.ghi_chu} Kiểm tra lúc ${checkedAt}.`);

            setText("market-list-source", source, cls);
            setText("footer-data-source", source);

            setDot(
                data.trang_thai === "mat_ket_noi" ? "conn-error" : cls,
                data.trang_thai === "mat_ket_noi" ? "Mất kết nối MT5" : "Hệ thống sẵn sàng"
            );
        } catch (error) {
            console.error("Lỗi kiểm tra trạng thái kết nối:", error);
            setText("sys-market", "Lỗi kiểm tra", "conn-error");
            setText("sys-market-note", "Không đọc được API trạng thái thị trường.");
        } finally {
            busy = false;
        }
    }

    refresh();
    setInterval(refresh, 10000);
})();
