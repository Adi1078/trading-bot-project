from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime
from database import Base
from utils.helpers import get_ist_now


class FixedTrade(Base):
    __tablename__ = "fixed_trades"

    id = Column(Integer, primary_key=True, index=True)
    stock_name = Column(String, nullable=False)
    scrip_code = Column(String, nullable=True)
    strike_type = Column(String, nullable=False)   # "fixed" or "percent"
    strike_value = Column(Float, nullable=False)
    profit_target = Column(Float, nullable=False)
    loss_limit = Column(Float, nullable=False)
    month_type = Column(String, nullable=False)    # "current", "next", "option"
    # Expiry for the naked-CE path only (month_type == "option"). The single
    # month_type field is consumed by the value "option" itself, so it has no room
    # to also say current/next — this carries that choice. "current" | "next".
    option_expiry = Column(String, default="current")
    lot_size = Column(Integer, default=1)
    is_trade = Column(Boolean, default=True)       # False = paper trade
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=get_ist_now)
    updated_at = Column(DateTime, default=get_ist_now, onupdate=get_ist_now)
