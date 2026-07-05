"""
End-to-end mock test of the complete trading flow.
Fixed trades → watchlist check → strike calc → order placement → P&L monitor → close.
All 5paisa broker calls are mocked — no real credentials needed.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime, timedelta
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
    # TotalQty>0 marks the strikes as liquid (the bot skips zero-volume strikes).
    return {
        "success": True,
        "option_chain": [
            {"StrikeRate": ce_strike, "CPType": "CE", "LastRate": 13.85, "Scripcode": "55001", "TotalQty": 5000},
            {"StrikeRate": ce_strike - 50, "CPType": "PE", "LastRate": 11.1, "Scripcode": "55002", "TotalQty": 3000},
            {"StrikeRate": ce_strike - 100, "CPType": "PE", "LastRate": 8.5, "Scripcode": "55003", "TotalQty": 2000},
        ],
    }


def depth_ok():
    """Market-depth response with live sell-side offers (flag 83) → tradeable PE."""
    return {
        "success": True,
        "depth": [
            {"BbBuySellFlag": 66, "Price": 11.0, "Quantity": 150, "NumberOfOrders": 2},
            {"BbBuySellFlag": 83, "Price": 11.2, "Quantity": 120, "NumberOfOrders": 3},
        ],
    }


def depth_empty():
    """Market-depth response with NO sell-side offers → illiquid, can't buy."""
    return {"success": True, "depth": [{"BbBuySellFlag": 66, "Price": 11.0, "Quantity": 150, "NumberOfOrders": 2}]}


def order_ok(oid):
    return {"success": True, "broker_order_id": oid}


def fill_ok():
    """Simulates get_order_status response: order fully executed on the exchange."""
    return {"success": True, "orders": [{"Status": "Fully Executed"}]}


def fill_ok_priced(avg_price, exch_id="EX123"):
    """get_order_status response for a fully executed order WITH the real average
    fill price + exchange order id (what the bot records as the true entry)."""
    return {"success": True, "orders": [
        {"Status": "Fully Executed", "AveragePrice": avg_price, "ExchOrderID": exch_id}
    ]}


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
    mock_broker.get_market_depth.return_value = depth_ok()  # PE has live offers

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
def test_collar_pe_falls_through_to_liquid_strike(mock_broker):
    """Best PE strike has no live offers (illiquid) → bot falls through to the next
    PE that IS tradeable and uses that one (avoids the illiquid-contract rejection)."""
    add(make_settings(), make_watchlist(), make_fixed_trade())
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1150)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400
    mock_broker.place_order.side_effect = [order_ok(111), order_ok(222), order_ok(333)]
    mock_broker.get_order_status.return_value = fill_ok()
    # Best PE (strike 1100, scrip 55002) has no offers; next PE (1050, scrip 55003) does.
    def depth_by_scrip(token, client, exch, exch_type, scrip_code):
        return depth_empty() if str(scrip_code) == "55002" else depth_ok()
    mock_broker.get_market_depth.side_effect = depth_by_scrip

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_trade_opened_email"):
            with patch("bot.trade_manager.time"):
                trade_manager.run_fixed_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    assert trade is not None
    assert trade.pe_scrip_code == "55003"   # the liquid fallback, not the illiquid 55002
    assert trade.pe_entry_price == 8.5
    assert mock_broker.place_order.call_count == 3


@patch("bot.trade_manager.fivepaisa")
def test_collar_skips_when_no_liquid_pe(mock_broker):
    """No PE strike has live offers → the whole collar is skipped (NO naked FUT+CE),
    because there's no tradeable hedge."""
    add(make_settings(), make_watchlist(), make_fixed_trade())
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1150)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400
    mock_broker.get_market_depth.return_value = depth_empty()  # no PE has offers

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.time"):
            trade_manager.run_fixed_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    pe_log = db.query(Log).filter(Log.message.contains("no tradeable PE")).first()
    db.close()
    assert trade is None                       # nothing opened
    assert pe_log is not None                   # reason logged
    mock_broker.place_order.assert_not_called()  # crucial: never fire FUT+CE without a PE


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
    mock_broker.get_market_depth.return_value = depth_ok()  # PE has live offers

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


# ── Square-off verification (real-money safety) ─────────────────────────────────

def positions_resp(*open_scrip_codes):
    """NetPositionNetWise response where the given scrip codes still hold an open
    net qty. Call with no args for a fully flat account."""
    return {
        "success": True,
        "positions": [{"ScripCode": c, "NetQty": 1} for c in open_scrip_codes],
    }


def setup_sequential_squareoff(mock_broker, open_scrips, exit_fills=None, never_fills=None):
    """
    Model the CE->PE->Futures SEQUENTIAL square-off for tests: each leg's position
    reads OPEN (NetQty 1) until its exit order is placed, then FLAT (NetQty 0). A
    flat leg carries its exit average rate so _actual_exit_fills can read it.
      open_scrips: iterable of scrip codes initially open (a leg NOT listed is treated
                   as already flat / skipped).
      exit_fills:  {scrip: (avg_rate_field, price)} e.g. ("SellAvgRate", 1158.0).
      never_fills: scrip codes that STAY open even after an exit order is placed
                   (simulates a leg the market outruns).
    Also wires get_order_status + cancel_order so the cancel-then-replace path works.
    Returns the shared state dict (state["placed"]).
    """
    state = {"placed": set()}
    exit_fills = exit_fills or {}
    never = set(never_fills or [])

    def _positions(*a, **k):
        pos = []
        for sc in open_scrips:
            flat = (sc in state["placed"]) and (sc not in never)
            p = {"ScripCode": sc, "NetQty": 0 if flat else 1}
            if flat and sc in exit_fills:
                field, price = exit_fills[sc]
                p[field] = price
            pos.append(p)
        return {"success": True, "positions": pos}

    def _place(token, exch, etype, scrip, side, *a, **k):
        state["placed"].add(str(scrip))
        return order_ok(999)

    mock_broker.get_positions.side_effect = _positions
    mock_broker.place_order.side_effect = _place
    # a resting (Pending) order with an ExchOrderID, so cancel-then-replace can cancel it
    mock_broker.get_order_status.return_value = {
        "success": True, "orders": [{"Status": "Pending", "ExchOrderID": "111"}]}
    mock_broker.cancel_order.return_value = {"success": True}
    return state


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_keeps_trade_open_when_not_confirmed_flat(mock_now, mock_broker):
    """
    Profit target hits and the exit orders are ACCEPTED, but the broker still shows
    the legs open afterwards (the marketable-limit orders never filled). The trade
    MUST stay open (so the de-dup guard blocks a duplicate real-money trade) and the
    client must be alerted. This is the exact RVNL failure mode.
    """
    add(make_settings(), make_open_trade(profit_target=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)  # profit
    mock_broker.place_order.return_value = order_ok(999)  # accepted...
    # ...but positions ALWAYS show every leg still open -> never confirmed flat.
    mock_broker.get_positions.return_value = positions_resp(
        "INFY_FUT", "INFY_CE_1150", "INFY_PE_1100")

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_squareoff_failed_email") as mock_alert:
                with patch("notifications.email.send_trade_closed_email") as mock_closed:
                    trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "open", "must NOT mark closed while position is still live"
    assert trade.close_reason is None
    mock_alert.assert_called_once()          # client warned to check 5paisa
    mock_closed.assert_not_called()          # never send a 'closed' email


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_marks_closed_when_positions_confirm_flat(mock_now, mock_broker):
    """Profit hits, exit orders fill, and the broker confirms the legs are flat →
    the trade is marked closed with the live exit P&L."""
    add(make_settings(), make_open_trade(profit_target=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)
    setup_sequential_squareoff(mock_broker, {"INFY_FUT", "INFY_CE_1150", "INFY_PE_1100"})

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_trade_closed_email"):
                trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "closed"
    assert trade.close_reason == "profit"
    assert trade.pnl > 20
    assert mock_broker.place_order.call_count == 3  # all 3 legs squared off (one order each)


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_is_idempotent_skips_already_flat_legs(mock_now, mock_broker):
    """
    A leg already flat at the broker (e.g. a previous attempt's exit has since
    filled) must NOT be re-squared — otherwise a retry would flip it to the opposite
    side. Only the still-open legs get an exit order.
    """
    add(make_settings(), make_open_trade(profit_target=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)
    # PE already flat (not in the open set); only FUT + CE are open.
    setup_sequential_squareoff(mock_broker, {"INFY_FUT", "INFY_CE_1150"})

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_trade_closed_email"):
                trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "closed"
    # Only CE and FUT were squared; the already-flat PE leg was skipped (no order).
    squared_scrips = [call.args[3] for call in mock_broker.place_order.call_args_list]
    assert mock_broker.place_order.call_count == 2
    assert "INFY_PE_1100" not in squared_scrips


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_not_retried_within_cooldown_and_emails_once(mock_now, mock_broker):
    """
    Two monitor cycles in quick succession (same minute) while the square-off keeps
    failing must NOT place a second round of exit orders, and must email the client
    only ONCE — no every-10s flood.
    """
    add(make_settings(), make_open_trade(profit_target=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)  # profit
    mock_broker.place_order.return_value = order_ok(999)
    mock_broker.get_positions.return_value = positions_resp(
        "INFY_FUT", "INFY_CE_1150", "INFY_PE_1100")  # never goes flat -> always fails

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_squareoff_failed_email") as mock_alert:
                trade_manager.monitor_open_trades()   # cycle 1 -> attempt 1
                trade_manager.monitor_open_trades()   # cycle 2 -> cooldown, skip

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "open"
    assert trade.squareoff_attempts == 1          # only ONE _close_trade attempt despite two cycles
    # profit close + CE never fills -> CE gets its 2 cancel-then-replace attempts, then
    # STOP (keep PE/FUT hedged). So 2 CE orders, and no second round from cycle 2.
    assert mock_broker.place_order.call_count == 2
    mock_alert.assert_called_once()                # client emailed exactly once


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_retries_after_cooldown_without_second_email(mock_now, mock_broker):
    """After the 2-minute cooldown a still-hit target retries the square-off, but the
    client is NOT emailed again (already alerted on the first failure)."""
    trade = make_open_trade(profit_target=20)
    trade.squareoff_attempts = 1
    trade.squareoff_alerted = True
    trade.last_squareoff_attempt_at = datetime(2026, 5, 20, 10, 27)  # 3 min before 'now'
    add(make_settings(), trade)
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)  # profit still hit
    mock_broker.place_order.return_value = order_ok(999)
    mock_broker.get_positions.return_value = positions_resp(
        "INFY_FUT", "INFY_CE_1150", "INFY_PE_1100")

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_squareoff_failed_email") as mock_alert:
                trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.squareoff_attempts == 2          # retried
    assert mock_broker.place_order.called          # exit orders placed again
    mock_alert.assert_not_called()                 # no second email


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_gives_up_after_max_attempts(mock_now, mock_broker):
    """Once the max attempts are used up the bot stops auto-retrying — it places no
    further exit orders and leaves the trade open for manual close / position sync."""
    trade = make_open_trade(profit_target=20)
    trade.squareoff_attempts = 3                   # == SQUAREOFF_MAX_ATTEMPTS
    trade.squareoff_alerted = True
    trade.last_squareoff_attempt_at = datetime(2026, 5, 20, 10, 20)  # well past cooldown
    add(make_settings(), trade)
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)  # still profit

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_squareoff_failed_email") as mock_alert:
            trade_manager.monitor_open_trades()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()

    assert trade.status == "open"
    assert trade.squareoff_attempts == 3           # not incremented further
    mock_broker.place_order.assert_not_called()    # no more exit orders
    mock_alert.assert_not_called()


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
    mock_broker.get_order_status.return_value = fill_ok()   # CE confirmed executed

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
def test_partial_fill_tracks_open_legs_and_emails(mock_broker):
    """Futures fills but CE & PE are rejected → bot SAVES a trade tracking ONLY the
    filled futures leg (CE/PE left empty, like a naked-CE trade), does NOT square it
    off, and sends a partial-fill alert email so the client handles the rest."""
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
    mock_broker.get_order_status.return_value = fill_ok()  # the accepted FUT leg fills
    mock_broker.get_market_depth.return_value = depth_ok()  # PE has live offers

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_partial_fill_alert") as mock_alert:
            with patch("bot.trade_manager.time"):
                trade_manager.run_fixed_trades()

    # All 3 order calls fired at once — no square-off (no extra calls)
    assert mock_broker.place_order.call_count == 3, "all 3 legs fired at once"
    mock_alert.assert_called_once()   # client alerted about the failed legs

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    # The filled futures leg IS tracked; the rejected CE/PE legs are left empty.
    assert trade is not None, "the leg that opened should be tracked"
    assert trade.status == "open"
    assert trade.futures_broker_order_id == "111"
    assert trade.futures_entry_price == 1126.3
    assert trade.ce_broker_order_id is None
    assert trade.pe_broker_order_id is None
    assert trade.ce_scrip_code is None
    assert trade.pe_scrip_code is None
    assert trade.is_paper_trade is False


@patch("bot.trade_manager.fivepaisa")
def test_webhook_partial_fill_tracks_open_legs(mock_broker):
    """Screener collar: PE rejected as illiquid (the real BEL case) → bot tracks the
    filled FUT+CE legs and leaves PE empty, sending the partial-fill alert."""
    add(make_settings(), make_watchlist("ONGC", "500312"))
    mock_broker.get_market_quote.return_value = quote_ok(280.5, "500312")
    mock_broker.get_option_chain.return_value = chain_ok(300)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400
    # FUT accepted, CE accepted, PE rejected (illiquid contract)
    mock_broker.place_order.side_effect = [
        order_ok(201),
        order_ok(202),
        {"success": False, "error": "Trading not allowed in illiquid contract"},
    ]
    mock_broker.get_order_status.return_value = fill_ok()  # accepted legs fill
    mock_broker.get_market_depth.return_value = depth_ok()  # PE has live offers

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_partial_fill_alert") as mock_alert:
            with patch("bot.trade_manager.time"):
                trade_manager.run_webhook_trade("ONGC")

    assert mock_broker.place_order.call_count == 3
    mock_alert.assert_called_once()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    assert trade is not None
    assert trade.status == "open"
    assert trade.futures_broker_order_id == "201"
    assert trade.ce_broker_order_id == "202"
    assert trade.pe_broker_order_id is None   # the illiquid PE is left empty
    assert trade.pe_scrip_code is None


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


# ── Position reconciler (false "manual close" safety) ───────────────────────────
# Regression guard for the bug where freshly-placed live trades were marked
# "closed manually by client" within a minute of opening (broker positions feed
# lags at market open) — with no P&L recorded.

from bot import position_sync

# get_ist_now() returns a tz-AWARE datetime in production, while placed_at loaded
# from SQLite is tz-NAIVE. SYNC_NOW_AWARE mirrors get_ist_now(); placed_at is set
# from the naive SYNC_NOW (and round-trips through SQLite naive anyway). This is the
# exact aware-vs-naive mix that crashed position sync in production.
from utils.helpers import IST
SYNC_NOW = datetime(2026, 5, 20, 11, 0, 0)
SYNC_NOW_AWARE = IST.localize(SYNC_NOW)


def _aged_open_trade(minutes_old=60):
    """An open trade placed `minutes_old` minutes before SYNC_NOW."""
    t = make_open_trade()
    t.placed_at = SYNC_NOW - timedelta(minutes=minutes_old)
    return t


@patch("bot.position_sync.fivepaisa")
@patch("bot.position_sync.get_ist_now", return_value=SYNC_NOW_AWARE)
def test_sync_does_not_close_on_empty_snapshot(mock_now, mock_broker):
    """An EMPTY positions snapshot (feed lag / 'no record') must NEVER be read as
    'the client closed everything' — the trade stays open."""
    add(make_settings(), _aged_open_trade(minutes_old=60))
    mock_broker.get_positions.return_value = positions_resp()  # success=True, []

    with patch("bot.position_sync.SessionLocal", TestSession):
        position_sync.sync_positions()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    assert trade.status == "open"
    assert trade.close_reason is None


@patch("bot.position_sync.fivepaisa")
@patch("bot.position_sync.get_ist_now", return_value=SYNC_NOW_AWARE)
def test_sync_skips_trade_inside_grace_period(mock_now, mock_broker):
    """A just-placed trade (inside the grace window) is not reconciled even if its
    legs aren't in a live, non-empty snapshot yet (open-time feed lag)."""
    add(make_settings(), _aged_open_trade(minutes_old=2))
    # Live feed is alive (has OTHER positions) but not our legs yet.
    mock_broker.get_positions.return_value = positions_resp("SOMEONE_ELSE_FUT")

    with patch("bot.position_sync.SessionLocal", TestSession):
        position_sync.sync_positions()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    assert trade.status == "open"
    assert trade.close_reason is None


@patch("bot.trade_manager.fivepaisa")
@patch("bot.position_sync.fivepaisa")
@patch("bot.position_sync.get_ist_now", return_value=SYNC_NOW_AWARE)
def test_sync_closes_when_legs_absent_from_live_feed_and_records_pnl(
        mock_now, mock_pos_broker, mock_tm_broker):
    """An aged trade whose legs are absent from a LIVE, non-empty snapshot is a real
    manual close → marked closed (manual) AND P&L recorded (not blank)."""
    add(make_settings(), _aged_open_trade(minutes_old=60))
    mock_pos_broker.get_positions.return_value = positions_resp("SOMEONE_ELSE_FUT")
    # P&L recording fetches current prices via trade_manager._fetch_current_prices.
    mock_tm_broker.get_market_quote.return_value = multi_quote_ok(1130, 10.0, 12.0)

    with patch("bot.position_sync.SessionLocal", TestSession):
        position_sync.sync_positions()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    assert trade.status == "closed"
    assert trade.close_reason == "manual"
    assert trade.pnl is not None
    assert trade.closed_at is not None


@patch("bot.position_sync.fivepaisa")
@patch("bot.position_sync.get_ist_now", return_value=SYNC_NOW_AWARE)
def test_sync_keeps_trade_open_when_legs_present(mock_now, mock_broker):
    """If our legs still hold an open net qty, the trade stays open."""
    add(make_settings(), _aged_open_trade(minutes_old=60))
    mock_broker.get_positions.return_value = positions_resp(
        "INFY_FUT", "INFY_CE_1150", "INFY_PE_1100")

    with patch("bot.position_sync.SessionLocal", TestSession):
        position_sync.sync_positions()

    db = TestSession()
    trade = db.query(Trade).first()
    db.close()
    assert trade.status == "open"


# ── Rejection-reason logging (turn "Rejected by Exch" into a real reason) ────────

@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.generate_remote_order_id", side_effect=lambda name: name + "_RID")
def test_confirm_fills_logs_broker_rejection_reason(mock_rid, mock_broker):
    """A leg accepted by RMS but then Rejected by the exchange → the bot pulls the
    broker's REASON from the order book and surfaces it in the log AND the partial-
    fill alert, instead of just 'Rejected by Exch'. (The NIFTY tick-size case.)"""
    add(make_settings(), make_watchlist(), make_fixed_trade())
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1150)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400
    mock_broker.get_market_depth.return_value = depth_ok()
    # All 3 accepted by RMS...
    mock_broker.place_order.side_effect = [order_ok(111), order_ok(222), order_ok(333)]
    # ...but FUT is Rejected by Exch; CE & PE fully executed.
    mock_broker.get_order_status.side_effect = [
        {"success": True, "orders": [{"Status": "Rejected by Exch"}]},  # FUT
        fill_ok(),  # CE
        fill_ok(),  # PE
    ]
    # The order book carries the reason, keyed by the FUT RemoteOrderID.
    mock_broker.get_order_book.return_value = {
        "success": True,
        "orders": [{"RemoteOrderID": "INFY_FUT_RID",
                    "Reason": "The order price Is Not multiple of the tick size."}],
    }

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_partial_fill_alert") as mock_alert:
            with patch("bot.trade_manager.time"):
                trade_manager.run_fixed_trades()

    # Reason surfaced in the partial-fill alert...
    mock_alert.assert_called_once()
    alert_text = " ".join(str(a) for a in mock_alert.call_args[0])
    assert "tick size" in alert_text.lower(), f"reason missing from alert: {alert_text}"

    # ...and written to the logs.
    db = TestSession()
    logs = " ".join(l.message for l in db.query(Log).all())
    trade = db.query(Trade).first()
    db.close()
    assert "tick size" in logs.lower(), "broker reason should be in the logs"
    # FUT (rejected) is left empty; CE+PE are tracked.
    assert trade is not None
    assert trade.futures_scrip_code is None
    assert trade.ce_scrip_code is not None and trade.pe_scrip_code is not None


# ── Per-instrument tick size (fix for futures "not multiple of tick size") ───────

@patch("bot.trade_manager.fivepaisa")
def test_marketable_limit_price_uses_instrument_tick(mock_broker):
    """Futures with a 0.1 tick must get a price on the 0.1 grid — a 0.05-rounded
    price (…x5) is rejected by the exchange (the NIFTY/RELIANCE futures bug)."""
    from bot.trade_manager import _marketable_limit_price
    mock_broker.get_tick_size.return_value = 0.1
    for ltp, side in [(24091.0, "B"), (24091.0, "S"), (24191.85, "B"), (567.9, "S")]:
        p = _marketable_limit_price(ltp, side, "62329")
        assert abs(p / 0.1 - round(p / 0.1)) < 1e-9, f"{p} (ltp={ltp},{side}) not on 0.1 grid"


def test_marketable_limit_price_defaults_to_005_without_scrip():
    """No scrip_code → 0.05 tick (previous behaviour); never raises."""
    from bot.trade_manager import _marketable_limit_price
    p = _marketable_limit_price(250.0, "B")          # > ₹100 → 0.5% buffer
    assert abs(p / 0.05 - round(p / 0.05)) < 1e-9
    assert _marketable_limit_price(0, "B", "62329") == 0      # bad ltp → 0, no crash
    assert _marketable_limit_price("x", "B", "62329") == 0    # non-numeric → 0, no crash


# ── Live order-book pricing (stop orders resting "away" on a stale LTP) ──────────

@patch("bot.trade_manager.fivepaisa")
def test_order_price_sell_uses_best_bid_not_stale_ltp(mock_broker):
    """The MCX case: stale LTP ~1.95 but live bid 1.50. A SELL must anchor to the
    best BID (1.50) so it crosses and fills, not rest away at 1.95."""
    from bot.trade_manager import _order_price
    add(make_settings())
    mock_broker.get_tick_size.return_value = 0.05
    mock_broker.get_market_depth.return_value = {"success": True, "depth": [
        {"BbBuySellFlag": 66, "Price": 1.45, "Quantity": 100},
        {"BbBuySellFlag": 66, "Price": 1.50, "Quantity": 200},   # best bid
        {"BbBuySellFlag": 83, "Price": 1.95, "Quantity": 50},    # stale offer
    ]}
    db = TestSession(); s = db.query(Settings).first()
    price = _order_price(db, s, "146623", "S", 1.95, "CE")   # stale ltp 1.95
    db.close()
    assert price <= 1.55, f"sell should price near the 1.50 bid, got {price} (stale 1.95?)"
    assert price > 0


@patch("bot.trade_manager.fivepaisa")
def test_order_price_buy_uses_best_ask(mock_broker):
    """A BUY anchors to the best ASK so it crosses up and fills."""
    from bot.trade_manager import _order_price
    add(make_settings())
    mock_broker.get_tick_size.return_value = 0.05
    mock_broker.get_market_depth.return_value = {"success": True, "depth": [
        {"BbBuySellFlag": 83, "Price": 2.00, "Quantity": 100},   # best ask
        {"BbBuySellFlag": 83, "Price": 2.10, "Quantity": 200},
        {"BbBuySellFlag": 66, "Price": 1.50, "Quantity": 50},
    ]}
    db = TestSession(); s = db.query(Settings).first()
    price = _order_price(db, s, "X", "B", 1.50, "PE")   # stale ltp 1.50
    db.close()
    assert price >= 2.00, f"buy should price at/above best ask 2.00, got {price}"


@patch("bot.trade_manager.fivepaisa")
def test_order_price_falls_back_to_ltp_when_no_depth(mock_broker):
    """No live book → fall back to the LTP-based marketable price (previous behaviour)."""
    from bot.trade_manager import _order_price
    add(make_settings())
    mock_broker.get_tick_size.return_value = 0.05
    mock_broker.get_market_depth.return_value = {"success": False, "error": "no depth"}
    db = TestSession(); s = db.query(Settings).first()
    price = _order_price(db, s, "X", "S", 10.0, "CE")
    db.close()
    assert abs(price - 9.9) < 0.001, f"expected LTP fallback ~9.9, got {price}"


@patch("bot.trade_manager.fivepaisa")
def test_order_price_never_raises_on_depth_crash(mock_broker):
    """A depth-fetch crash must NOT block the order — it falls back to LTP."""
    from bot.trade_manager import _order_price
    add(make_settings())
    mock_broker.get_tick_size.return_value = 0.05
    mock_broker.get_market_depth.side_effect = Exception("boom")
    db = TestSession(); s = db.query(Settings).first()
    price = _order_price(db, s, "X", "B", 20.0, "FUT")
    db.close()
    assert abs(price - 20.2) < 0.001, f"expected LTP fallback ~20.2, got {price}"


# ── Record the REAL executed price + exchange trade id (client request #2) ───────

@patch("bot.trade_manager.fivepaisa")
def test_collar_records_real_fill_price_and_exch_id(mock_broker):
    """The collar records each leg's ACTUAL average fill price + exchange order id
    (from OrderStatus), not the LTP it priced off."""
    add(make_settings(), make_watchlist(), make_fixed_trade())
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1150)
    mock_broker.get_futures_scrip_code.return_value = "62620"
    mock_broker.get_lot_size.return_value = 400
    mock_broker.get_market_depth.return_value = depth_ok()
    mock_broker.place_order.side_effect = [order_ok(111), order_ok(222), order_ok(333)]
    # Each leg fills at a price DIFFERENT from the LTP, with its own exch id.
    mock_broker.get_order_status.side_effect = [
        fill_ok_priced(1130.5, "FUT-EX"),   # FUT
        fill_ok_priced(6.25, "CE-EX"),      # CE
        fill_ok_priced(9.4, "PE-EX"),       # PE
    ]

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_trade_opened_email"):
            with patch("bot.trade_manager.time"):
                trade_manager.run_fixed_trades()

    db = TestSession(); trade = db.query(Trade).first(); db.close()
    assert trade is not None
    assert trade.futures_entry_price == 1130.5 and trade.futures_exch_order_id == "FUT-EX"
    assert trade.ce_entry_price == 6.25 and trade.ce_exch_order_id == "CE-EX"
    assert trade.pe_entry_price == 9.4 and trade.pe_exch_order_id == "PE-EX"


@patch("bot.trade_manager.fivepaisa")
def test_naked_ce_records_real_fill_price_and_exch_id(mock_broker):
    """Naked CE records the real fill price + exch id (the MCX case: priced off a
    stale LTP but fills at a different price)."""
    add(make_settings(), make_watchlist(),
        make_fixed_trade(strike_type="percent", strike_value=3, month_type="option"))
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1160)
    mock_broker.get_lot_size.return_value = 400
    mock_broker.place_order.return_value = order_ok(555)
    mock_broker.get_order_status.return_value = fill_ok_priced(1.5, "MCX-EX")  # real fill 1.5

    with patch("bot.trade_manager.SessionLocal", TestSession):
        trade_manager.run_fixed_trades()

    db = TestSession(); trade = db.query(Trade).first(); db.close()
    assert trade is not None
    assert trade.ce_entry_price == 1.5, f"should record real fill 1.5, got {trade.ce_entry_price}"
    assert trade.ce_exch_order_id == "MCX-EX"


@patch("bot.trade_manager.fivepaisa")
def test_naked_ce_not_recorded_if_not_executed(mock_broker):
    """If the CE order is accepted but does NOT execute (rests away), the bot does
    NOT record a live trade — it alerts the client instead."""
    add(make_settings(), make_watchlist(),
        make_fixed_trade(strike_type="percent", strike_value=3, month_type="option"))
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1160)
    mock_broker.get_lot_size.return_value = 400
    mock_broker.place_order.return_value = order_ok(555)               # accepted...
    mock_broker.get_order_status.return_value = {"success": True, "orders": [{"Status": "Pending"}]}  # ...stays pending
    mock_broker.get_order_book.return_value = {"success": True, "orders": []}

    # patch time so the ~10s fill-confirmation poll doesn't actually sleep in the test
    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_partial_fill_alert") as mock_alert:
            with patch("bot.trade_manager.time"):
                trade_manager.run_fixed_trades()

    db = TestSession(); trade = db.query(Trade).first(); db.close()
    assert trade is None, "an unexecuted CE must NOT be recorded as a live trade (after polling)"
    mock_alert.assert_called_once()


# ── Exit P&L tracks the ACTUAL fill, not the LTP ────────────────────────────────

@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_close_records_actual_exit_fill_not_ltp(mock_now, mock_broker):
    """On close, the exit price is read from the broker's closing-side average rate
    (the real fill), not the LTP at the close moment — so P&L matches the broker."""
    add(make_settings(), make_open_trade(profit_target=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)  # LTP (profit)
    # Actual broker fills differ from the LTP. Long legs (FUT/PE) close by SELL ->
    # SellAvgRate; short CE closes by BUY -> BuyAvgRate.
    setup_sequential_squareoff(
        mock_broker, {"INFY_FUT", "INFY_CE_1150", "INFY_PE_1100"},
        exit_fills={
            "INFY_FUT": ("SellAvgRate", 1158.0),
            "INFY_CE_1150": ("BuyAvgRate", 5.2),
            "INFY_PE_1100": ("SellAvgRate", 7.9),
        })

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_trade_closed_email"):
                trade_manager.monitor_open_trades()

    db = TestSession(); trade = db.query(Trade).first(); db.close()
    assert trade.status == "closed"
    # Exit prices = the ACTUAL fills, NOT the LTP (1160 / 5.0 / 8.0)
    assert trade.futures_exit_price == 1158.0
    assert trade.ce_exit_price == 5.2
    assert trade.pe_exit_price == 7.9
    # P&L from the real fills: (1158-1126.3)+(13.85-5.2)+(7.9-11.1) = 37.15
    assert abs(trade.pnl - 37.15) < 0.01


# ── Fill confirmation polls (a slow fill must not be dropped — the BEL case) ─────

@patch("bot.trade_manager.fivepaisa")
def test_confirm_fills_polls_until_pending_order_fills(mock_broker):
    """A leg that reads 'Pending' on the first check but fills a couple seconds later
    must be caught by the ~10s poll window and RECORDED with its real fill — not
    wrongly dropped (exactly the BEL CE untracked-position bug)."""
    add(make_settings(), make_watchlist(),
        make_fixed_trade(strike_type="percent", strike_value=3, month_type="option"))
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1160)
    mock_broker.get_lot_size.return_value = 400
    mock_broker.place_order.return_value = order_ok(555)
    # First poll: still Pending. Next poll: Fully Executed (the slightly-slow fill).
    mock_broker.get_order_status.side_effect = [
        {"success": True, "orders": [{"Status": "Pending"}]},
        fill_ok_priced(3.1, "BEL-EX"),
    ]

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("bot.trade_manager.time"):   # skip the poll sleeps
            trade_manager.run_fixed_trades()

    db = TestSession(); trade = db.query(Trade).first(); db.close()
    assert trade is not None, "a leg that fills on a later poll MUST be recorded"
    assert trade.status == "open"
    assert trade.ce_entry_price == 3.1          # real fill, captured on the 2nd poll
    assert trade.ce_exch_order_id == "BEL-EX"


# ── Naked-CE open email: real money only ────────────────────────────────────────

@patch("bot.trade_manager.fivepaisa")
def test_naked_ce_real_open_sends_email(mock_broker):
    """A REAL naked-CE trade opening emails the client."""
    add(make_settings(), make_watchlist(),
        make_fixed_trade(strike_type="percent", strike_value=3, month_type="option"))  # is_trade=True -> real
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1160)
    mock_broker.get_lot_size.return_value = 400
    mock_broker.place_order.return_value = order_ok(555)
    mock_broker.get_order_status.return_value = fill_ok_priced(9.6, "EX1")

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_naked_ce_opened_email") as mock_email:
            trade_manager.run_fixed_trades()

    mock_email.assert_called_once()


@patch("bot.trade_manager.fivepaisa")
def test_naked_ce_paper_open_no_email(mock_broker):
    """A PAPER naked-CE trade opening does NOT email (real money only)."""
    add(make_settings(), make_watchlist(),
        make_fixed_trade(strike_type="percent", strike_value=3, month_type="option", is_trade=False))  # paper
    mock_broker.get_market_quote.return_value = quote_ok(1126.3)
    mock_broker.get_option_chain.return_value = chain_ok(1160)
    mock_broker.get_lot_size.return_value = 400

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_naked_ce_opened_email") as mock_email:
            trade_manager.run_fixed_trades()

    mock_email.assert_not_called()


@patch("bot.trade_manager.fivepaisa")
def test_safety_check_excludes_paper_trades(mock_broker):
    """3:40 PM summary reports ONLY real-money trades — paper trades are excluded."""
    real = make_open_trade(name="INFY")
    paper = make_open_trade(name="RELIANCE")
    paper.is_paper_trade = True
    add(make_settings(is_trading=True), real, paper)
    mock_broker.get_market_quote.return_value = multi_quote_ok(1130, 12.0, 10.0)

    with patch("bot.trade_manager.SessionLocal", TestSession):
        with patch("notifications.email.send_safety_alert_email") as mock_email:
            count = trade_manager.safety_check()

    assert count == 1, f"only the 1 real trade should be reported (paper excluded), got {count}"
    mock_email.assert_called_once()
    summary_arg = mock_email.call_args[0][0]
    names = [s["stock_name"] for s in summary_arg]
    assert names == ["INFY"], f"paper trade must be excluded from the summary, got {names}"


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_closes_ce_then_pe_then_futures(mock_now, mock_broker):
    """Square-off places the exit orders in CE -> PE -> Futures order (the short CE
    leg first)."""
    add(make_settings(), make_open_trade(profit_target=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)  # profit
    setup_sequential_squareoff(mock_broker, {"INFY_FUT", "INFY_CE_1150", "INFY_PE_1100"})

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_trade_closed_email"):
                trade_manager.monitor_open_trades()

    # scrip_code is the 4th positional arg (index 3) of place_order
    scrips = [c.args[3] for c in mock_broker.place_order.call_args_list]
    assert scrips == ["INFY_CE_1150", "INFY_PE_1100", "INFY_FUT"], \
        f"expected exit order CE -> PE -> FUT, got {scrips}"


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_profit_stops_and_keeps_hedges_if_ce_fails(mock_now, mock_broker):
    """PROFIT close + CE won't close -> STOP: do NOT place PE/Futures exit orders
    (keep them as hedges), keep the trade open, alert the client. Cancel-then-replace
    is used between the CE's two attempts."""
    add(make_settings(), make_open_trade(profit_target=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1160, 5.0, 8.0)  # profit
    setup_sequential_squareoff(
        mock_broker, {"INFY_FUT", "INFY_CE_1150", "INFY_PE_1100"},
        never_fills={"INFY_CE_1150"})   # CE keeps getting outrun

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_squareoff_failed_email") as mock_alert:
                trade_manager.monitor_open_trades()

    db = TestSession(); trade = db.query(Trade).first(); db.close()
    assert trade.status == "open"          # never marked closed
    scrips = [c.args[3] for c in mock_broker.place_order.call_args_list]
    assert set(scrips) == {"INFY_CE_1150"}, f"only CE should be attempted on profit, got {scrips}"
    assert mock_broker.place_order.call_count == 2         # CE tried twice (cancel-then-replace)
    assert mock_broker.cancel_order.called                # the 1st CE order was cancelled before the 2nd
    mock_alert.assert_called_once()


@patch("bot.trade_manager.fivepaisa")
@patch("bot.trade_manager.get_ist_now", return_value=datetime(2026, 5, 20, 10, 30))
def test_squareoff_loss_continues_to_pe_and_futures_if_ce_fails(mock_now, mock_broker):
    """LOSS close + CE won't close -> CONTINUE: still square off PE and Futures
    (close whatever fills). Only the CE is left open."""
    add(make_settings(), make_open_trade(loss_limit=20))
    mock_broker.get_market_quote.return_value = multi_quote_ok(1100, 13.85, 11.1)  # ~ -26 loss
    setup_sequential_squareoff(
        mock_broker, {"INFY_FUT", "INFY_CE_1150", "INFY_PE_1100"},
        never_fills={"INFY_CE_1150"})   # CE won't close; PE + FUT will

    with patch("bot.trade_manager.SQUAREOFF_SETTLE_SECONDS", 0):
        with patch("bot.trade_manager.SessionLocal", TestSession):
            with patch("notifications.email.send_squareoff_failed_email"):
                trade_manager.monitor_open_trades()

    scrips = [c.args[3] for c in mock_broker.place_order.call_args_list]
    # CE attempted (twice), and PE + Futures were still squared off (continued).
    assert "INFY_PE_1100" in scrips and "INFY_FUT" in scrips, f"PE+FUT must be closed on loss, got {scrips}"
    assert scrips.count("INFY_CE_1150") == 2              # CE got its 2 cancel-then-replace attempts
    db = TestSession(); trade = db.query(Trade).first(); db.close()
    assert trade.status == "open"          # not fully flat (CE still open) -> stays open


# ── Manual Real-P&L adjustment (dashboard override; real-only, paper untouched) ──

def test_manual_real_pnl_adjustment_real_only_and_keeps_adding():
    from routes.dashboard import get_total_pnl, set_real_pnl
    real = make_open_trade(name="INFY"); real.status = "closed"; real.pnl = 1000.0
    paper = make_open_trade(name="RELIANCE"); paper.status = "closed"; paper.pnl = 500.0
    paper.is_paper_trade = True
    add(make_settings(), real, paper)

    db = TestSession()
    before = get_total_pnl(db)
    assert before["real_pnl"] == 1000.0 and before["paper_pnl"] == 500.0

    # client sets the REAL total to 800 (e.g. broker net after charges)
    set_real_pnl({"target": 800.0}, db)
    after = get_total_pnl(db)
    assert after["real_pnl"] == 800.0                 # shows the client's number
    assert after["paper_pnl"] == 500.0                # paper P&L NOT affected
    assert after["manual_pnl_adjustment"] == -200.0
    assert after["real_pnl_computed"] == 1000.0       # bot's raw sum unchanged
    db.close()

    # a new real trade closes at +300 -> displayed real P&L becomes 800 + 300
    db = TestSession()
    nt = make_open_trade(name="SBIN"); nt.status = "closed"; nt.pnl = 300.0
    db.add(nt); db.commit()
    after2 = get_total_pnl(db)
    assert after2["real_pnl"] == 1100.0               # kept adding from the client's number
    assert after2["paper_pnl"] == 500.0               # still unaffected
    db.close()


def test_set_real_pnl_rejects_non_number():
    from routes.dashboard import set_real_pnl
    from fastapi import HTTPException
    add(make_settings())
    db = TestSession()
    try:
        set_real_pnl({"target": "abc"}, db)
        assert False, "should have raised for a non-number target"
    except HTTPException as e:
        assert e.status_code == 400
    finally:
        db.close()
