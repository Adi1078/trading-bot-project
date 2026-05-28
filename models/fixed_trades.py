from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime
from datetime import datetime
from database import Base


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
    lot_size = Column(Integer, default=1)
    is_trade = Column(Boolean, default=True)       # False = paper trade
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
