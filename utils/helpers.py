import uuid
import pytz
from datetime import datetime

IST = pytz.timezone("Asia/Kolkata")


def get_ist_now():
    return datetime.now(IST)


def to_naive(dt):
    """
    Strip tzinfo so a tz-aware datetime (get_ist_now()) can be safely
    subtracted/compared with a tz-naive one. Datetimes loaded back from SQLite are
    naive, so `get_ist_now() - trade.placed_at` otherwise raises "can't subtract
    offset-naive and offset-aware datetimes". The server runs in IST, so the
    wall-clock values already align — this only removes the offset mismatch.
    """
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def get_ist_time_str():
    return datetime.now(IST).strftime("%H:%M")


def is_market_hours():
    # Only trade on NSE trading days — never on weekends or holidays
    from utils.exchange_calendar import is_trading_day
    now = datetime.now(IST)
    if not is_trading_day(now.date()):
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def is_within_trading_window(start_time_str, close_time_str):
    now = datetime.now(IST)
    start = now.replace(
        hour=int(start_time_str.split(":")[0]),
        minute=int(start_time_str.split(":")[1]),
        second=0, microsecond=0
    )
    close = now.replace(
        hour=int(close_time_str.split(":")[0]),
        minute=int(close_time_str.split(":")[1]),
        second=0, microsecond=0
    )
    return start <= now <= close


def generate_remote_order_id(stock_name):
    unique = str(uuid.uuid4())[:8].upper()
    return f"{stock_name}_{unique}"


def calculate_trade_pnl(futures_entry, futures_exit, ce_entry, ce_exit, pe_entry, pe_exit, lot_size=1):
    futures_pnl = (futures_exit - futures_entry) * lot_size if futures_exit is not None else 0
    ce_pnl = (ce_entry - ce_exit) * lot_size if ce_exit is not None else 0  # sold CE, profit when price drops
    pe_pnl = (pe_exit - pe_entry) * lot_size if pe_exit is not None else 0  # bought PE
    return round(futures_pnl + ce_pnl + pe_pnl, 2)


def is_safety_check_time():
    # Fires in the 3:40–3:59 PM window (not a single exact minute) so the once-a-
    # minute scheduler cadence can't skip past it. The "ran today" guard ensures it
    # still only sends once. Note: this is AFTER market close (3:30 PM), so the
    # scheduler must check it OUTSIDE the is_market_hours() gate.
    now = datetime.now(IST)
    return now.hour == 15 and now.minute >= 40
