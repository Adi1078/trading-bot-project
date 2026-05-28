import uuid
import pytz
from datetime import datetime

IST = pytz.timezone("Asia/Kolkata")


def get_ist_now():
    return datetime.now(IST)


def get_ist_time_str():
    return datetime.now(IST).strftime("%H:%M")


def is_market_hours():
    now = datetime.now(IST)
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
    now = datetime.now(IST)
    return now.hour == 15 and now.minute == 40
