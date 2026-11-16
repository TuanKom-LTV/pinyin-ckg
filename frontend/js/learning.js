(function () {
    "use strict";

    const STORAGE_KEY = "pinyin_learning_v1";

    const lessons = {
        basics: {
            title: "Kiến thức nền tảng",
            content: `
                <h4>1. Forex là gì?</h4>
                <p>Forex là thị trường trao đổi ngoại tệ. Các cặp tiền như EUR/USD thể hiện giá trị tương đối giữa hai đồng tiền.</p>
                <h4>2. Vàng XAUUSD</h4>
                <p>XAUUSD là ký hiệu thường dùng cho vàng được định giá bằng đô la Mỹ. Giá có thể biến động theo cung cầu và nhiều yếu tố kinh tế.</p>
                <h4>3. Bid và Ask</h4>
                <p>Bid là giá mua vào của thị trường từ góc nhìn người bán; Ask là giá bán ra từ góc nhìn người mua. Chênh lệch giữa hai mức giá gọi là Spread.</p>
                <h4>4. Cách đọc mã giao dịch</h4>
                <p>Mỗi mã đại diện cho một sản phẩm hoặc cặp tài sản. Cần tìm hiểu quy ước giá và đơn vị của từng sản phẩm trước khi phân tích.</p>
            `
        },
        technical: {
            title: "Phân tích kỹ thuật",
            content: `
                <h4>1. Nến Nhật</h4>
                <p>Một cây nến thể hiện giá mở cửa, đóng cửa, cao nhất và thấp nhất trong một khoảng thời gian.</p>
                <h4>2. Xu hướng</h4>
                <p>Xu hướng tăng thường có các đỉnh và đáy cao dần. Xu hướng giảm thường có các đỉnh và đáy thấp dần. Thị trường đi ngang có biên độ dao động tương đối ổn định.</p>
                <h4>3. EMA</h4>
                <p>EMA là đường trung bình động hàm mũ, đặt trọng số lớn hơn cho dữ liệu giá gần đây. EMA giúp quan sát diễn biến giá đã xảy ra, không dự đoán chắc chắn tương lai.</p>
                <h4>4. VWAP</h4>
                <p>VWAP là giá trung bình có trọng số theo khối lượng. Chỉ báo này thường được dùng để tham khảo vị trí giá so với mức trung bình trong phiên.</p>
                <h4>5. ATR</h4>
                <p>ATR đo lường mức độ biến động, không cho biết thị trường sẽ tăng hay giảm.</p>
            `
        },
        risk: {
            title: "Quản trị rủi ro",
            content: `
                <h4>1. Vốn và rủi ro</h4>
                <p>Quản trị rủi ro là việc hiểu rõ khả năng tổn thất và giới hạn mức độ ảnh hưởng của một quyết định tài chính.</p>
                <h4>2. Tỷ lệ lợi nhuận/rủi ro</h4>
                <p>Đây là cách so sánh mức lợi nhuận giả định với mức tổn thất giả định. Tỷ lệ này không đảm bảo kết quả thực tế.</p>
                <h4>3. Đa dạng hóa</h4>
                <p>Không nên phụ thuộc toàn bộ kế hoạch vào một tài sản hoặc một giả định duy nhất. Các tài sản khác nhau vẫn có thể cùng giảm giá.</p>
                <h4>4. Tâm lý và kỷ luật</h4>
                <p>Ghi chép, đánh giá lại quyết định và tránh hành động vội vàng là những kỹ năng quan trọng trong giáo dục tài chính.</p>
            `
        },
        practice: {
            title: "Thực hành mô phỏng",
            content: `
                <h4>1. Mục đích mô phỏng</h4>
                <p>Môi trường mô phỏng giúp làm quen với giao diện và cách ghi nhận dữ liệu giả lập mà không sử dụng tiền thật.</p>
                <h4>2. Làm quen với BUY và SELL</h4>
                <p>BUY và SELL là hai lựa chọn hướng biến động được mô phỏng trong giao diện. Kết quả chỉ có ý nghĩa minh họa theo dữ liệu giả lập của ứng dụng.</p>
                <h4>3. Theo dõi lịch sử</h4>
                <p>Tab Lịch sử hiển thị các lệnh mô phỏng đã đóng. Tab Thống kê tổng hợp số lượng kết quả theo từng nhóm.</p>
                <p><strong>Lưu ý:</strong> Giá và kết quả hiện tại là dữ liệu minh họa, không phải báo giá trực tiếp hay kết quả đầu tư thực tế.</p>
            `
        }
    };

    let progress = {};

    function loadProgress() {
        try {
            progress = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
        } catch (error) {
            progress = {};
        }
    }

    function saveProgress() {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(progress));
    }

    function updateProgress() {
        const completed = Object.keys(progress).filter(key => progress[key] === true).length;
        const total = Object.keys(lessons).length;

        const text = document.getElementById("learning-progress-text");
        const bar = document.getElementById("learning-progress-bar");

        if (text) text.textContent = completed + " / " + total + " chuyên đề";
        if (bar) bar.style.width = (completed / total * 100) + "%";

        document.querySelectorAll(".learning-card").forEach(card => {
            const button = card.querySelector(".learning-start-btn");
            if (!button) return;

            const key = button.dataset.lesson;
            const done = progress[key] === true;

            card.classList.toggle("completed", done);
            button.textContent = done ? "Đã hoàn thành · Xem lại" : "Bắt đầu học";
        });
    }

    function openLesson(key) {
        const lesson = lessons[key];
        if (!lesson) return;

        document.getElementById("learning-lesson-title").textContent = lesson.title;
        document.getElementById("learning-lesson-content").innerHTML = lesson.content;

        const area = document.getElementById("learning-lesson-area");
        const completeButton = document.getElementById("learning-complete-btn");

        completeButton.dataset.lesson = key;
        completeButton.textContent = progress[key] ? "Đã hoàn thành · Học lại" : "Đánh dấu đã hoàn thành";

        area.hidden = false;
        area.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function initialize() {
        const area = document.getElementById("learning-lesson-area");
        if (!area) return;

        loadProgress();
        updateProgress();

        document.querySelectorAll(".learning-start-btn").forEach(button => {
            button.addEventListener("click", () => openLesson(button.dataset.lesson));
        });

        document.getElementById("learning-close-btn").addEventListener("click", () => {
            area.hidden = true;
        });

        document.getElementById("learning-complete-btn").addEventListener("click", event => {
            const key = event.currentTarget.dataset.lesson;
            if (!lessons[key]) return;

            progress[key] = true;
            saveProgress();
            updateProgress();

            event.currentTarget.textContent = "Đã hoàn thành · Học lại";
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialize);
    } else {
        initialize();
    }
})();
