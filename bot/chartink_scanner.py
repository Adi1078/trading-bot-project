"""
Chartink screener polling.

Replaces the old webhook (push) model: instead of Chartink pushing signals to us,
we actively run the client's screeners every few minutes and read back the matching
NSE symbols. Built from the client's standalone script, but trimmed to one scan cycle
(no infinite loop, no CSV files, no pandas/bs4) so it runs cheaply inside the bot.

A screener is defined by its `scan_clause` (the filter query) — NOT its URL. The URL is
only used to obtain a CSRF token that Chartink requires for the process request.
"""
import logging
import re
import requests

logger = logging.getLogger(__name__)

# Any valid public screener page works for fetching the CSRF token.
CSRF_PAGE = "https://chartink.com/screener/btst-with-adx-volume-op-1"
PROCESS_URL = "https://chartink.com/screener/process"

# Default screeners (used as fallback if none are configured in the database).
DEFAULT_SCAN_CLAUSES = {
    "btst-with-adx-volume-op-1": (
        "( {33492} ( latest close > 3 days ago high and latest close > 2 days ago high "
        "and latest close > 1 day ago high and latest volume > 3 days ago volume and "
        "latest volume > 2 days ago volume and latest volume > 1 day ago volume and "
        "latest close > latest open and latest close > 5 days ago high and latest volume > "
        "5 days ago volume and latest volume > 4 days ago volume and latest close > "
        "4 days ago high and latest sma ( close,50 ) > 6 months ago ema ( close,20 ) and "
        "latest volume > latest sma ( volume,20 ) and latest close > 1 day ago close * 1.02 "
        "and latest adx di positive ( 14 ) >= latest adx di negative ( 14 ) and latest adx "
        "di positive ( 14 ) >= 10 and latest adx di positive ( 14 ) >= latest adx ( 14 ) and "
        "latest volume > 100000 and latest rsi( 14 ) >= 40) ) "
    ),
    "ema-9-26-cross-over-3": (
        "( {33492} ( latest ema ( close,9 ) > latest ema ( close,26 ) and 1 day ago  ema "
        "( close,9 )<= 1 day ago  ema ( close,26 ) ) ) "
    ),
    "50sma-2days-ver2-1": (
        "( {33492} ( 2 days ago close > 2 days ago sma ( 2 days ago close , 50 ) and "
        "3 days ago  close <= 3 days ago  sma ( 2 days ago close , 50 ) and 1 day ago close > "
        "2 days ago high ) ) "
    ),
}


def _get_configured_screeners():
    """Read screener config from the database; fall back to defaults if not set."""
    try:
        from database import SessionLocal
        from models.settings import Settings
        db = SessionLocal()
        try:
            s = db.query(Settings).first()
            if not s:
                return DEFAULT_SCAN_CLAUSES

            screeners = {}
            for i in range(1, 4):
                url_field = f"screener_{i}_url"
                clause_field = f"screener_{i}_clause"
                url = getattr(s, url_field, None)
                clause = getattr(s, clause_field, None)
                if clause:  # Only add if clause is non-empty
                    label = url or f"Screener {i}"
                    screeners[label] = clause

            return screeners if screeners else DEFAULT_SCAN_CLAUSES
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Failed to load screeners from DB: {e} — using defaults")
        return DEFAULT_SCAN_CLAUSES

# A browser-like User-Agent reduces the chance of Chartink rejecting the request.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def _get_csrf(session):
    """Fetch a screener page and extract the csrf-token (Chartink requires it for POST)."""
    resp = session.get(CSRF_PAGE, timeout=15)
    resp.raise_for_status()
    # Attribute order can vary, so try both orderings.
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', resp.text)
    if not m:
        m = re.search(r'content="([^"]+)"\s+name="csrf-token"', resp.text)
    if not m:
        raise ValueError("CSRF token not found on Chartink page")
    return m.group(1)


def run_chartink_scan():
    """
    Run all configured screeners once and return a de-duplicated, sorted list of
    NSE symbols (uppercased). A failure in any single screener is logged and skipped
    so it never breaks the whole scan; on a total failure an empty list is returned.
    """
    symbols = set()
    screeners = _get_configured_screeners()

    try:
        with requests.Session() as s:
            s.headers.update(HEADERS)
            try:
                s.headers["x-csrf-token"] = _get_csrf(s)
            except Exception as e:
                logger.error(f"Chartink: failed to get CSRF token - {e}")
                return []

            for label, clause in screeners.items():
                try:
                    r = s.post(PROCESS_URL, data={"scan_clause": clause}, timeout=15)
                    r.raise_for_status()
                    data = r.json()
                    rows = data.get("data", []) if isinstance(data, dict) else []
                    for row in rows:
                        code = row.get("nsecode")
                        if code:
                            symbols.add(code.strip().upper())
                    logger.info(f"Chartink screener '{label}': {len(rows)} stocks")
                except Exception as e:
                    logger.error(f"Chartink screener '{label}' failed - {e}")
                    continue
    except Exception as e:
        logger.error(f"Chartink scan error - {e}")
        return []

    return sorted(symbols)
