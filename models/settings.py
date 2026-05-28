from sqlalchemy import Column, Integer, String, Boolean
from database import Base


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    app_key = Column(String, nullable=True)
    encry_key = Column(String, nullable=True)
    user_id = Column(String, nullable=True)
    algo_id = Column(String, nullable=True)
    access_token = Column(String, nullable=True)
    client_code = Column(String, nullable=True)
    token_date = Column(String, nullable=True)
    notification_email = Column(String, nullable=True)
    trade_start_time = Column(String, default="09:30")
    trade_close_time = Column(String, default="12:00")
    is_trading = Column(Boolean, default=False)
    webhook_trade_type = Column(String, default="collar")   # "collar" or "option"
    webhook_strike_type = Column(String, default="percent") # "fixed" or "percent"
    webhook_strike_value = Column(Integer, default=2)
    webhook_lot_size = Column(Integer, default=1)
    webhook_month_type = Column(String, default="current")  # "current" or "next"
    webhook_profit_target = Column(Integer, default=15000)
    webhook_loss_limit = Column(Integer, default=12000)
