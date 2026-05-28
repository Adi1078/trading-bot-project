import threading
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from database import get_db
from models.watchlist import Watchlist
from models.settings import Settings
from models.log import Log

router = APIRouter()


def _fire_webhook_trade(stock_name: str):
    """Run in background thread so Chartink gets an immediate HTTP 200."""
    try:
        from bot.trade_manager import run_webhook_trade
        run_webhook_trade(stock_name)
    except Exception as e:
        from database import SessionLocal
        db = SessionLocal()
        try:
            log = Log(level="ERROR", message=f"Webhook background trade failed for {stock_name}: {str(e)}")
            db.add(log)
            db.commit()
        finally:
            db.close()


@router.post("/chartink")
async def chartink_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive Chartink webhook signal.
    Chartink sends stock symbols when screener conditions are met.
    We check each against the watchlist before firing a collar trade.
    """
    try:
        data = await request.json()
    except Exception:
        return {"success": False, "error": "Invalid payload received"}

    log = Log(level="INFO", message=f"Chartink webhook received: {data}")
    db.add(log)
    db.commit()

    settings = db.query(Settings).first()
    if not settings or not settings.is_trading:
        log = Log(level="INFO", message="Webhook received but trading is stopped, ignoring")
        db.add(log)
        db.commit()
        return {"success": False, "error": "Trading is stopped"}

    if not settings.access_token:
        log = Log(level="ERROR", message="Webhook received but broker is not connected")
        db.add(log)
        db.commit()
        return {"success": False, "error": "Broker not connected"}

    # Extract stock names — Chartink sends comma-separated string or list
    stocks = data.get("stocks", "")
    if isinstance(stocks, str):
        stock_list = [s.strip().upper() for s in stocks.split(",") if s.strip()]
    elif isinstance(stocks, list):
        stock_list = [s.strip().upper() for s in stocks if s.strip()]
    else:
        stock_list = []

    if not stock_list:
        return {"success": False, "error": "No stocks found in webhook payload"}

    triggered = []
    skipped = []

    for stock_name in stock_list:
        in_watchlist = db.query(Watchlist).filter(Watchlist.stock_name.ilike(stock_name)).first()

        if not in_watchlist:
            log = Log(level="INFO", message=f"Webhook: {stock_name} not in watchlist, skipping")
            db.add(log)
            skipped.append(stock_name)
            continue

        log = Log(level="INFO", message=f"Webhook: {stock_name} in watchlist — firing collar trade")
        db.add(log)
        triggered.append(stock_name)

        # Fire trade in background so Chartink doesn't time out waiting for us
        t = threading.Thread(target=_fire_webhook_trade, args=(stock_name,), daemon=True)
        t.start()

    db.commit()
    return {"success": True, "triggered": triggered, "skipped": skipped}


@router.post("/test")
async def test_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Manual test endpoint — simulates a Chartink signal for a given stock.
    Use from the admin panel to verify the full webhook→trade flow.
    """
    try:
        data = await request.json()
    except Exception:
        return {"success": False, "error": "Invalid payload"}

    stock_name = (data.get("stock_name") or "").strip().upper()
    if not stock_name:
        return {"success": False, "error": "stock_name is required"}

    # Reuse the real webhook logic by building a Chartink-style payload
    fake_payload = {"stocks": stock_name}
    from fastapi.requests import Request as FRequest
    import json

    log = Log(level="INFO", message=f"Manual webhook test triggered for {stock_name}")
    db.add(log)

    settings = db.query(Settings).first()
    if not settings or not settings.is_trading:
        db.commit()
        return {"success": False, "error": "Trading is stopped — turn on the Trade switch first"}

    if not settings.access_token:
        db.commit()
        return {"success": False, "error": "Broker not connected"}

    in_watchlist = db.query(Watchlist).filter(Watchlist.stock_name.ilike(stock_name)).first()
    if not in_watchlist:
        db.commit()
        return {"success": False, "error": f"{stock_name} is not in your watchlist"}

    db.commit()

    t = threading.Thread(target=_fire_webhook_trade, args=(stock_name,), daemon=True)
    t.start()

    return {"success": True, "message": f"Webhook trade fired for {stock_name} — check logs for result"}
