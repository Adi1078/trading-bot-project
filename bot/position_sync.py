import logging
from database import SessionLocal
from models.trade import Trade
from models.settings import Settings
from models.log import Log
from broker import fivepaisa
from utils.helpers import get_ist_now

logger = logging.getLogger(__name__)


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

        # The Net Position response identifies positions by ScripCode + NetQty
        # (there is no OrderID). Build the set of scrip codes that still have an
        # open net quantity on 5paisa.
        open_scrip_codes = set()
        for position in live_positions:
            scrip_code = str(position.get("ScripCode", ""))
            net_qty = position.get("NetQty", 0)
            if scrip_code and net_qty != 0:
                open_scrip_codes.add(scrip_code)

        # Check each of our open trades
        for trade in our_open_trades:
            if _is_closed_on_broker(trade, open_scrip_codes):
                trade.status = "closed"
                trade.close_reason = "manual"
                trade.closed_at = get_ist_now()

                _save_log(db, "INFO",
                    f"Position sync: trade {trade.id} ({trade.stock_name}) was closed on 5paisa by client, marking closed in database")

    finally:
        db.close()


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
