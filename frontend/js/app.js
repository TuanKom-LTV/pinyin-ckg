const navigationItems = document.querySelectorAll(".nav-item");

const pages = {
    overview: {
        title: "Tổng quan thị trường",
        breadcrumb: "TỔNG QUAN"
    },
    analysis: {
        title: "Phân tích AI",
        breadcrumb: "PHÂN TÍCH AI"
    },
    simulation: {
        title: "Giao dịch mô phỏng",
        breadcrumb: "GIAO DỊCH MÔ PHỎNG"
    },
    learning: {
        title: "Trung tâm học tập",
        breadcrumb: "HỌC TẬP"
    }
};

navigationItems.forEach((item) => {
    item.addEventListener("click", () => {
        const selectedPage = item.dataset.page;

        navigationItems.forEach((button) => {
            button.classList.remove("active");
        });

        item.classList.add("active");

        document.querySelectorAll(".page").forEach((page) => {
            page.classList.remove("active");
        });

        document
            .getElementById(`page-${selectedPage}`)
            .classList.add("active");

        document.getElementById("page-title").textContent =
            pages[selectedPage].title;

        document.getElementById("breadcrumb").textContent =
            pages[selectedPage].breadcrumb;
    });
});

async function checkBackend() {
    try {
        const response = await fetch("/api/v1/health");
        const data = await response.json();

        console.log("Kết nối backend thành công:", data);
    } catch (error) {
        console.error("Không thể kết nối backend:", error);
    }
}

checkBackend();
