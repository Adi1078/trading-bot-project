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


def is_trading_day(check_date=None):
    """
    True only on NSE trading days. Excludes weekends (Sat/Sun) and NSE holidays.
    Defaults to today (IST) if no date is given.
    """
    d = check_date or datetime.now(IST).date()
    if d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    if d.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    return True


def _get_last_tuesday(year, month):
    last_day = calendar.monthrange(year, month)[1]
    last_date = datetime(year, month, last_day)
    # Tuesday = weekday 1, find how many days to go back
    days_back = (last_date.weekday() - 1) % 7
    return (last_date - timedelta(days=days_back)).date()


def get_expiry_date(year, month):
    """
    Fallback estimate of a month's F&O expiry, used ONLY when the live scrip
    master can't be loaded. NSE monthly contracts expire on the last Tuesday of
    the month (it was the last Thursday before the 2025 expiry-day change). Rolls
    back over holidays/weekends to the previous trading day.

    The real, authoritative expiry comes from the scrip master via
    get_current_expiry()/get_next_expiry() — this is just a safety net.
    """
    expiry = _get_last_tuesday(year, month)
    # If expiry falls on a holiday or weekend, move back until a valid trading day
    while True:
        date_str = expiry.strftime("%Y-%m-%d")
        if date_str in NSE_HOLIDAYS or expiry.weekday() >= 5:
            expiry = expiry - timedelta(days=1)
        else:
            break
    return expiry


def _scrip_master_expiries():
    """Real monthly expiry dates from the NSE scrip master; [] if unavailable."""
    try:
        from broker import fivepaisa
        return fivepaisa.get_monthly_expiries()
    except Exception:
        return []


def get_current_expiry():
    """
    The nearest monthly F&O expiry that hasn't passed yet.

    Primary source is the live scrip master, so we always track the actual NSE
    contract calendar whatever weekday the exchange uses. Falls back to a computed
    last-Tuesday only if the scrip master can't be loaded.
    """
    now = datetime.now(IST).date()
    upcoming = [e for e in _scrip_master_expiries() if e >= now]
    if upcoming:
        return upcoming[0]

    # Fallback: compute from the calendar
    expiry = get_expiry_date(now.year, now.month)
    if now > expiry:
        if now.month == 12:
            return get_expiry_date(now.year + 1, 1)
        return get_expiry_date(now.year, now.month + 1)
    return expiry


def get_next_expiry():
    """The monthly expiry immediately after the current one (scrip master first)."""
    now = datetime.now(IST).date()
    upcoming = [e for e in _scrip_master_expiries() if e >= now]
    if len(upcoming) >= 2:
        return upcoming[1]

    # Fallback: compute the month after the current expiry
    current = get_current_expiry()
    if current.month == 12:
        return get_expiry_date(current.year + 1, 1)
    return get_expiry_date(current.year, current.month + 1)


def is_last_trading_day():
    today = datetime.now(IST).date()
    expiry = get_current_expiry()
    return today == expiry
