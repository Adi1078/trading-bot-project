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


def fill_ok():
    """Simulates get_order_status response: order fully executed on the exchange."""
    return {"success": True, "orders": [{"Status": "Fully Executed"}]}


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
    mock_broker.get_order_status.return_value = fill_ok()  # all legs fully executed

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_trade_opened_email"):
            with patch("bot.trade_manager.time"):  # skip the 1-second sleep in tests
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
    mock_broker.get_order_status.return_value = fill_ok()  # all legs fully executed

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.time"):  # skip the 1-second sleep in tests
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
    """At/after the configured close time on the trade's expiry date → force-closed."""
    trade = make_open_trade()
    trade.expiry_date = "2026-05-28"
    add(make_settings(trade_close_time="12:00"), trade)  # close time 12:00, now 12:30 -> close
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
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 28, 12, 30))
def test_monitor_does_not_close_before_close_time_on_expiry_day(mock_now, mock_broker):
    """On the expiry date but BEFORE the configured close time → stays open."""
    trade = make_open_trade()
    trade.expiry_date = "2026-05-28"
    add(make_settings(trade_close_time="15:00"), trade)  # close time 15:00, now 12:30 -> stay open
    # Live prices that are NOT at target/SL, so only the expiry rule could close it.
    mock_broker.get_market_quote.return_value = multi_quote_ok(1120, 11.0, 10.0)

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "open"   # not yet 15:00, must remain open


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 27, 15, 45))
def test_monitor_does_not_close_before_expiry_day(mock_now, mock_broker):
    """Before the expiry date — even past the close time — must NOT close on expiry rule."""
    trade = make_open_trade()
    trade.expiry_date = "2026-05-28"   # expiry is tomorrow
    add(make_settings(trade_close_time="12:00"), trade)
    mock_broker.get_market_quote.return_value = multi_quote_ok(1120, 11.0, 10.0)

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "open"   # not the expiry date yet


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
    log = db.query(Log).filter(Log.message.contains("Open-trades summary")).first()
    db.close()

    assert settings.is_trading is True   # trading must NOT be stopped
    assert log is not None
    mock_email.assert_called_once()


def test_safety_check_does_nothing_with_no_open_trades():
    """No open trades → is_trading stays True, no action taken, returns 0."""
    add(make_settings(is_trading=True))

    with patch("bot.trade_manager.SessionLocal", TestSession):
        result = trade_manager.safety_check()

    db = TestSession()
    settings = db.query(Settings).first()
    db.close()

    assert settings.is_trading is True
    assert result == 0


@patch("bot.trade_manager.fivepaisa")
def test_safety_check_returns_count_for_manual_report(mock_broker):
    """Manual 'Email Report Now' → safety_check returns the open-trade count."""
    add(make_settings(is_trading=True), make_open_trade())
    mock_broker.get_market_quote.return_value = multi_quote_ok(1130, 12.0, 10.0)

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_safety_alert_email") as mock_email:
            count = trade_manager.safety_check(trigger="manual")

    assert count == 1
    mock_email.assert_called_once()


# ── Naked CE option trade — percent strike support ────────────────────────────

@patch("bot.trade_manager.fivepaisa")
def test_naked_ce_percent_strike_places_limit_order(mock_broker):
    """
    Naked CE (month_type='option') with a PERCENT strike: the strike is computed
    from the live spot, and the CE is placed as a real limit order (non-zero price)
    — not a 0-price market order. Regression for the bug where the naked-CE path
    ignored strike_type and treated the value as a literal strike.
    """
    add(make_settings(), make_watchlist(),
        make_fixed_trade(strike_type="percent", strike_value=3, month_type="option"))

    mock_broker.get_market_quote.return_value = quote_ok(1126.3)   # spot for percent calc
    mock_broker.get_option_chain.return_value = chain_ok(1160)
    mock_broker.get_lot_size.return_value = 400
    mock_broker.place_order.return_value = order_ok(555)

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_trade_opened_email"):
            trade_manager.run_fixed_trades()

    assert mock_broker.place_order.called, "CE order should have been placed"
    price_arg = mock_broker.place_order.call_args[0][5]   # 6th positional arg = price
    assert price_arg > 0, f"CE must be a limit order (non-zero price), got {price_arg}"

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    assert trade is not None and trade.status == "open" and trade.month_type == "option"


# ── Partial fill: no square-off, email the client ─────────────────────────────

@patch("bot.trade_manager.fivepaisa")
def test_partial_fill_no_squareoff_and_emails(mock_broker):
    """Futures fills but CE fails → bot does NOT square off the futures and sends
    an immediate partial-fill alert email (client handles it manually)."""
    add(make_settings(), make_watchlist(), make_fixed_trade())
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1150)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400
    # All 3 fired at once: Futures accepted, CE rejected, PE rejected
    mock_broker.place_order.side_effect = [
        order_ok(111),
        {"success": False, "error": "RMS reject CE"},
        {"success": False, "error": "RMS reject PE"},
    ]

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_partial_fill_alert") as mock_alert:
            with patch("bot.trade_manager.time"):
                trade_manager.run_fixed_trades()

    # All 3 order calls fired at once — no square-off (no extra calls)
    assert mock_broker.place_order.call_count == 3, "all 3 legs fired at once"
    mock_alert.assert_called_once()   # client alerted

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    assert trade is None   # partial collar is not saved as a normal trade


# ── Traceback debugging on real-trade errors ─────────────────────────────────

@patch("bot.trade_manager.fivepaisa")
def test_unexpected_error_logs_full_traceback(mock_broker):
    """An unexpected exception during a fixed trade is logged WITH a full traceback
    (so real-trade failures can be traced to the exact line)."""
    add(make_settings(), make_watchlist(), make_fixed_trade())
    mock_broker.get_market_quote.side_effect = RuntimeError("boom-during-order")

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_fixed_trades()

    db = TestSession()
    err = db.query(Log).filter(Log.level == "ERROR", Log.message.contains("Traceback")).first()
    db.close()
    assert err is not None, "An error log with a traceback should exist"
    assert "boom-during-order" in err.message       # the actual error
    assert "Traceback (most recent call last)" in err.message  # the traceback


# ── Marketable Limit Price (tiered buffer) ────────────────────────────────────

def test_marketable_limit_price_tiered_buffer():
    """Cheap (<=₹100) uses 1%; pricier (>₹100) uses 0.5%; rounded to ₹0.05 tick."""
    mlp = trade_manager._marketable_limit_price

    # Cheap (<= 100) → 1% buffer
    assert mlp(5, "B") == 5.05      # 5 * 1.01
    assert mlp(5, "S") == 4.95      # 5 * 0.99
    assert mlp(100, "B") == 101.00  # boundary is "cheap" (<=100) → 1%

    # Pricier (> 100) → 0.5% buffer
    assert mlp(150, "B") == 150.75  # 150 * 1.005
    assert mlp(150, "S") == 149.25  # 150 * 0.995
    assert mlp(2800, "B") == 2814.00

    # Bad / unavailable LTP → 0 (market fallback)
    assert mlp(0, "B") == 0
    assert mlp(None, "B") == 0


# ── Chartink Scanner Tests ────────────────────────────────────────────────────

def test_chartink_scan_matches_watchlist_and_trades():
    """Chartink returns stocks → matched to watchlist → trades placed."""
    db = TestSession()
    add(
        make_settings(is_trading=True),
        Watchlist(stock_name="INFY", scrip_code="123"),
        Watchlist(stock_name="DIVISLAB", scrip_code="124"),
    )
    db.close()

    mock_broker = {
        "place_order": lambda *args, **kwargs: {"success": True, "ScripCode": 123},
        "get_market_quote": lambda *args, **kwargs: {"LTP": 1200},
        "get_scrip_master": lambda *args, **kwargs: {
            "rows": [
                {"ScripCode": 123, "Multiplier": 1, "LotSize": 1, "SymbolRoot": "INFY"},
            ]
        },
    }

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.run_webhook_trade") as mock_webhook:
            with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
                mock_scan.return_value = ["INFY", "RELIANCE"]  # RELIANCE not in watchlist
                trade_manager.run_chartink_cycle()
                mock_webhook.assert_called_once_with("INFY", force=False)


def test_chartink_scan_skips_non_watchlist_stocks():
    """Chartink returns stocks not in watchlist → they are skipped."""
    db = TestSession()
    add(make_settings(is_trading=True), Watchlist(stock_name="INFY", scrip_code="123"))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.run_webhook_trade") as mock_webhook:
            with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
                mock_scan.return_value = ["RELIANCE", "TCS"]  # Neither in watchlist
                trade_manager.run_chartink_cycle()
                mock_webhook.assert_not_called()


def test_chartink_scan_returns_empty_when_no_match():
    """Chartink returns stocks → none match watchlist → no trades."""
    db = TestSession()
    add(make_settings(is_trading=True))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
            mock_scan.return_value = ["INFY", "RELIANCE"]
            trade_manager.run_chartink_cycle()
            # No assertion needed; just verify it doesn't crash


def test_chartink_skipped_when_trading_off():
    """Chartink cycle skipped if trading is OFF."""
    db = TestSession()
    add(make_settings(is_trading=False))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
            trade_manager.run_chartink_cycle()
            mock_scan.assert_not_called()


def test_chartink_skipped_when_no_watchlist():
    """Chartink cycle skipped if watchlist is empty."""
    db = TestSession()
    add(make_settings(is_trading=True))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
            trade_manager.run_chartink_cycle()
            mock_scan.assert_not_called()


def test_chartink_cycle_skips_stock_already_attempted_today():
    """A stock attempted earlier today is filtered out — run_webhook_trade is NOT
    called again for it (so no re-processing / no repeated skip log every cycle)."""
    db = TestSession()
    add(make_settings(is_trading=True),
        Watchlist(stock_name="INFY", scrip_code="123"),
        Log(level="INFO", message="Screener attempt recorded: INFY [LIVE]"))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.run_webhook_trade") as mock_webhook:
            with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
                mock_scan.return_value = ["INFY"]
                trade_manager.run_chartink_cycle()       # automatic run
                mock_webhook.assert_not_called()         # filtered out, not re-tried


def test_chartink_cycle_force_retries_attempted_stock():
    """A manual forced run DOES retry a stock already attempted today."""
    db = TestSession()
    add(make_settings(is_trading=True),
        Watchlist(stock_name="INFY", scrip_code="123"),
        Log(level="INFO", message="Screener attempt recorded: INFY [LIVE]"))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.run_webhook_trade") as mock_webhook:
            with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
                mock_scan.return_value = ["INFY"]
                trade_manager.run_chartink_cycle(force=True)
                mock_webhook.assert_called_once_with("INFY", force=True)


def test_manual_chartink_run_forces_trades():
    """The manual 'Run Chartink Trades' button passes force=True to each trade."""
    db = TestSession()
    add(make_settings(is_trading=True), Watchlist(stock_name="INFY", scrip_code="123"))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.run_webhook_trade") as mock_webhook:
            with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
                mock_scan.return_value = ["INFY"]
                trade_manager.run_chartink_cycle(force=True)
                mock_webhook.assert_called_once_with("INFY", force=True)


def test_screener_skips_stock_already_attempted_today():
    """
    Once a stock has been attempted today (even a rejected order), an automatic
    screener run must NOT re-attempt it — this stops the all-day order spam.
    """
    db = TestSession()
    add(make_settings(is_trading=True),
        Watchlist(stock_name="INFY", scrip_code="123"),
        Log(level="INFO", message="Screener attempt recorded: INFY [LIVE]"))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        # force=False (automatic) — should bail out at the attempted-today guard,
        # so it never reaches the broker/quote calls.
        trade_manager.run_webhook_trade("INFY")

    db = TestSession()
    skipped = db.query(Log).filter(Log.message.contains("already attempted today")).count()
    db.close()
    assert skipped >= 1


def test_chartink_logs_stocks_not_in_watchlist():
    """Stocks the screener finds but that aren't in the watchlist are logged."""
    db = TestSession()
    add(make_settings(is_trading=True), Watchlist(stock_name="INFY", scrip_code="123"))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.run_webhook_trade"):
            with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
                mock_scan.return_value = ["INFY", "RELIANCE", "TCS"]
                trade_manager.run_chartink_cycle()

    db = TestSession()
    msgs = [l.message for l in db.query(Log).filter(Log.message.contains("not in watchlist")).all()]
    db.close()
    # Each not-in-watchlist stock is logged once (its own line)
    assert any("RELIANCE" in m for m in msgs)
    assert any("TCS" in m for m in msgs)


def test_chartink_not_in_watchlist_logged_once_per_day():
    """A not-in-watchlist stock is logged only ONCE per day, even across cycles."""
    db = TestSession()
    add(make_settings(is_trading=True), Watchlist(stock_name="INFY", scrip_code="123"))
    db.close()

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.run_webhook_trade"):
            with patch("bot.chartink_scanner.run_chartink_scan") as mock_scan:
                mock_scan.return_value = ["MAXHEALTH"]   # not in watchlist
                trade_manager.run_chartink_cycle()       # cycle 1
                trade_manager.run_chartink_cycle()       # cycle 2 (5 min later)
                trade_manager.run_chartink_cycle()       # cycle 3

    db = TestSession()
    count = db.query(Log).filter(Log.message.contains("MAXHEALTH")).count()
    db.close()
    assert count == 1, f"MAXHEALTH should be logged once per day, got {count}"
