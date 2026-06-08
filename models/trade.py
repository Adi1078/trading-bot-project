from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime
from database import Base
from utils.helpers import get_ist_now


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    stock_name = Column(String, nullable=False)
    trade_source = Column(String, nullable=False)         # "fixed" or "webhook"
    fixed_trade_id = Column(Integer, nullable=True)
    is_paper_trade = Column(Boolean, default=False)
    month_type = Column(String, nullable=True)

    futures_scrip_code = Column(String, nullable=True)
    ce_scrip_code = Column(String, nullable=True)
    pe_scrip_code = Column(String, nullable=True)

    futures_broker_order_id = Column(String, nullable=True)
    ce_broker_order_id = Column(String, nullable=True)
    pe_broker_order_id = Column(String, nullable=True)

    futures_entry_price = Column(Float, nullable=True)
    ce_entry_price = Column(Float, nullable=True)
    pe_entry_price = Column(Float, nullable=True)

    futures_exit_price = Column(Float, nullable=True)
    ce_exit_price = Column(Float, nullable=True)
    pe_exit_price = Column(Float, nullable=True)

    lot_size = Column(Integer, default=1)
    profit_target = Column(Float, nullable=False)
    loss_limit = Column(Float, nullable=False)
    expiry_date = Column(String, nullable=True)            # "YYYY-MM-DD" — force-closed at 12:00 PM on this date

    status = Column(String, default="open")               # "open" or "closed"
    close_reason = Column(String, nullable=True)          # "profit", "loss", "manual", "expiry", "time"

    pnl = Column(Float, nullable=True)
    placed_at = Column(DateTime, default=get_ist_now)
    closed_at = Column(DateTime, nullable=True)
