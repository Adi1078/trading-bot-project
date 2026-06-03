from sqlalchemy import Column, Integer, String, DateTime
from database import Base
from utils.helpers import get_ist_now


class Watchlist(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    stock_name = Column(String, nullable=False)
    scrip_code = Column(String, nullable=True)
    exchange = Column(String, default="N")
    exchange_type = Column(String, default="D")
    added_at = Column(DateTime, default=get_ist_now)
