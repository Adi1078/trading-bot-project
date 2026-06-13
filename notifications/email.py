import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER

logger = logging.getLogger(__name__)


def _resolve_receiver(to: str = None):
    """
    Pick where to send: an explicit recipient if given, otherwise the client's
    configured notification email from settings, falling back to EMAIL_RECEIVER.
    """
    if to:
        return to
    try:
        from database import SessionLocal
        from models.settings import Settings
        db = SessionLocal()
        try:
            s = db.query(Settings).first()
            if s and s.notification_email:
                return s.notification_email
        finally:
            db.close()
    except Exception:
        pass
    return EMAIL_RECEIVER


def _send_email(subject: str, body: str, to: str = None):
    """
    Core function to send an email. All other functions call this.
    Raises with a step-tagged message so callers can pinpoint where it broke:
    [config] / [build] / [connect] / [login] / [send].
    """
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        logger.warning("Email credentials not configured, skipping email")
        return

    receiver = _resolve_receiver(to)

    # Build the message
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = receiver
        msg.attach(MIMEText(body, "html"))
    except Exception as e:
        logger.error(f"[build] Failed to build email '{subject}': {e}")
        raise RuntimeError(f"[build] could not build message: {e}")

    # Connect → login → send, each step tagged so the exact failure point is clear
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            try:
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            except smtplib.SMTPAuthenticationError as e:
                logger.error(f"[login] SMTP auth failed for {EMAIL_SENDER}: {e}")
                raise RuntimeError(
                    "[login] Gmail rejected the sender login — check EMAIL_SENDER "
                    "and that EMAIL_PASSWORD is a valid Gmail App Password (not the "
                    f"normal account password). Detail: {e}"
                )
            try:
                server.sendmail(EMAIL_SENDER, receiver, msg.as_string())
            except smtplib.SMTPRecipientsRefused as e:
                logger.error(f"[send] recipient refused {receiver}: {e}")
                raise RuntimeError(f"[send] recipient address refused: {receiver} — {e}")
    except (smtplib.SMTPConnectError, OSError, TimeoutError) as e:
        logger.error(f"[connect] could not reach smtp.gmail.com:465 — {e}")
        raise RuntimeError(f"[connect] could not reach Gmail SMTP (port 465 blocked?): {e}")

    logger.info(f"Email sent to {receiver}: {subject}")


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


def send_safety_alert_email(open_trades: list):
    """
    Daily 3:40 PM summary of the still-open trades with their current P&L.
    open_trades: list of dicts {stock_name, is_paper, pnl, target, loss}.
    These positions are held until target/stop-loss or their expiry date — this
    is an informational summary, not an alert to act on.
    """
    rows = ""
    for t in open_trades:
        pnl = t.get("pnl")
        if isinstance(pnl, (int, float)):
            pnl_str = f"₹{pnl:.2f}"
            color = "#2ecc71" if pnl >= 0 else "#e74c3c"
        else:
            pnl_str, color = "—", "#8b949e"
        mode = "Paper" if t.get("is_paper") else "Live"
        rows += (
            f"<tr><td>{t.get('stock_name')}</td><td>{mode}</td>"
            f"<td style='color:{color};'><b>{pnl_str}</b></td>"
            f"<td>₹{t.get('target')}</td><td>₹{t.get('loss')}</td></tr>"
        )

    subject = "Daily Open Trades Summary (3:40 PM)"
    body = f"""
    <html><body style="font-family:Arial,sans-serif;">
    <h2>Open Trades — End-of-Day Summary</h2>
    <p>These positions are still open at 3:40 PM. They are held until target /
    stop-loss or their expiry date — no action needed.</p>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
        <tr style="background:#f0f0f0;">
            <th>Stock</th><th>Mode</th><th>Current P&amp;L</th><th>Target</th><th>Loss Limit</th>
        </tr>
        {rows}
    </table>
    </body></html>
    """
    _send_email(subject, body)


def send_monthly_report_email(report_html: str, month: str):
    """Send monthly trade report via email."""
    subject = f"Monthly Trading Report: {month}"
    _send_email(subject, report_html)


def send_test_email(to_email: str):
    """Send a test email to confirm the email pipe works end-to-end."""
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        raise RuntimeError(
            "Email credentials are not configured on the server "
            "(EMAIL_SENDER / EMAIL_PASSWORD). No email can be sent."
        )
    subject = "Test Email — iAction Pulse Trading Bot"
    body = """
    <html><body style="font-family:Arial,sans-serif;">
    <h2 style="color:#2ecc71;">Email is working ✅</h2>
    <p>If you can read this, the trading bot can successfully send you emails —
    including the end-of-day open-trades summary.</p>
    </body></html>
    """
    _send_email(subject, body, to=to_email)


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
