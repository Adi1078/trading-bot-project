import logging
from datetime import datetime
from database import SessionLocal
from models.trade import Trade
from models.log import Log

logger = logging.getLogger(__name__)


def generate_monthly_report(year: int, month: int) -> dict:
    """
    Generate a monthly report for all trades in the given month.
    Returns a dict with summary stats and the full trade list.
    """
    db = SessionLocal()
    try:
        # Fetch all trades closed in the given month
        trades = db.query(Trade).filter(
            Trade.status == "closed",
            Trade.closed_at != None
        ).all()

        # Real-money trades only — paper trades are excluded from reports
        monthly_trades = [
            t for t in trades
            if t.closed_at and t.closed_at.year == year and t.closed_at.month == month
            and not t.is_paper_trade
        ]

        total_trades = len(monthly_trades)
        profitable = [t for t in monthly_trades if t.pnl and t.pnl > 0]
        losing = [t for t in monthly_trades if t.pnl and t.pnl < 0]

        total_pnl = sum(t.pnl for t in monthly_trades if t.pnl is not None)
        total_profit = sum(t.pnl for t in profitable)
        total_loss = sum(t.pnl for t in losing)

        report = {
            "month": f"{year}-{str(month).zfill(2)}",
            "total_trades": total_trades,
            "profitable_trades": len(profitable),
            "losing_trades": len(losing),
            "paper_trades": 0,
            "total_pnl": round(total_pnl, 2),
            "total_profit": round(total_profit, 2),
            "total_loss": round(total_loss, 2),
            "trades": [_trade_to_dict(t) for t in monthly_trades]
        }

        log = Log(level="INFO", message=f"Monthly report generated for {year}-{month}")
        db.add(log)
        db.commit()

        return report

    finally:
        db.close()


def generate_monthly_report_html(year: int, month: int) -> str:
    """Generate an HTML version of the monthly report for email or download."""
    report = generate_monthly_report(year, month)
    month_label = datetime(year, month, 1).strftime("%B %Y")

    pnl_color = "#2ecc71" if report["total_pnl"] >= 0 else "#e74c3c"

    rows = ""
    for t in report["trades"]:
        pnl_val = t["pnl"] if t["pnl"] is not None else "N/A"
        pnl_col = "#2ecc71" if isinstance(pnl_val, float) and pnl_val >= 0 else "#e74c3c"
        paper_tag = " (Paper)" if t["is_paper_trade"] else ""
        rows += f"""
        <tr>
            <td>{t['stock_name']}{paper_tag}</td>
            <td>{t['trade_source']}</td>
            <td>{t['placed_at']}</td>
            <td>{t['closed_at']}</td>
            <td>{t['close_reason']}</td>
            <td style="color:{pnl_col};"><b>₹{pnl_val}</b></td>
        </tr>
        """

    html = f"""
    <html><body style="font-family:Arial,sans-serif;">
    <h2>Monthly Trading Report — {month_label}</h2>

    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;margin-bottom:20px;">
        <tr><td><b>Total Trades</b></td><td>{report['total_trades']}</td></tr>
        <tr><td><b>Profitable</b></td><td style="color:#2ecc71;">{report['profitable_trades']}</td></tr>
        <tr><td><b>Losing</b></td><td style="color:#e74c3c;">{report['losing_trades']}</td></tr>
        <tr><td><b>Paper Trades</b></td><td>{report['paper_trades']}</td></tr>
        <tr><td><b>Total Profit</b></td><td style="color:#2ecc71;">₹{report['total_profit']}</td></tr>
        <tr><td><b>Total Loss</b></td><td style="color:#e74c3c;">₹{report['total_loss']}</td></tr>
        <tr><td><b>Net P&amp;L</b></td><td style="color:{pnl_color};"><b>₹{report['total_pnl']}</b></td></tr>
    </table>

    <h3>Trade Details</h3>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;">
        <tr style="background:#f0f0f0;">
            <th>Stock</th><th>Source</th><th>Placed At</th>
            <th>Closed At</th><th>Close Reason</th><th>P&amp;L</th>
        </tr>
        {rows}
    </table>
    </body></html>
    """
    return html


def generate_yearly_report(year: int) -> dict:
    """
    Generate a yearly report aggregating all real-money trades closed in the year,
    with a month-by-month breakdown. Paper trades are excluded.
    """
    db = SessionLocal()
    try:
        trades = db.query(Trade).filter(
            Trade.status == "closed",
            Trade.closed_at != None
        ).all()

        # Real-money trades only, closed in the given year
        yearly_trades = [
            t for t in trades
            if t.closed_at and t.closed_at.year == year and not t.is_paper_trade
        ]

        total_trades = len(yearly_trades)
        profitable = [t for t in yearly_trades if t.pnl and t.pnl > 0]
        losing = [t for t in yearly_trades if t.pnl and t.pnl < 0]

        total_pnl = sum(t.pnl for t in yearly_trades if t.pnl is not None)
        total_profit = sum(t.pnl for t in profitable)
        total_loss = sum(t.pnl for t in losing)

        # Month-by-month breakdown
        monthly_breakdown = []
        for m in range(1, 13):
            m_trades = [t for t in yearly_trades if t.closed_at.month == m]
            m_pnl = sum(t.pnl for t in m_trades if t.pnl is not None)
            monthly_breakdown.append({
                "month": datetime(year, m, 1).strftime("%B"),
                "total_trades": len(m_trades),
                "profitable_trades": len([t for t in m_trades if t.pnl and t.pnl > 0]),
                "losing_trades": len([t for t in m_trades if t.pnl and t.pnl < 0]),
                "total_pnl": round(m_pnl, 2),
            })

        report = {
            "year": year,
            "total_trades": total_trades,
            "profitable_trades": len(profitable),
            "losing_trades": len(losing),
            "total_pnl": round(total_pnl, 2),
            "total_profit": round(total_profit, 2),
            "total_loss": round(total_loss, 2),
            "monthly_breakdown": monthly_breakdown,
            "trades": [_trade_to_dict(t) for t in yearly_trades]
        }

        log = Log(level="INFO", message=f"Yearly report generated for {year}")
        db.add(log)
        db.commit()

        return report

    finally:
        db.close()


def generate_yearly_report_html(year: int) -> str:
    """Generate an HTML version of the yearly report for download."""
    report = generate_yearly_report(year)
    pnl_color = "#2ecc71" if report["total_pnl"] >= 0 else "#e74c3c"

    breakdown_rows = ""
    for m in report["monthly_breakdown"]:
        m_col = "#2ecc71" if m["total_pnl"] >= 0 else "#e74c3c"
        breakdown_rows += f"""
        <tr>
            <td>{m['month']}</td>
            <td>{m['total_trades']}</td>
            <td style="color:#2ecc71;">{m['profitable_trades']}</td>
            <td style="color:#e74c3c;">{m['losing_trades']}</td>
            <td style="color:{m_col};"><b>₹{m['total_pnl']}</b></td>
        </tr>
        """

    rows = ""
    for t in report["trades"]:
        pnl_val = t["pnl"] if t["pnl"] is not None else "N/A"
        pnl_col = "#2ecc71" if isinstance(pnl_val, float) and pnl_val >= 0 else "#e74c3c"
        rows += f"""
        <tr>
            <td>{t['stock_name']}</td>
            <td>{t['trade_source']}</td>
            <td>{t['placed_at']}</td>
            <td>{t['closed_at']}</td>
            <td>{t['close_reason']}</td>
            <td style="color:{pnl_col};"><b>₹{pnl_val}</b></td>
        </tr>
        """

    html = f"""
    <html><body style="font-family:Arial,sans-serif;">
    <h2>Yearly Trading Report — {year} (Real-Money Trades)</h2>

    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;margin-bottom:20px;">
        <tr><td><b>Total Trades</b></td><td>{report['total_trades']}</td></tr>
        <tr><td><b>Profitable</b></td><td style="color:#2ecc71;">{report['profitable_trades']}</td></tr>
        <tr><td><b>Losing</b></td><td style="color:#e74c3c;">{report['losing_trades']}</td></tr>
        <tr><td><b>Total Profit</b></td><td style="color:#2ecc71;">₹{report['total_profit']}</td></tr>
        <tr><td><b>Total Loss</b></td><td style="color:#e74c3c;">₹{report['total_loss']}</td></tr>
        <tr><td><b>Net P&amp;L</b></td><td style="color:{pnl_color};"><b>₹{report['total_pnl']}</b></td></tr>
    </table>

    <h3>Month-by-Month Breakdown</h3>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;margin-bottom:20px;">
        <tr style="background:#f0f0f0;">
            <th>Month</th><th>Trades</th><th>Profitable</th><th>Losing</th><th>Net P&amp;L</th>
        </tr>
        {breakdown_rows}
    </table>

    <h3>Trade Details</h3>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;">
        <tr style="background:#f0f0f0;">
            <th>Stock</th><th>Source</th><th>Placed At</th>
            <th>Closed At</th><th>Close Reason</th><th>P&amp;L</th>
        </tr>
        {rows}
    </table>
    </body></html>
    """
    return html


def _trade_to_dict(trade: Trade) -> dict:
    return {
        "stock_name": trade.stock_name,
        "trade_source": trade.trade_source,
        "is_paper_trade": trade.is_paper_trade,
        "month_type": trade.month_type,
        "futures_entry_price": trade.futures_entry_price,
        "ce_entry_price": trade.ce_entry_price,
        "pe_entry_price": trade.pe_entry_price,
        "futures_exit_price": trade.futures_exit_price,
        "ce_exit_price": trade.ce_exit_price,
        "pe_exit_price": trade.pe_exit_price,
        "profit_target": trade.profit_target,
        "loss_limit": trade.loss_limit,
        "close_reason": trade.close_reason,
        "pnl": trade.pnl,
        "placed_at": str(trade.placed_at) if trade.placed_at else None,
        "closed_at": str(trade.closed_at) if trade.closed_at else None
    }
