from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from models.watchlist import Watchlist
from models.log import Log
from broker.fivepaisa import get_scrip_master

router = APIRouter()


class AddStockRequest(BaseModel):
    stock_name: str
    scrip_code: str
    exchange: str = "N"
    exchange_type: str = "D"


@router.get("/")
def get_watchlist(db: Session = Depends(get_db)):
    """Get all stocks in the watchlist (Number 2)."""
    stocks = db.query(Watchlist).all()
    return {
        "stocks": [
            {
                "id": s.id,
                "stock_name": s.stock_name,
                "scrip_code": s.scrip_code,
                "exchange": s.exchange,
                "exchange_type": s.exchange_type,
                "added_at": str(s.added_at)
            }
            for s in stocks
        ]
    }


@router.post("/add")
def add_stock(request: AddStockRequest, db: Session = Depends(get_db)):
    """Add a stock to the watchlist."""
    existing = db.query(Watchlist).filter(Watchlist.stock_name.ilike(request.stock_name)).first()
    if existing:
        return {"success": False, "error": f"{request.stock_name} is already in the watchlist"}

    stock = Watchlist(
        stock_name=request.stock_name.upper(),
        scrip_code=request.scrip_code,
        exchange=request.exchange,
        exchange_type=request.exchange_type
    )
    db.add(stock)

    log = Log(level="INFO", message=f"Stock {request.stock_name} added to watchlist")
    db.add(log)
    db.commit()

    return {"success": True, "id": stock.id}


@router.delete("/{stock_id}")
def remove_stock(stock_id: int, db: Session = Depends(get_db)):
    """Remove a stock from the watchlist."""
    stock = db.query(Watchlist).filter(Watchlist.id == stock_id).first()
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    stock_name = stock.stock_name
    db.delete(stock)

    log = Log(level="INFO", message=f"Stock {stock_name} removed from watchlist")
    db.add(log)
    db.commit()

    return {"success": True}


@router.get("/search")
def search_stocks(query: str):
    """Search 5paisa instrument list for autocomplete when adding stocks."""
    if len(query) < 2:
        return {"success": False, "instruments": [], "error": "Query too short"}

    result = get_scrip_master()
    if not result["success"]:
        return {"success": False, "instruments": [], "error": result["error"]}

    query_lower = query.lower()

    # Cash equity series only (ExchType "C", Series "EQ"). In 5paisa's data this
    # keeps stocks AND indices (NIFTY/BANKNIFTY are tagged EQ) while dropping bonds/
    # NCDs (series NI/NP/NQ/...) and F&O contract rows.
    eq = [
        inst for inst in result["instruments"]
        if inst.get("ExchType") == "C" and inst.get("Series") == "EQ"
        and (query_lower in inst.get("Symbol", "").lower()
             or query_lower in inst.get("FullName", "").lower())
    ]

    # De-duplicate by symbol (keep first occurrence).
    by_symbol = {}
    for inst in eq:
        name = inst.get("Symbol", "")
        if name not in by_symbol:
            by_symbol[name] = inst

    # Rank by relevance so an exact/prefix symbol match (e.g. "NIFTY") sits at the
    # top instead of being buried under ETFs that merely contain the text.
    def _rank(inst):
        sym = inst.get("Symbol", "").lower()
        if sym == query_lower:
            return 0
        if sym.startswith(query_lower):
            return 1
        if query_lower in sym:
            return 2
        return 3

    matched = sorted(by_symbol.values(), key=_rank)[:20]
    return {"success": True, "instruments": matched}


@router.get("/check/{stock_name}")
def check_in_watchlist(stock_name: str, db: Session = Depends(get_db)):
    """Check if a stock is in the watchlist."""
    stock = db.query(Watchlist).filter(Watchlist.stock_name.ilike(stock_name)).first()
    return {"in_watchlist": stock is not None, "stock": stock.stock_name if stock else None}
