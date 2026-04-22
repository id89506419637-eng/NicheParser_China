/**
 * NicheParser_China — Main JS
 * Фильтрация таблицы, сортировка, интерактивность.
 */

document.addEventListener("DOMContentLoaded", () => {
    initTableFilter();
    initTableSort();
    initRowClick();
});

// === Table Filter ===
function initTableFilter() {
    const filterInput = document.getElementById("niche-filter");
    if (!filterInput) return;

    filterInput.addEventListener("input", (e) => {
        const query = e.target.value.toLowerCase();
        const rows = document.querySelectorAll("#niches-table tbody tr");

        rows.forEach((row) => {
            const text = row.textContent.toLowerCase();
            row.style.display = text.includes(query) ? "" : "none";
        });
    });
}

// === Table Sort ===
function initTableSort() {
    const headers = document.querySelectorAll(".data-table th.sortable");
    if (!headers.length) return;

    headers.forEach((header) => {
        header.addEventListener("click", () => {
            const table = header.closest("table");
            const tbody = table.querySelector("tbody");
            const colIndex = parseInt(header.dataset.col);
            const rows = Array.from(tbody.querySelectorAll("tr"));

            // Toggle sort direction
            const isAsc = header.classList.contains("sort-asc");
            headers.forEach((h) => h.classList.remove("sort-asc", "sort-desc"));
            header.classList.add(isAsc ? "sort-desc" : "sort-asc");

            rows.sort((a, b) => {
                const aText = a.cells[colIndex]?.textContent.trim() || "";
                const bText = b.cells[colIndex]?.textContent.trim() || "";

                // Try numeric sort
                const aNum = parseFloat(aText.replace(/[^\d.-]/g, ""));
                const bNum = parseFloat(bText.replace(/[^\d.-]/g, ""));

                if (!isNaN(aNum) && !isNaN(bNum)) {
                    return isAsc ? bNum - aNum : aNum - bNum;
                }

                // Fall back to string sort
                return isAsc
                    ? bText.localeCompare(aText, "ru")
                    : aText.localeCompare(bText, "ru");
            });

            rows.forEach((row) => tbody.appendChild(row));
        });
    });
}

// === Row Click (navigate to detail) ===
function initRowClick() {
    const rows = document.querySelectorAll(".niche-row");

    rows.forEach((row) => {
        row.style.cursor = "pointer";
        row.addEventListener("click", (e) => {
            // Don't navigate if clicking a link/button
            if (e.target.closest("a, button")) return;

            const id = row.dataset.id;
            if (id) {
                window.location.href = `/niche/${id}`;
            }
        });
    });
}
