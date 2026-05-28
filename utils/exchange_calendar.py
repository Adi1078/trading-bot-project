import calendar
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

# NSE holidays for 2026 - update this list every year
NSE_HOLIDAYS = [
    "2026-01-26",  # Republic Day
    "2026-03-02",  # Holi
    "2026-03-30",  # Ram Navami
    "2026-04-02",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-20",  # Diwali Laxmi Puja
    "2026-10-21",  # Diwali Balipratipada
    "2026-11-04",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
]


def _get_last_thursday(year, month):
    last_day = calendar.monthrange(year, month)[1]
    last_date = datetime(year, month, last_day)
    # Thursday = weekday 3, find how many days to go back
    days_back = (last_date.weekday() - 3) % 7
    return (last_date - timedelta(days=days_back)).date()


def get_expiry_date(year, month):
    expiry = _get_last_thursday(year, month)
    # If expiry falls on a holiday or weekend, move back until a valid trading day
    while True:
        date_str = expiry.strftime("%Y-%m-%d")
        if date_str in NSE_HOLIDAYS or expiry.weekday() >= 5:
            expiry = expiry - timedelta(days=1)
        else:
            break
    return expiry


def get_current_expiry():
    now = datetime.now(IST).date()
    expiry = get_expiry_date(now.year, now.month)
    # If today is past this month's expiry, return next month's expiry
    if now > expiry:
        if now.month == 12:
            return get_expiry_date(now.year + 1, 1)
        else:
            return get_expiry_date(now.year, now.month + 1)
    return expiry


def get_next_expiry():
    current = get_current_expiry()
    if current.month == 12:
        return get_expiry_date(current.year + 1, 1)
    else:
        return get_expiry_date(current.year, current.month + 1)


def is_last_trading_day():
    today = datetime.now(IST).date()
    expiry = get_current_expiry()
    return today == expiry
