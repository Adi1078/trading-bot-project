import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER

logger = logging.getLogger(__name__)


def _send_email(subject: str, body: str):
    """Core function to send an email. All other functions call this."""
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        logger.warning("Email credentials not configured, skipping email")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())

        logger.info(f"Email sent: {subject}")

    except Exception as e:
        logger.error(f"Failed to send email '{subject}': {str(e)}")
        raise


def send_trade_opened_email(stock_name: str, futures_price: float, ce_strike: float,
                             ce_premium: float, pe_strike: float, pe_premium: float):
    """Send email when a new trade position is opened."""
    subject = f"Trade Opened: {stock_name}"
    body = f"""
    <html><body>
    <h2 style="color:#2ecc71;">Trade Opened ✅</h2>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
        <tr><td><b>Stock</b></td><td>{stock_name}</td></tr>
        <tr><td><b>Futures Price</b></td><td>₹{futures_price}</td></tr>
        <tr><td><b>CE Sold</b></td><td>Strike {ce_strike} @ ₹{ce_premium}</td></tr>
        <tr><td><b>PE Bought</b></td><td>Strike {pe_strike} @ ₹{pe_premium}</td></tr>
    </table>
    </body></html>
    """
    _send_email(subject, body)


def send_trade_closed_email(stock_name: str, reason: str, pnl: float):
    """Send email when a trade is closed with reason and P&L."""
    color = "#2ecc71" if pnl and pnl >= 0 else "#e74c3c"
    reason_labels = {
        "profit": "Profit Target Hit",
        "loss": "Loss Limit Hit",
        "manual": "Closed Manually",
        "expiry": "Expiry Day Close",
        "time": "Close Time Reached"
    }
    reason_label = reason_labels.get(reason, reason)
    pnl_display = f"₹{pnl}" if pnl is not None else "N/A"

    subject = f"Trade Closed: {stock_name} | {reason_label}"
    body = f"""
    <html><body>
    <h2 style="color:{color};">Trade Closed 🔔</h2>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
        <tr><td><b>Stock</b></td><td>{stock_name}</td></tr>
        <tr><td><b>Reason</b></td><td>{reason_label}</td></tr>
        <tr><td><b>P&amp;L</b></td><td style="color:{color};"><b>{pnl_display}</b></td></tr>
    </table>
    </body></html>
    """
    _send_email(subject, body)


def send_safety_alert_email(open_stocks: list):
    """Send alert email at 3:40 PM if trades are still open."""
    stocks_list = "".join(f"<li>{s}</li>" for s in open_stocks)
    subject = "⚠️ Safety Alert: Open Trades at 3:40 PM"
    body = f"""
    <html><body>
    <h2 style="color:#e67e22;">Safety Alert ⚠️</h2>
    <p>The following trades are still open at 3:40 PM. Trading has been stopped automatically.</p>
    <ul>{stocks_list}</ul>
    <p>Please check your 5paisa account and close positions manually if needed.</p>
    </body></html>
    """
    _send_email(subject, body)


def send_monthly_report_email(report_html: str, month: str):
    """Send monthly trade report via email."""
    subject = f"Monthly Trading Report: {month}"
    _send_email(subject, report_html)


def send_token_expiry_reminder():
    """Send 8:30 AM daily reminder to reconnect the broker before market opens."""
    subject = "Action Required: Reconnect 5paisa Before Market Opens"
    body = """
    <html><body>
    <h2 style="color:#e67e22;">Daily Broker Reconnect Required 🔗</h2>
    <p>The 5paisa access token expires every day. Market opens at <b>9:15 AM</b>.</p>
    <p>Please <b>reconnect the broker now</b> before trading starts:</p>
    <ol>
        <li>Open the Trading Bot dashboard</li>
        <li>Click <b>Connect Broker</b> in the top bar</li>
        <li>Complete the 5paisa login</li>
        <li>Confirm the status dot turns green</li>
    </ol>
    <p style="color:#e74c3c;"><b>If not reconnected, no trades will be placed today.</b></p>
    </body></html>
    """
    _send_email(subject, body)
