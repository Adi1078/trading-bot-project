from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from database import get_db
from models.settings import Settings
from models.log import Log

router = APIRouter()


class SettingsRequest(BaseModel):
    app_key: Optional[str] = None
    encry_key: Optional[str] = None
    user_id: Optional[str] = None
    algo_id: Optional[str] = None
    notification_email: Optional[str] = None
    trade_start_time: Optional[str] = None
    trade_close_time: Optional[str] = None
    webhook_trade_type: Optional[str] = None
    webhook_strike_type: Optional[str] = None
    webhook_strike_value: Optional[float] = None
    webhook_lot_size: Optional[int] = None
    webhook_month_type: Optional[str] = None
    webhook_profit_target: Optional[int] = None
    webhook_loss_limit: Optional[int] = None
    webhook_is_paper: Optional[bool] = None
    screener_1_url: Optional[str] = None
    screener_1_clause: Optional[str] = None
    screener_2_url: Optional[str] = None
    screener_2_clause: Optional[str] = None
    screener_3_url: Optional[str] = None
    screener_3_clause: Optional[str] = None


@router.get("/")
def get_settings(db: Session = Depends(get_db)):
    """Get current settings. Credentials are masked for security."""
    settings = db.query(Settings).first()
    if not settings:
        return {"settings": {}}

    return {
        "settings": {
            "app_key": _mask(settings.app_key),
            "encry_key": _mask(settings.encry_key),
            "user_id": _mask(settings.user_id),
            "algo_id": settings.algo_id or "",
            "notification_email": settings.notification_email or "",
            "trade_start_time": settings.trade_start_time or "09:30",
            "trade_close_time": settings.trade_close_time or "12:00",
            "webhook_trade_type": settings.webhook_trade_type or "collar",
            "webhook_strike_type": settings.webhook_strike_type or "percent",
            "webhook_strike_value": settings.webhook_strike_value or 2,
            "webhook_lot_size": settings.webhook_lot_size or 1,
            "webhook_month_type": settings.webhook_month_type or "current",
            "webhook_profit_target": settings.webhook_profit_target or 15000,
            "webhook_loss_limit": settings.webhook_loss_limit or 12000,
            "webhook_is_paper": bool(getattr(settings, "webhook_is_paper", False)),
            "screener_1_url": settings.screener_1_url or "",
            "screener_1_clause": settings.screener_1_clause or "",
            "screener_2_url": settings.screener_2_url or "",
            "screener_2_clause": settings.screener_2_clause or "",
            "screener_3_url": settings.screener_3_url or "",
            "screener_3_clause": settings.screener_3_clause or "",
            "is_trading": settings.is_trading
        }
    }


@router.post("/save")
def save_settings(request: SettingsRequest, db: Session = Depends(get_db)):
    """Save broker credentials and app settings."""
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)

    if request.app_key:
        settings.app_key = request.app_key
    if request.encry_key:
        settings.encry_key = request.encry_key
    if request.user_id:
        settings.user_id = request.user_id
    if request.algo_id:
        settings.algo_id = request.algo_id
    if request.notification_email:
        settings.notification_email = request.notification_email
    if request.trade_start_time:
        settings.trade_start_time = request.trade_start_time
    if request.trade_close_time:
        settings.trade_close_time = request.trade_close_time
    if request.webhook_trade_type is not None:
        settings.webhook_trade_type = request.webhook_trade_type
    if request.webhook_strike_type is not None:
        settings.webhook_strike_type = request.webhook_strike_type
    if request.webhook_strike_value is not None:
        settings.webhook_strike_value = request.webhook_strike_value
    if request.webhook_lot_size is not None:
        settings.webhook_lot_size = request.webhook_lot_size
    if request.webhook_month_type is not None:
        settings.webhook_month_type = request.webhook_month_type
    if request.webhook_profit_target is not None:
        settings.webhook_profit_target = request.webhook_profit_target
    if request.webhook_loss_limit is not None:
        settings.webhook_loss_limit = request.webhook_loss_limit
    if request.webhook_is_paper is not None:
        settings.webhook_is_paper = request.webhook_is_paper
    if request.screener_1_url is not None:
        settings.screener_1_url = request.screener_1_url
    if request.screener_1_clause is not None:
        settings.screener_1_clause = request.screener_1_clause
    if request.screener_2_url is not None:
        settings.screener_2_url = request.screener_2_url
    if request.screener_2_clause is not None:
        settings.screener_2_clause = request.screener_2_clause
    if request.screener_3_url is not None:
        settings.screener_3_url = request.screener_3_url
    if request.screener_3_clause is not None:
        settings.screener_3_clause = request.screener_3_clause

    log = Log(level="INFO", message="Settings updated")
    db.add(log)
    db.commit()

    return {"success": True}


@router.post("/clear-credentials")
def clear_credentials(db: Session = Depends(get_db)):
    """Clear all broker credentials from the database."""
    settings = db.query(Settings).first()
    if not settings:
        return {"success": True}

    settings.app_key = None
    settings.encry_key = None
    settings.user_id = None
    settings.algo_id = None

    log = Log(level="INFO", message="Credentials cleared")
    db.add(log)
    db.commit()

    return {"success": True}


def _mask(value: str):
    """Mask sensitive credential values."""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]
