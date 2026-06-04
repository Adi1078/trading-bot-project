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
    """Get total P&L and trade counts."""
    closed_trades = db.query(Trade).filter(Trade.status == "closed").all()
    open_trades = db.query(Trade).filter(Trade.status == "open").all()
    total_pnl = sum(t.pnl for t in closed_trades if t.pnl is not None)

    return {
        "total_pnl": round(total_pnl, 2),
        "open_trades_count": len(open_trades),
        "closed_trades_count": len(closed_trades)
    }


@router.get("/live-trades")
def get_live_trades(db: Session = Depends(get_db)):
    """Get all currently open trades."""
    trades = db.query(Trade).filter(Trade.status == "open").order_by(Trade.placed_at.desc()).all()
    return {"trades": [_trade_to_dict(t) for t in trades]}


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
    from bot.trade_manager import _fetch_current_prices, _square_off_legs
    if not trade.is_paper_trade:
        _square_off_legs(db, settings, trade)

    # Compute P&L from current market prices before closing
    current_prices = _fetch_current_prices(db, settings, trade)
    if current_prices:
        trade.futures_exit_price, trade.ce_exit_price, trade.pe_exit_price = current_prices
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


@router.post("/run-fixed-trades-now")
def run_fixed_trades_now(db: Session = Depends(get_db)):
    """Manually trigger fixed trades right now — for testing only."""
    settings = db.query(Settings).first()
    if not settings or not settings.is_trading:
        return {"success": False, "error": "Turn on the Trade switch first"}
    if not settings.access_token:
        return {"success": False, "error": "Broker not connected"}

    import threading
    def _run():
        from bot.trade_manager import run_fixed_trades
        run_fixed_trades()
    threading.Thread(target=_run, daemon=True).start()

    log = Log(level="INFO", message="Fixed trades triggered manually by user")
    db.add(log)
    db.commit()
    return {"success": True, "message": "Fixed trades triggered — check logs"}


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


def _trade_to_dict(trade: Trade):
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
        "placed_at": str(trade.placed_at) if trade.placed_at else None,
        "closed_at": str(trade.closed_at) if trade.closed_at else None
    }
