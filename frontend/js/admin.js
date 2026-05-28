// ── API Helper ────────────────────────────────────────────────────────────────

async function api(url, options = {}) {
    const res = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        ...options
    });
    return res.json();
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function showToast(message, type = "info") {
    const container = document.getElementById("toastContainer");
    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

// ── Sidebar Navigation ────────────────────────────────────────────────────────

function showSection(e, name) {
    document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));
    document.querySelectorAll(".sidebar-item").forEach(s => s.classList.remove("active"));
    document.getElementById(`section-${name}`).classList.add("active");
    e.target.classList.add("active");

    if (name === "watchlist") loadWatchlist();
    if (name === "fixedTrades") loadFixedTrades();
    if (name === "chartink") loadChartinkSection();
}

// ── Settings ──────────────────────────────────────────────────────────────────

async function loadSettings() {
    const data = await api("/api/settings/");
    if (!data.settings) return;
    const s = data.settings;
    document.getElementById("appKey").placeholder = s.app_key || "Not set";
    document.getElementById("encryKey").placeholder = s.encry_key || "Not set";
    document.getElementById("userId").placeholder = s.user_id || "Not set";
    document.getElementById("algoId").value = s.algo_id || "";
    document.getElementById("notificationEmail").value = s.notification_email || "";
    document.getElementById("tradeStartTime").value = s.trade_start_time || "09:30";
    document.getElementById("tradeCloseTime").value = s.trade_close_time || "12:00";
}

async function saveCredentials() {
    const body = {};
    const appKey = document.getElementById("appKey").value.trim();
    const encryKey = document.getElementById("encryKey").value.trim();
    const userId = document.getElementById("userId").value.trim();
    const algoId = document.getElementById("algoId").value.trim();

    if (appKey) body.app_key = appKey;
    if (encryKey) body.encry_key = encryKey;
    if (userId) body.user_id = userId;
    if (algoId) body.algo_id = algoId;

    const data = await api("/api/settings/save", {
        method: "POST",
        body: JSON.stringify(body)
    });

    if (data.success) {
        showToast("Credentials saved successfully", "success");
        document.getElementById("appKey").value = "";
        document.getElementById("encryKey").value = "";
        document.getElementById("userId").value = "";
        loadSettings();
    } else {
        showToast(data.error || "Failed to save credentials", "error");
    }
}

async function saveTradeSettings() {
    const body = {
        notification_email: document.getElementById("notificationEmail").value.trim(),
        trade_start_time: document.getElementById("tradeStartTime").value,
        trade_close_time: document.getElementById("tradeCloseTime").value
    };

    const data = await api("/api/settings/save", {
        method: "POST",
        body: JSON.stringify(body)
    });

    if (data.success) {
        showToast("Settings saved successfully", "success");
    } else {
        showToast(data.error || "Failed to save settings", "error");
    }
}

// ── Watchlist ─────────────────────────────────────────────────────────────────

async function loadWatchlist() {
    const data = await api("/api/watchlist/");
    const tbody = document.getElementById("watchlistBody");

    if (!data.stocks || data.stocks.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No stocks in watchlist</td></tr>`;
        return;
    }

    tbody.innerHTML = data.stocks.map(s => `
        <tr>
            <td><b>${s.stock_name}</b></td>
            <td>${s.scrip_code}</td>
            <td>${s.exchange} / ${s.exchange_type}</td>
            <td>${formatDate(s.added_at)}</td>
            <td>
                <button class="btn btn-danger btn-sm" onclick="removeStock(${s.id}, '${s.stock_name}')">Remove</button>
            </td>
        </tr>
    `).join("");
}

let searchTimeout = null;

async function searchStocks() {
    const query = document.getElementById("stockSearch").value.trim();
    const list = document.getElementById("stockAutocomplete");

    if (query.length < 2) {
        list.classList.remove("show");
        return;
    }

    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(async () => {
        const data = await api(`/api/watchlist/search?query=${encodeURIComponent(query)}`);
        if (!data.success || !data.instruments.length) {
            list.classList.remove("show");
            return;
        }

        list.innerHTML = data.instruments.map(inst => `
            <div class="autocomplete-item" onclick="selectStock('${inst.Symbol}', '${inst.ScripCode}')">
                <b>${inst.Symbol}</b> — ${inst.FullName || ""}
            </div>
        `).join("");
        list.classList.add("show");
    }, 400);
}

function selectStock(name, scripCode) {
    document.getElementById("stockSearch").value = name;
    document.getElementById("selectedStockName").value = name;
    document.getElementById("stockScripCode").value = scripCode;
    document.getElementById("stockAutocomplete").classList.remove("show");
}

async function addStock() {
    const stockName = document.getElementById("selectedStockName").value.trim();
    const scripCode = document.getElementById("stockScripCode").value.trim();

    if (!stockName || !scripCode) {
        showToast("Please select a stock from the search results", "error");
        return;
    }

    const data = await api("/api/watchlist/add", {
        method: "POST",
        body: JSON.stringify({ stock_name: stockName, scrip_code: scripCode })
    });

    if (data.success) {
        showToast(`${stockName} added to watchlist`, "success");
        document.getElementById("stockSearch").value = "";
        document.getElementById("stockScripCode").value = "";
        document.getElementById("selectedStockName").value = "";
        loadWatchlist();
    } else {
        showToast(data.error || "Failed to add stock", "error");
    }
}

async function removeStock(stockId, stockName) {
    if (!confirm(`Remove ${stockName} from watchlist?`)) return;

    const data = await api(`/api/watchlist/${stockId}`, { method: "DELETE" });
    if (data.success) {
        showToast(`${stockName} removed`, "success");
        loadWatchlist();
    } else {
        showToast("Failed to remove stock", "error");
    }
}

// Close autocomplete when clicking outside
document.addEventListener("click", (e) => {
    if (!e.target.closest(".autocomplete-wrapper")) {
        document.getElementById("stockAutocomplete").classList.remove("show");
    }
});

// ── Fixed Trades ──────────────────────────────────────────────────────────────

async function loadFixedTrades() {
    const data = await api("/api/fixed-trades/");
    const tbody = document.getElementById("fixedTradesBody");

    if (!data.trades || data.trades.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" class="empty-state">No fixed trades configured</td></tr>`;
        return;
    }

    tbody.innerHTML = data.trades.map(t => `
        <tr>
            <td><b>${t.stock_name}</b></td>
            <td>${t.strike_type}</td>
            <td>${t.strike_value}</td>
            <td>${t.lot_size || 1}</td>
            <td style="color:#2ecc71;">₹${t.profit_target}</td>
            <td style="color:#e74c3c;">₹${t.loss_limit}</td>
            <td>${t.month_type}</td>
            <td>
                <span class="badge ${t.is_trade ? 'badge-open' : 'badge-paper'}">
                    ${t.is_trade ? "Yes" : "Paper"}
                </span>
            </td>
            <td style="display:flex;gap:6px;">
                <button class="btn btn-secondary btn-sm" onclick="editFixedTrade(${JSON.stringify(t).replace(/"/g, '&quot;')})">Edit</button>
                <button class="btn btn-warning btn-sm" onclick="toggleTrade(${t.id}, '${t.stock_name}')">Toggle</button>
                <button class="btn btn-danger btn-sm" onclick="deleteFixedTrade(${t.id}, '${t.stock_name}')">Delete</button>
            </td>
        </tr>
    `).join("");
}

function openAddTradeModal() {
    document.getElementById("tradeModalTitle").textContent = "Add Fixed Trade";
    document.getElementById("editTradeId").value = "";
    document.getElementById("ftStockName").value = "";
    document.getElementById("ftScripCode").value = "";
    document.getElementById("ftStrikeType").value = "fixed";
    document.getElementById("ftStrikeValue").value = "";
    document.getElementById("ftProfit").value = "";
    document.getElementById("ftLoss").value = "";
    document.getElementById("ftMonthType").value = "current";
    document.getElementById("ftLotSize").value = "1";
    document.getElementById("ftIsTrade").value = "true";
    document.getElementById("tradeModal").classList.add("show");
}

function editFixedTrade(trade) {
    document.getElementById("tradeModalTitle").textContent = "Edit Fixed Trade";
    document.getElementById("editTradeId").value = trade.id;
    document.getElementById("ftStockName").value = trade.stock_name;
    document.getElementById("ftScripCode").value = trade.scrip_code || "";
    document.getElementById("ftStrikeType").value = trade.strike_type;
    document.getElementById("ftStrikeValue").value = trade.strike_value;
    document.getElementById("ftProfit").value = trade.profit_target;
    document.getElementById("ftLoss").value = trade.loss_limit;
    document.getElementById("ftMonthType").value = trade.month_type;
    document.getElementById("ftLotSize").value = trade.lot_size || 1;
    document.getElementById("ftIsTrade").value = String(trade.is_trade);
    document.getElementById("tradeModal").classList.add("show");
}

function closeTradeModal() {
    document.getElementById("tradeModal").classList.remove("show");
}

async function saveFixedTrade() {
    const editId = document.getElementById("editTradeId").value;
    const body = {
        stock_name: document.getElementById("ftStockName").value.trim().toUpperCase(),
        scrip_code: document.getElementById("ftScripCode").value.trim() || null,
        strike_type: document.getElementById("ftStrikeType").value,
        strike_value: parseFloat(document.getElementById("ftStrikeValue").value),
        profit_target: parseFloat(document.getElementById("ftProfit").value),
        loss_limit: parseFloat(document.getElementById("ftLoss").value),
        month_type: document.getElementById("ftMonthType").value,
        lot_size: parseInt(document.getElementById("ftLotSize").value) || 1,
        is_trade: document.getElementById("ftIsTrade").value === "true"
    };

    if (!body.stock_name || isNaN(body.strike_value) || isNaN(body.profit_target) || isNaN(body.loss_limit)) {
        showToast("Please fill all required fields", "error");
        return;
    }

    const url = editId ? `/api/fixed-trades/${editId}` : "/api/fixed-trades/add";
    const method = editId ? "PUT" : "POST";

    const data = await api(url, { method, body: JSON.stringify(body) });

    if (data.success) {
        showToast(editId ? "Trade updated" : "Trade added", "success");
        closeTradeModal();
        loadFixedTrades();
    } else {
        showToast(data.error || "Failed to save trade", "error");
    }
}

async function toggleTrade(tradeId, stockName) {
    const data = await api(`/api/fixed-trades/toggle/${tradeId}`, { method: "POST" });
    if (data.success) {
        showToast(`${stockName} trade ${data.is_trade ? "enabled" : "disabled (paper)"}`, "success");
        loadFixedTrades();
    } else {
        showToast("Failed to toggle trade", "error");
    }
}

async function deleteFixedTrade(tradeId, stockName) {
    if (!confirm(`Delete fixed trade for ${stockName}?`)) return;
    const data = await api(`/api/fixed-trades/${tradeId}`, { method: "DELETE" });
    if (data.success) {
        showToast(`${stockName} deleted`, "success");
        loadFixedTrades();
    } else {
        showToast("Failed to delete trade", "error");
    }
}

// ── Chartink ──────────────────────────────────────────────────────────────────

function loadChartinkSection() {
    document.getElementById("webhookUrl").value =
        window.location.origin + "/api/webhook/chartink";
    loadWebhookSettings();
}

async function loadWebhookSettings() {
    const data = await api("/api/settings/");
    if (!data.settings) return;
    const s = data.settings;
    document.getElementById("webhookTradeType").value = s.webhook_trade_type || "collar";
    document.getElementById("webhookMonthType").value = s.webhook_month_type || "current";
    document.getElementById("webhookStrikeType").value = s.webhook_strike_type || "percent";
    document.getElementById("webhookStrikeValue").value = s.webhook_strike_value || 2;
    document.getElementById("webhookLotSize").value = s.webhook_lot_size || 1;
    document.getElementById("webhookProfit").value = s.webhook_profit_target || 15000;
    document.getElementById("webhookLoss").value = s.webhook_loss_limit || 12000;
}

function copyWebhookUrl() {
    const url = document.getElementById("webhookUrl").value;
    navigator.clipboard.writeText(url).then(() => {
        showToast("Webhook URL copied!", "success");
    });
}

async function saveWebhookSettings() {
    const profit = parseInt(document.getElementById("webhookProfit").value);
    const loss = parseInt(document.getElementById("webhookLoss").value);
    const strikeValue = parseFloat(document.getElementById("webhookStrikeValue").value);
    const lotSize = parseInt(document.getElementById("webhookLotSize").value);

    if (isNaN(profit) || isNaN(loss) || isNaN(strikeValue) || isNaN(lotSize)) {
        showToast("Please fill all fields with valid numbers", "error");
        return;
    }

    const body = {
        webhook_trade_type: document.getElementById("webhookTradeType").value,
        webhook_month_type: document.getElementById("webhookMonthType").value,
        webhook_strike_type: document.getElementById("webhookStrikeType").value,
        webhook_strike_value: strikeValue,
        webhook_lot_size: lotSize,
        webhook_profit_target: profit,
        webhook_loss_limit: loss
    };

    const data = await api("/api/settings/save", { method: "POST", body: JSON.stringify(body) });
    if (data.success) {
        showToast("Webhook configuration saved", "success");
    } else {
        showToast(data.error || "Failed to save", "error");
    }
}

async function testWebhook() {
    const stock = document.getElementById("testWebhookStock").value.trim().toUpperCase();
    if (!stock) {
        showToast("Enter a stock name", "error");
        return;
    }

    const resultEl = document.getElementById("testWebhookResult");
    resultEl.style.display = "block";
    resultEl.style.color = "#8b949e";
    resultEl.textContent = `Firing signal for ${stock}...`;

    const data = await api("/api/webhook/test", {
        method: "POST",
        body: JSON.stringify({ stock_name: stock })
    });

    if (data.success) {
        resultEl.style.color = "#2ecc71";
        resultEl.textContent = data.message;
        showToast(data.message, "success");
    } else {
        resultEl.style.color = "#e74c3c";
        resultEl.textContent = data.error || "Test failed";
        showToast(data.error || "Test failed", "error");
    }
}

// ── Reports ───────────────────────────────────────────────────────────────────

let currentReport = null;

async function generateReport() {
    const year = parseInt(document.getElementById("reportYear").value);
    const month = parseInt(document.getElementById("reportMonth").value);

    const data = await api(`/api/reports/?year=${year}&month=${month}`);

    if (!data || data.error) {
        showToast(data?.error || "Failed to generate report", "error");
        return;
    }

    currentReport = data;
    document.getElementById("reportOutput").style.display = "block";

    const monthNames = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    document.getElementById("reportTitle").textContent = `${monthNames[month]} ${year} Report`;
    document.getElementById("rTotalTrades").textContent = data.total_trades;

    const pnlEl = document.getElementById("rNetPnl");
    pnlEl.textContent = `₹${data.total_pnl.toFixed(2)}`;
    pnlEl.className = "value " + (data.total_pnl >= 0 ? "positive" : "negative");

    document.getElementById("rWinLoss").textContent = `${data.profitable_trades} / ${data.losing_trades}`;

    const tbody = document.getElementById("reportTradesBody");
    if (!data.trades || data.trades.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No trades this month</td></tr>`;
        return;
    }

    tbody.innerHTML = data.trades.map(t => {
        const pnlClass = t.pnl >= 0 ? "pnl-positive" : "pnl-negative";
        const paper = t.is_paper_trade ? " (Paper)" : "";
        return `
        <tr>
            <td>${t.stock_name}${paper}</td>
            <td>${t.trade_source}</td>
            <td>${t.close_reason || "—"}</td>
            <td class="${pnlClass}">${t.pnl !== null ? "₹" + t.pnl.toFixed(2) : "—"}</td>
            <td>${formatDate(t.placed_at)}</td>
            <td>${formatDate(t.closed_at)}</td>
        </tr>`;
    }).join("");
}

function downloadReport() {
    if (!currentReport) return;
    const year = document.getElementById("reportYear").value;
    const month = document.getElementById("reportMonth").value;
    window.open(`/api/reports/download?year=${year}&month=${month}`, "_blank");
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatDate(dateStr) {
    if (!dateStr) return "—";
    const d = new Date(dateStr);
    return d.toLocaleString("en-IN", { dateStyle: "short", timeStyle: "short" });
}

// ── Init ──────────────────────────────────────────────────────────────────────

loadSettings();
