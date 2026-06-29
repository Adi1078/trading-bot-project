import logging
from datetime import timedelta
from database import SessionLocal
from models.trade import Trade
from models.settings import Settings
from models.log import Log
from broker import fivepaisa
from utils.helpers import get_ist_now, calculate_trade_pnl, to_naive

logger = logging.getLogger(__name__)

# A freshly placed trade is NOT reconciled until it is at least this old. Right
# after a market-open entry, 5paisa's NetPositionNetWise feed lags and may not yet
# list the just-opened position, which previously made the reconciler conclude the
# client had "manually closed" a live trade within a minute of it opening.
SYNC_GRACE_MINUTES = 15


def _save_log(db, level: str, message: str):
    log = Log(level=level, message=message)
    db.add(log)
    db.commit()


def sync_positions():
    """
    Compare our open trades in the database with actual open positions on 5paisa.
    If a trade our bot placed is no longer open on 5paisa (client closed it manually),
    we mark it as closed in our database too.
    We completely ignore any positions on 5paisa that our bot did not place.
    """
    db = SessionLocal()
    try:
        settings = db.query(Settings).first()

        if not settings or not settings.access_token:
            return

        # Get all trades our bot currently thinks are open
        our_open_trades = db.query(Trade).filter(
            Trade.status == "open",
            Trade.is_paper_trade == False
        ).all()

        if not our_open_trades:
            return

        # Fetch actual positions from 5paisa
        result = fivepaisa.get_positions(settings.access_token, settings.client_code)

        if not result["success"]:
            _save_log(db, "WARNING", f"Position sync failed: could not fetch positions from 5paisa - {result['error']}")
            return

        live_positions = result.get("positions", [])

        # CRITICAL SAFETY GUARD: an EMPTY positions snapshot is never treated as
        # "the client closed everything". 5paisa returns an empty NetPositionNetWise
        # list both when the account is genuinely flat AND, transiently, right after
        # a market-open entry (feed lag) or on a "no record found" response — the two
        # are indistinguishable. Concluding a manual close from an empty snapshot is
        # what falsely closed live trades within a minute of opening (and with no
        # P&L). We only ever trust an *absence* when the feed is alive and returning
        # OTHER positions, so the absence is real rather than a lagging/empty feed.
        if not live_positions:
            _save_log(db, "WARNING",
                f"Position sync: 5paisa returned NO positions while {len(our_open_trades)} "
                f"of our trades are open - treating as a lagging/empty feed, NOT a manual "
                f"close. No trades changed.")
            return

        # The Net Position response identifies positions by ScripCode + NetQty
        # (there is no OrderID). Build the set of scrip codes that still have an
        # open net quantity on 5paisa.
        open_scrip_codes = set()
        for position in live_positions:
            scrip_code = str(position.get("ScripCode", ""))
            net_qty = position.get("NetQty", 0)
            if scrip_code and net_qty != 0:
                open_scrip_codes.add(scrip_code)

        now = get_ist_now()

        # Check each of our open trades
        for trade in our_open_trades:
            # Grace period: a just-placed trade may not show in the feed yet.
            # to_naive() because placed_at loaded from SQLite is tz-naive while
            # get_ist_now() is tz-aware (can't subtract the two directly).
            if trade.placed_at and (to_naive(now) - to_naive(trade.placed_at)) < timedelta(minutes=SYNC_GRACE_MINUTES):
                continue

            if _is_closed_on_broker(trade, open_scrip_codes):
                # Record P&L from current prices so a genuine manual close isn't
                # left blank (mirrors the profit/loss/expiry close path).
                _record_manual_close_pnl(db, settings, trade)

                trade.status = "closed"
                trade.close_reason = "manual"
                trade.closed_at = now

                _save_log(db, "INFO",
                    f"Position sync: trade {trade.id} ({trade.stock_name}) is no longer "
                    f"open on 5paisa (legs absent from a live, non-empty positions feed) - "
                    f"marking closed (manual). P&L={trade.pnl}")
                db.commit()

    finally:
        db.close()


def _record_manual_close_pnl(db, settings, trade: Trade):
    """
    Fetch current leg prices and store exit prices + P&L on a trade the client
    closed manually, so it doesn't show a blank P&L. Best-effort: if prices can't
    be fetched we leave P&L blank rather than block the close.
    """
    try:
        from bot.trade_manager import _fetch_current_prices
        prices = _fetch_current_prices(db, settings, trade)
        if not prices:
            return
        trade.futures_exit_price, trade.ce_exit_price, trade.pe_exit_price = prices
        trade.pnl = calculate_trade_pnl(
            trade.futures_entry_price, trade.futures_exit_price,
            trade.ce_entry_price, trade.ce_exit_price,
            trade.pe_entry_price, trade.pe_exit_price,
            trade.lot_size,
        )
    except Exception as e:
        logger.error(f"[SYNC:PNL] could not record P&L for trade {trade.id}: {e}", exc_info=True)


def _is_closed_on_broker(trade: Trade, open_scrip_codes: set) -> bool:
    """
    Check if all legs of a trade are no longer active on 5paisa.
    A leg is identified by its scrip code; it is still open if that scrip code
    has a non-zero net quantity. Returns True only if NONE of the trade's legs
    still have an open position (i.e. the client squared everything off manually).
    """
    leg_scrip_codes = [
        str(code) for code in [
            trade.futures_scrip_code,
            trade.ce_scrip_code,
            trade.pe_scrip_code
        ]
        if code is not None
    ]

    if not leg_scrip_codes:
        return False

    # If none of our legs still hold an open net position, the trade is closed
    return not any(code in open_scrip_codes for code in leg_scrip_codes)
