"""
End-to-end mock test of the complete trading flow.
Fixed trades → watchlist check → strike calc → order placement → P&L monitor → close.
All 5paisa broker calls are mocked — no real credentials needed.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models.watchlist import Watchlist
from models.settings import Settings
from models.fixed_trades import FixedTrade
from models.trade import Trade
from models.log import Log
from bot import trade_manager


TEST_DB_URL = "sqlite:///./test_full_flow.db"
engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


# ── Object Factories ──────────────────────────────────────────────────────────

def add(*objects):
    db = TestSession()
    for obj in objects:
        db.add(obj)
    db.commit()
    db.close()


def make_settings(is_trading=True, access_token="fake_token", trade_close_time="15:00"):
    return Settings(
        is_trading=is_trading,
        access_token=access_token,
        user_id="TEST001",
        trade_close_time=trade_close_time,
    )


def make_watchlist(name="INFY", code="1660"):
    return Watchlist(stock_name=name, scrip_code=code, exchange="N", exchange_type="D")


def make_fixed_trade(name="INFY", strike_type="percent", strike_value=2,
                     month_type="current", is_trade=True):
    return FixedTrade(
        stock_name=name, scrip_code="1660",
        strike_type=strike_type, strike_value=strike_value,
        profit_target=20, loss_limit=25,
        month_type=month_type, is_trade=is_trade, is_active=True,
    )


def make_open_trade(name="INFY", profit_target=20, loss_limit=25):
    return Trade(
        stock_name=name,
        trade_source="fixed",
        is_paper_trade=False,
        month_type="current",
        futures_scrip_code="INFY_FUT",
        ce_scrip_code="INFY_CE_1150",
        pe_scrip_code="INFY_PE_1100",
        futures_broker_order_id="111",
        ce_broker_order_id="222",
        pe_broker_order_id="333",
        futures_entry_price=1126.3,
        ce_entry_price=13.85,
        pe_entry_price=11.1,
        profit_target=profit_target,
        loss_limit=loss_limit,
        status="open",
    )


# ── Mock Broker Responses ──────────────────────────────────────────────────────

def quote_ok(price, scrip="1660"):
    return {"success": True, "quotes": [{"ScripCode": scrip, "LastRate": price}]}


def multi_quote_ok(fut, ce, pe):
    return {
        "success": True,
        "quotes": [
            {"ScripCode": "INFY_FUT", "LastRate": fut},
            {"ScripCode": "INFY_CE_1150", "LastRate": ce},
            {"ScripCode": "INFY_PE_1100", "LastRate": pe},
        ],
    }


def chain_ok(ce_strike=1150):
    return {
        "success": True,
        "option_chain": [
            {"StrikeRate": ce_strike, "CPType": "CE", "LastRate": 13.85, "Scripcode": "55001"},
            {"StrikeRate": ce_strike - 50, "CPType": "PE", "LastRate": 11.1, "Scripcode": "55002"},
            {"StrikeRate": ce_strike - 100, "CPType": "PE", "LastRate": 8.5, "Scripcode": "55003"},
        ],
    }


def order_ok(oid):
    return {"success": True, "broker_order_id": oid}


def cancel_ok():
    return {"success": True}


# ── Fixed Trade Flow ──────────────────────────────────────────────────────────

@patch("bot.trade_manager.fivepaisa")
def test_collar_trade_placed_and_saved(mock_broker):
    """
    Full collar: run_fixed_trades() → 3 orders placed → trade row saved.
    INFY futures at 1126.3 + 2% = 1148.826 → rounds up to CE strike 1150.
    """
    add(make_settings(), make_watchlist(), make_fixed_trade())

    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1150)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400
    mock_broker.place_order.side_effect = [order_ok(111), order_ok(222), order_ok(333)]

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_trade_opened_email"):
            trade_manager.run_fixed_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade is not None, "Trade should have been saved to the database"
    assert trade.stock_name == "INFY"
    assert trade.status == "open"
    assert trade.futures_entry_price == 1126.3
    assert trade.ce_entry_price == 13.85
    assert trade.pe_entry_price == 11.1
    assert trade.futures_broker_order_id == "111"
    assert trade.ce_broker_order_id == "222"
    assert trade.pe_broker_order_id == "333"
    assert trade.is_paper_trade is False
    assert mock_broker.place_order.call_count == 3


@patch("bot.trade_manager.fivepaisa")
def test_fixed_trade_skips_stock_not_in_watchlist(mock_broker):
    """Stock in fixed trades but not in watchlist → no order placed, skip logged."""
    add(
        make_settings(),
        FixedTrade(
            stock_name="RELIANCE", scrip_code="500325",
            strike_type="percent", strike_value=2,
            profit_target=20, loss_limit=25,
            month_type="current", is_trade=True, is_active=True,
        ),
    )

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_fixed_trades()

    db = TestSession()
    count = db.query(Trade).count()
    skip_log = db.query(Log).filter(Log.message.contains("not in watchlist")).first()
    db.close()

    assert count == 0
    assert skip_log is not None


@patch("bot.trade_manager.fivepaisa")
def test_fixed_trade_skips_when_trading_stopped(mock_broker):
    """Master trading switch is OFF → no orders sent."""
    add(make_settings(is_trading=False), make_watchlist(), make_fixed_trade())

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_fixed_trades()

    db = TestSession()
    count = db.query(Trade).count()
    db.close()

    assert count == 0
    mock_broker.place_order.assert_not_called()


@patch("bot.trade_manager.fivepaisa")
def test_fixed_trade_skips_duplicate_open_trade(mock_broker):
    """If an open trade already exists for the same stock, a second run does nothing."""
    add(make_settings(), make_watchlist(), make_fixed_trade(), make_open_trade())

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_fixed_trades()

    db = TestSession()
    count = db.query(Trade).count()
    db.close()

    assert count == 1  # only the pre-seeded trade, no new one
    mock_broker.place_order.assert_not_called()


@patch("bot.trade_manager.fivepaisa")
def test_paper_trade_records_without_placing_orders(mock_broker):
    """is_trade=False → paper trade row saved, zero broker orders sent."""
    add(make_settings(), make_watchlist(), make_fixed_trade(is_trade=False))
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1150)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_fixed_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade is not None
    assert trade.is_paper_trade is True
    assert trade.status == "open"
    assert trade.futures_entry_price == 1126.3
    mock_broker.place_order.assert_not_called()


# ── Webhook Trade Flow ────────────────────────────────────────────────────────

@patch("bot.trade_manager.fivepaisa")
def test_webhook_trade_places_collar(mock_broker):
    """run_webhook_trade() for a watchlisted stock places all 3 legs."""
    add(make_settings(), make_watchlist("ONGC", "500312"))

    mock_broker.get_market_quote.return_value = quote_ok(280.5, "500312")
    # ONGC at 280.5 + 2% = 286.11 → next 50 = 300
    mock_broker.get_option_chain.return_value = chain_ok(300)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400
    mock_broker.place_order.side_effect = [order_ok(201), order_ok(202), order_ok(203)]

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_webhook_trade("ONGC")

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade is not None
    assert trade.stock_name == "ONGC"
    assert trade.trade_source == "webhook"
    assert trade.status == "open"
    assert mock_broker.place_order.call_count == 3


@patch("bot.trade_manager.fivepaisa")
def test_webhook_skips_when_trading_stopped(mock_broker):
    """Webhook trade ignored when master switch is OFF."""
    add(make_settings(is_trading=False))

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_webhook_trade("ONGC")

    mock_broker.place_order.assert_not_called()


@patch("bot.trade_manager.fivepaisa")
def test_webhook_skips_existing_open_trade(mock_broker):
    """Webhook won't open a second trade for the same stock."""
    add(make_settings(), make_watchlist("ONGC"), make_open_trade("ONGC"))

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_webhook_trade("ONGC")

    db = TestSession()
    count = db.query(Trade).count()
    db.close()

    assert count == 1
    mock_broker.place_order.assert_not_called()


# ── Monitor & Close ───────────────────────────────────────────────────────────

@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_monitor_closes_on_profit(mock_now, mock_broker):
    """
    P&L exceeds profit target → trade closed with reason 'profit'.
    Exit: fut=1160, CE=5.0, PE=8.0
    P&L = (1160-1126.3) + (13.85-5.0) + (8.0-11.1) = 33.7 + 8.85 - 3.1 = 39.45 > target 20
    """
    add(make_settings(), make_open_trade(profit_target=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)
    mock_broker.cancel_order.return_value = cancel_ok()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_trade_closed_email"):
            trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "closed"
    assert trade.close_reason == "profit"
    assert trade.pnl > 20


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_monitor_closes_on_loss(mock_now, mock_broker):
    """
    P&L exceeds loss limit → trade closed with reason 'loss'.
    Exit: fut=1100, CE=22.0, PE=8.0
    P&L = (1100-1126.3) + (13.85-22.0) + (8.0-11.1) = -26.3 - 8.15 - 3.1 = -37.55 < -25
    """
    add(make_settings(), make_open_trade(loss_limit=25))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1100, 22.0, 8.0)
    mock_broker.cancel_order.return_value = cancel_ok()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_trade_closed_email"):
            trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "closed"
    assert trade.close_reason == "loss"
    assert trade.pnl < -25


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 28, 12, 30))
def test_monitor_closes_on_expiry_day(mock_now, mock_broker):
    """At 12:00+ on the trade's own expiry date → force-closed with reason 'expiry'."""
    trade = make_open_trade()
    trade.expiry_date = "2026-05-28"
    add(make_settings(), trade)
    mock_broker.cancel_order.return_value = cancel_ok()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_trade_closed_email"):
            trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "closed"
    assert trade.close_reason == "expiry"


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_monitor_holds_open_when_within_limits(mock_now, mock_broker):
    """
    P&L within limits → trade stays open, no cancel called.
    Exit: fut=1130, CE=12.0, PE=10.0
    P&L = (1130-1126.3) + (13.85-12.0) + (10.0-11.1) = 3.7 + 1.85 - 1.1 = 4.45
    4.45 is between -25 and 20 → no close.
    """
    add(make_settings(), make_open_trade(profit_target=20, loss_limit=25))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1130, 12.0, 10.0)

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "open"
    mock_broker.cancel_order.assert_not_called()


# ── Safety Check ──────────────────────────────────────────────────────────────

@patch("bot.trade_manager.fivepaisa")
def test_safety_check_emails_open_trades_without_stopping(mock_broker):
    """Open trades at 3:40 PM → summary emailed, trading is NOT stopped."""
    add(make_settings(is_trading=True), make_open_trade())
    mock_broker.get_market_quote.return_value = multi_quote_ok(1130, 12.0, 10.0)

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_safety_alert_email") as mock_email:
            trade_manager.safety_check()

    db = TestSession()
    settings = db.query(Settings).first()
    log = db.query(Log).filter(Log.message.contains("Daily 3:40 PM summary")).first()
    db.close()

    assert settings.is_trading is True   # trading must NOT be stopped
    assert log is not None
    mock_email.assert_called_once()


def test_safety_check_does_nothing_with_no_open_trades():
    """No open trades → is_trading stays True, no action taken."""
    add(make_settings(is_trading=True))

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.safety_check()

    db = TestSession()
    settings = db.query(Settings).first()
    db.close()

    assert settings.is_trading is True
