from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models.trade import Trade
from models.settings import Settings
from models.log import Log
from utils.helpers import get_ist_now, calculate_trade_pnl

router = APIRouter()


@router.get("/pnl")
def get_total_pnl(db: Session = Depends(get_db)):
    """
    Get P&L and trade counts, split into real-money and paper trades.
    The headline (total) P&L is real-money only — paper trades are reported
    separately and never mixed into the real numbers.
    """
    closed_trades = db.query(Trade).filter(Trade.status == "closed").all()
    open_trades = db.query(Trade).filter(Trade.status == "open").all()

    real_closed = [t for t in closed_trades if not t.is_paper_trade]
    paper_closed = [t for t in closed_trades if t.is_paper_trade]
    real_open = [t for t in open_trades if not t.is_paper_trade]
    paper_open = [t for t in open_trades if t.is_paper_trade]

    real_pnl = sum(t.pnl for t in real_closed if t.pnl is not None)
    paper_pnl = sum(t.pnl for t in paper_closed if t.pnl is not None)

    return {
        "total_pnl": round(real_pnl, 2),   # real-money only (paper excluded)
        "real_pnl": round(real_pnl, 2),
        "paper_pnl": round(paper_pnl, 2),
        "real_open_count": len(real_open),
        "real_closed_count": len(real_closed),
        "paper_open_count": len(paper_open),
        "paper_closed_count": len(paper_closed),
    }


@router.get("/live-trades")
def get_live_trades(db: Session = Depends(get_db)):
    """Get all currently open trades, each with its live (running) P&L."""
    trades = db.query(Trade).filter(Trade.status == "open").order_by(Trade.placed_at.desc()).all()
    pnl_map = _compute_live_pnl(db, trades)
    return {"trades": [_trade_to_dict(t, pnl_map.get(t.id)) for t in trades]}


def _compute_live_pnl(db, trades):
    """
    Compute the current running P&L for each open trade using one batched
    market-quote call for all legs. Returns {trade_id: pnl}. Empty if the broker
    is not connected or prices are unavailable.
    """
    pnl_map = {}
    if not trades:
        return pnl_map

    settings = db.query(Settings).first()
    if not settings or not settings.access_token:
        return pnl_map

    # Gather every distinct leg scrip code across all open trades into one request
    scrip_codes = set()
    for t in trades:
        for code in [t.futures_scrip_code, t.ce_scrip_code, t.pe_scrip_code]:
            if code:
                scrip_codes.add(str(code))
    if not scrip_codes:
        return pnl_map

    from broker.fivepaisa import get_market_quote
    scrip_list = [{"exchange": "N", "exchange_type": "D", "scrip_code": c} for c in scrip_codes]
    result = get_market_quote(settings.access_token, scrip_list)
    if not result["success"]:
        return pnl_map

    prices = {str(q["ScripCode"]): q["LastRate"] for q in result["quotes"]}
    for t in trades:
        cur_fut = prices.get(str(t.futures_scrip_code), t.futures_entry_price)
        cur_ce = prices.get(str(t.ce_scrip_code), t.ce_entry_price)
        cur_pe = prices.get(str(t.pe_scrip_code), t.pe_entry_price)
        pnl_map[t.id] = calculate_trade_pnl(
            t.futures_entry_price, cur_fut,
            t.ce_entry_price, cur_ce,
            t.pe_entry_price, cur_pe,
            lot_size=t.lot_size or 1
        )
    return pnl_map


@router.get("/trade-history")
def get_trade_history(db: Session = Depends(get_db)):
    """Get all closed trades."""
    trades = db.query(Trade).filter(Trade.status == "closed").order_by(Trade.closed_at.desc()).all()
    return {"trades": [_trade_to_dict(t) for t in trades]}


@router.post("/close-trade/{trade_id}")
def close_trade(trade_id: int, db: Session = Depends(get_db)):
    """Manually close a trade from the dashboard."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    if trade.status == "closed":
        return {"success": False, "error": "Trade is already closed"}

    settings = db.query(Settings).first()
    if not settings or not settings.access_token:
        return {"success": False, "error": "Broker not connected"}

    # Square off filled legs with opposite-side market orders (same as auto-close path)
    from bot.trade_manager import _fetch_current_prices, _square_off_legs, _actual_exit_fills
    if not trade.is_paper_trade:
        _square_off_legs(db, settings, trade)

    # P&L: prefer each leg's ACTUAL broker fill (net-position average rates); fall
    # back to the LTP only where the broker didn't report a fill (and for paper).
    fills = {} if trade.is_paper_trade else _actual_exit_fills(db, settings, trade)
    lt_fut, lt_ce, lt_pe = _fetch_current_prices(db, settings, trade) or (None, None, None)
    if trade.futures_scrip_code:
        trade.futures_exit_price = fills.get(str(trade.futures_scrip_code)) or lt_fut
    if trade.ce_scrip_code:
        trade.ce_exit_price = fills.get(str(trade.ce_scrip_code)) or lt_ce
    if trade.pe_scrip_code:
        trade.pe_exit_price = fills.get(str(trade.pe_scrip_code)) or lt_pe
    trade.pnl = calculate_trade_pnl(
        trade.futures_entry_price, trade.futures_exit_price,
        trade.ce_entry_price, trade.ce_exit_price,
        trade.pe_entry_price, trade.pe_exit_price,
        lot_size=trade.lot_size or 1
    )

    trade.status = "closed"
    trade.close_reason = "manual"
    trade.closed_at = get_ist_now()

    log = Log(level="INFO", message=f"Trade {trade_id} ({trade.stock_name}) manually closed from dashboard")
    db.add(log)
    db.commit()

    return {"success": True}


@router.post("/send-open-trades-report")
def send_open_trades_report(db: Session = Depends(get_db)):
    """
    Email the current open-trades summary right now (same report the 3:40 PM job
    sends). Sends to the configured notification email.
    """
    open_count = db.query(Trade).filter(Trade.status == "open").count()
    if open_count == 0:
        return {"success": True, "message": "No open trades right now — nothing to send"}

    try:
        from bot.trade_manager import safety_check
        count = safety_check(trigger="manual")
    except Exception as e:
        return {"success": False, "error": f"Email failed: {str(e)}"}

    return {"success": True, "message": f"Open-trades report ({count} trade(s)) sent — check inbox/spam"}


@router.post("/run-chartink-now")
def run_chartink_now(db: Session = Depends(get_db)):
    """
    Manually run the Chartink screeners and fire trades for watchlist-matched
    stocks right now. Bypasses the once-per-day attempted guard (force=True), so
    a stock that failed earlier (e.g. insufficient margin) can be retried.
    """
    from utils.exchange_calendar import is_trading_day
    if not is_trading_day():
        return {"success": False, "error": "Market is closed today (weekend/holiday)"}

    settings = db.query(Settings).first()
    if not settings or not settings.is_trading:
        return {"success": False, "error": "Turn on the Trade switch first"}
    if not settings.access_token:
        return {"success": False, "error": "Broker not connected"}

    import threading
    def _run():
        from bot.trade_manager import run_chartink_cycle
        run_chartink_cycle(force=True)
    threading.Thread(target=_run, daemon=True).start()

    log = Log(level="INFO", message="Chartink trades triggered manually by user")
    db.add(log)
    db.commit()
    return {"success": True, "message": "Chartink trades triggered — check logs"}


@router.post("/toggle-trading")
def toggle_trading(db: Session = Depends(get_db)):
    """Toggle the master Trade / Stop Trade switch."""
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings(is_trading=False)
        db.add(settings)

    settings.is_trading = not settings.is_trading
    status = "started" if settings.is_trading else "stopped"

    log = Log(level="INFO", message=f"Trading {status} by user")
    db.add(log)
    db.commit()

    return {"is_trading": settings.is_trading}


@router.get("/trading-status")
def get_trading_status(db: Session = Depends(get_db)):
    """Get current Trade/Stop Trade status."""
    settings = db.query(Settings).first()
    return {"is_trading": settings.is_trading if settings else False}


def _trade_to_dict(trade: Trade, live_pnl=None):
    return {
        "id": trade.id,
        "stock_name": trade.stock_name,
        "trade_source": trade.trade_source,
        "is_paper_trade": trade.is_paper_trade,
        "month_type": trade.month_type,
        "futures_entry_price": trade.futures_entry_price,
        "ce_entry_price": trade.ce_entry_price,
        "pe_entry_price": trade.pe_entry_price,
        "futures_exit_price": trade.futures_exit_price,
        "ce_exit_price": trade.ce_exit_price,
        "pe_exit_price": trade.pe_exit_price,
        "profit_target": trade.profit_target,
        "loss_limit": trade.loss_limit,
        "status": trade.status,
        "close_reason": trade.close_reason,
        "pnl": trade.pnl,
        "live_pnl": live_pnl,
        "placed_at": str(trade.placed_at) if trade.placed_at else None,
        "closed_at": str(trade.closed_at) if trade.closed_at else None
    }
