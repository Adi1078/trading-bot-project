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

        # Build a set of broker order IDs that are still open on 5paisa
        live_order_ids = set()
        for position in live_positions:
            order_id = str(position.get("OrderID", ""))
            net_qty = position.get("NetQty", 0)
            # Only consider positions that still have quantity (not yet squared off)
            if order_id and net_qty != 0:
                live_order_ids.add(order_id)

        # Check each of our open trades
        for trade in our_open_trades:
            if _is_closed_on_broker(trade, live_order_ids):
                trade.status = "closed"
                trade.close_reason = "manual"
                trade.closed_at = get_ist_now()

                _save_log(db, "INFO",
                    f"Position sync: trade {trade.id} ({trade.stock_name}) was closed on 5paisa by client, marking closed in database")

    finally:
        db.close()


def _is_closed_on_broker(trade: Trade, live_order_ids: set) -> bool:
    """
    Check if all legs of a trade are no longer active on 5paisa.
    Returns True only if ALL legs that were placed are now gone from 5paisa.
    """
    placed_order_ids = [
        oid for oid in [
            trade.futures_broker_order_id,
            trade.ce_broker_order_id,
            trade.pe_broker_order_id
        ]
        if oid is not None
    ]

    if not placed_order_ids:
        return False

    # If none of our placed orders are in the live positions anymore, trade is closed
    return not any(oid in live_order_ids for oid in placed_order_ids)
