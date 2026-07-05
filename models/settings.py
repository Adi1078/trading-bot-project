from sqlalchemy import Column, Integer, String, Boolean, Float
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
    totp_secret = Column(String, nullable=True)   # TOTP secret for automated daily login
    login_pin = Column(String, nullable=True)     # 5paisa login PIN for automated login
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
    webhook_is_paper = Column(Boolean, default=False)       # paper mode for Chartink screener trades
    screener_1_url = Column(String, nullable=True)          # Chartink screener 1 URL (for reference)
    screener_1_clause = Column(String, nullable=True)       # Screener 1 scan_clause (the payload)
    screener_2_url = Column(String, nullable=True)
    screener_2_clause = Column(String, nullable=True)
    screener_3_url = Column(String, nullable=True)
    screener_3_clause = Column(String, nullable=True)
    ui_username = Column(String, nullable=True)        # login page username (client-set)
    ui_password_hash = Column(String, nullable=True)   # PBKDF2 hash of login password
    ui_session_secret = Column(String, nullable=True)  # signing key for the trusted-device cookie

    # Manual adjustment for the displayed REAL-money P&L only (never paper). The
    # dashboard shows (sum of real closed-trade P&L) + this number, so the client can
    # type the total they want shown (to match the broker) and future closed trades
    # keep adding/subtracting from it. Display-only; never touches trade records.
    manual_pnl_adjustment = Column(Float, default=0.0)
