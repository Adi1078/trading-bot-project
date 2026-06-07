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

// ── Broker Status ─────────────────────────────────────────────────────────────

async function checkBrokerStatus() {
    const data = await api("/api/auth/status");
    const dot = document.getElementById("brokerDot");
    const text = document.getElementById("brokerStatusText");
    const btn = document.getElementById("connectBrokerBtn");
    if (data.connected) {
        dot.classList.add("connected");
        text.textContent = "Connected";
        text.style.color = "#2ecc71";
        btn.classList.remove("btn-secondary");
        btn.classList.add("btn-primary");
        btn.textContent = "Broker Connected";
    } else {
        dot.classList.remove("connected");
        text.textContent = "Disconnected";
        text.style.color = "#e74c3c";
        btn.classList.remove("btn-primary");
        btn.classList.add("btn-secondary");
        btn.textContent = "Connect Broker";
    }
}

function connectBroker() {
    window.location.href = "/api/auth/login";
}

// ── Trading Toggle ────────────────────────────────────────────────────────────

async function loadTradingStatus() {
    const data = await api("/api/dashboard/trading-status");
    const toggle = document.getElementById("tradingToggle");
    toggle.checked = data.is_trading;
    updateTradingLabel(data.is_trading);
}

async function toggleTrading() {
    const data = await api("/api/dashboard/toggle-trading", { method: "POST" });
    updateTradingLabel(data.is_trading);
    showToast(data.is_trading ? "Trading started" : "Trading stopped", data.is_trading ? "success" : "info");
}

function updateTradingLabel(isTrading) {
    const label = document.getElementById("tradingLabel");
    label.textContent = isTrading ? "LIVE" : "STOPPED";
    label.style.color = isTrading ? "#2ecc71" : "#e74c3c";
}

// ── P&L Stats ─────────────────────────────────────────────────────────────────

async function loadPnl() {
    const data = await api("/api/dashboard/pnl");
    const pnlEl = document.getElementById("totalPnl");
    pnlEl.textContent = `₹${data.total_pnl.toFixed(2)}`;
    pnlEl.className = "value " + (data.total_pnl >= 0 ? "positive" : "negative");
    document.getElementById("openTradesCount").textContent = data.open_trades_count;
    document.getElementById("closedTradesCount").textContent = data.closed_trades_count;
}

// ── Live Trades ───────────────────────────────────────────────────────────────

async function loadLiveTrades() {
    const data = await api("/api/dashboard/live-trades");
    const tbody = document.getElementById("liveTradesBody");

    if (!data.trades || data.trades.length === 0) {
        tbody.innerHTML = `<tr><td colspan="10" class="empty-state">No open trades</td></tr>`;
        return;
    }

    tbody.innerHTML = data.trades.map(t => {
        let pnlCell = '<span style="color:#8b949e;">—</span>';
        if (t.live_pnl !== null && t.live_pnl !== undefined) {
            const cls = t.live_pnl >= 0 ? "pnl-positive" : "pnl-negative";
            const sign = t.live_pnl >= 0 ? "+" : "";
            pnlCell = `<span class="${cls}">${sign}₹${t.live_pnl.toFixed(2)}</span>`;
        }
        return `
        <tr>
            <td><b>${t.stock_name}</b></td>
            <td>${t.trade_source}</td>
            <td>${t.is_paper_trade ? '<span class="badge badge-paper">Paper</span>' : '<span class="badge badge-open">Live</span>'}</td>
            <td>${t.futures_entry_price ? "₹" + t.futures_entry_price : "—"}</td>
            <td>${t.ce_entry_price ? "₹" + t.ce_entry_price : "—"}</td>
            <td>${t.pe_entry_price ? "₹" + t.pe_entry_price : "—"}</td>
            <td>${pnlCell}</td>
            <td style="color:#2ecc71;">₹${t.profit_target}</td>
            <td style="color:#e74c3c;">₹${t.loss_limit}</td>
            <td>
                <button class="btn btn-danger btn-sm" onclick="closeTrade(${t.id}, '${t.stock_name}')">Close</button>
            </td>
        </tr>`;
    }).join("");
}

async function closeTrade(tradeId, stockName) {
    if (!confirm(`Are you sure you want to close the trade for ${stockName}?`)) return;
    const data = await api(`/api/dashboard/close-trade/${tradeId}`, { method: "POST" });
    if (data.success) {
        showToast(`Trade for ${stockName} closed successfully`, "success");
        refreshAll();
    } else {
        showToast(data.error || "Failed to close trade", "error");
    }
}

// ── Trade History ─────────────────────────────────────────────────────────────

async function loadTradeHistory() {
    const data = await api("/api/dashboard/trade-history");
    const tbody = document.getElementById("tradeHistoryBody");

    if (!data.trades || data.trades.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No closed trades</td></tr>`;
        return;
    }

    const reasonLabels = {
        profit: "Profit Hit", loss: "Loss Hit",
        manual: "Manual", expiry: "Expiry", time: "Close Time"
    };

    tbody.innerHTML = data.trades.map(t => {
        const pnl = t.pnl;
        const pnlClass = pnl >= 0 ? "pnl-positive" : "pnl-negative";
        const pnlText = pnl !== null ? `₹${pnl.toFixed(2)}` : "—";
        const paper = t.is_paper_trade ? ' <span class="badge badge-paper">Paper</span>' : "";
        return `
        <tr>
            <td><b>${t.stock_name}</b>${paper}</td>
            <td>${t.trade_source}</td>
            <td>${t.month_type || "—"}</td>
            <td>${reasonLabels[t.close_reason] || t.close_reason || "—"}</td>
            <td class="${pnlClass}">${pnlText}</td>
            <td>${formatDate(t.placed_at)}</td>
            <td>${formatDate(t.closed_at)}</td>
        </tr>`;
    }).join("");
}

// ── Logs ──────────────────────────────────────────────────────────────────────

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatDate(dateStr) {
    if (!dateStr) return "—";
    const d = new Date(dateStr);
    return d.toLocaleString("en-IN", { dateStyle: "short", timeStyle: "short" });
}

function refreshAll() {
    checkBrokerStatus();
    loadTradingStatus();
    loadPnl();
    loadLiveTrades();
    loadTradeHistory();
}

// ── Init ──────────────────────────────────────────────────────────────────────

refreshAll();
setInterval(refreshAll, 60000);        // full refresh every minute
setInterval(loadLiveTrades, 10000);    // live P&L refresh every 10 seconds
