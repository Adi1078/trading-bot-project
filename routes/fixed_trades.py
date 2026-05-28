from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from database import get_db
from models.fixed_trades import FixedTrade
from models.log import Log

router = APIRouter()


class FixedTradeRequest(BaseModel):
    stock_name: str
    scrip_code: Optional[str] = None
    strike_type: str        # "fixed" or "percent"
    strike_value: float
    profit_target: float
    loss_limit: float
    month_type: str         # "current", "next", "option"
    lot_size: int = 1
    is_trade: bool = True


@router.get("/")
def get_fixed_trades(db: Session = Depends(get_db)):
    """Get all active fixed trades (Number 3)."""
    trades = db.query(FixedTrade).filter(FixedTrade.is_active == True).all()
    return {"trades": [_fixed_trade_to_dict(t) for t in trades]}


@router.post("/add")
def add_fixed_trade(request: FixedTradeRequest, db: Session = Depends(get_db)):
    """Add a new row to the fixed trades table."""
    trade = FixedTrade(
        stock_name=request.stock_name.upper(),
        scrip_code=request.scrip_code,
        strike_type=request.strike_type,
        strike_value=request.strike_value,
        profit_target=request.profit_target,
        loss_limit=request.loss_limit,
        month_type=request.month_type,
        lot_size=request.lot_size,
        is_trade=request.is_trade
    )
    db.add(trade)

    log = Log(level="INFO", message=f"Fixed trade added for {request.stock_name}")
    db.add(log)
    db.commit()

    return {"success": True, "id": trade.id}


@router.put("/{trade_id}")
def edit_fixed_trade(trade_id: int, request: FixedTradeRequest, db: Session = Depends(get_db)):
    """Edit an existing fixed trade row."""
    trade = db.query(FixedTrade).filter(FixedTrade.id == trade_id, FixedTrade.is_active == True).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Fixed trade not found")

    trade.stock_name = request.stock_name.upper()
    trade.scrip_code = request.scrip_code
    trade.strike_type = request.strike_type
    trade.strike_value = request.strike_value
    trade.profit_target = request.profit_target
    trade.loss_limit = request.loss_limit
    trade.month_type = request.month_type
    trade.lot_size = request.lot_size
    trade.is_trade = request.is_trade

    log = Log(level="INFO", message=f"Fixed trade updated for {request.stock_name}")
    db.add(log)
    db.commit()

    return {"success": True}


@router.delete("/{trade_id}")
def delete_fixed_trade(trade_id: int, db: Session = Depends(get_db)):
    """Delete (soft delete) a fixed trade row."""
    trade = db.query(FixedTrade).filter(FixedTrade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Fixed trade not found")

    trade.is_active = False

    log = Log(level="INFO", message=f"Fixed trade deleted for {trade.stock_name}")
    db.add(log)
    db.commit()

    return {"success": True}


@router.post("/toggle/{trade_id}")
def toggle_trade(trade_id: int, db: Session = Depends(get_db)):
    """Toggle Trade Yes/No for a specific fixed trade."""
    trade = db.query(FixedTrade).filter(FixedTrade.id == trade_id, FixedTrade.is_active == True).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Fixed trade not found")

    trade.is_trade = not trade.is_trade
    status = "enabled" if trade.is_trade else "disabled"

    log = Log(level="INFO", message=f"Fixed trade {status} for {trade.stock_name}")
    db.add(log)
    db.commit()

    return {"success": True, "is_trade": trade.is_trade}


def _fixed_trade_to_dict(trade: FixedTrade):
    return {
        "id": trade.id,
        "stock_name": trade.stock_name,
        "scrip_code": trade.scrip_code,
        "strike_type": trade.strike_type,
        "strike_value": trade.strike_value,
        "profit_target": trade.profit_target,
        "loss_limit": trade.loss_limit,
        "month_type": trade.month_type,
        "lot_size": trade.lot_size or 1,
        "is_trade": trade.is_trade,
        "created_at": str(trade.created_at)
    }
