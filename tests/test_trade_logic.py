"""
Tests for pure trading logic — no credentials or API calls needed.
Tests strike calculation, P&L calculation, premium validation, exchange calendar.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from bot.strike_calculator import (
    calculate_ce_strike,
    find_pe_strike,
    find_pe_candidates,
    validate_premium_condition,
    get_strike_interval,
    round_strike_up
)
from utils.helpers import calculate_trade_pnl, generate_remote_order_id
from utils.exchange_calendar import get_expiry_date, get_current_expiry, get_next_expiry


# ── Strike Calculator Tests ───────────────────────────────────────────────────

def test_fixed_ce_strike():
    """Fixed strike should return the exact value given."""
    result = calculate_ce_strike(1126.3, "fixed", 1150, "INFY")
    assert result == 1150.0

def test_percent_ce_strike_returns_raw_target():
    """
    'percent' now returns the RAW target (spot + X%), not a hardcoded-interval round.
    get_option_chain snaps this to the nearest real exchange strike >= target, which
    fixes low-priced stocks (PNB ₹109 +2% = ₹111.18 → real strike 112.5, not 150).
    CHOLAFIN at 1580 + 2% = 1611.6.
    """
    result = calculate_ce_strike(1580, "percent", 2, "CHOLAFIN")
    assert result == pytest.approx(1611.6)

def test_percent_ce_strike_low_priced_stock():
    """PNB ~₹108.96 + 2% should be ~₹111.14 — NOT rounded up to a dead ₹150 strike."""
    result = calculate_ce_strike(108.96, "percent", 2, "PNB")
    assert result == pytest.approx(111.1392)
    assert result < 115   # must stay near spot, not jump to 150

def test_nifty_strike_interval():
    """NIFTY strike interval should be 50."""
    assert get_strike_interval("NIFTY") == 50

def test_banknifty_strike_interval():
    """BANKNIFTY strike interval should be 100."""
    assert get_strike_interval("BANKNIFTY") == 100

def test_default_strike_interval():
    """Unknown stock should get default interval of 50."""
    assert get_strike_interval("INFY") == 50

def test_round_strike_up():
    assert round_strike_up(1611.6, 50) == 1650
    assert round_strike_up(24278, 50) == 24300
    assert round_strike_up(307.5, 50) == 350

def test_find_pe_strike_picks_highest_valid():
    """Should pick PE with highest premium that is still below CE premium."""
    option_chain = [
        {"strike": 1090, "premium": 11.1, "type": "PE"},
        {"strike": 1050, "premium": 8.5,  "type": "PE"},
        {"strike": 1000, "premium": 5.0,  "type": "PE"},
    ]
    strike, premium = find_pe_strike(option_chain, ce_sell_premium=13.85)
    assert strike == 1090
    assert premium == 11.1

def test_find_pe_strike_none_when_all_too_expensive():
    """If all PE premiums are higher than CE premium, return None."""
    option_chain = [
        {"strike": 1090, "premium": 15.0, "type": "PE"},
        {"strike": 1050, "premium": 20.0, "type": "PE"},
    ]
    strike, premium = find_pe_strike(option_chain, ce_sell_premium=13.85)
    assert strike is None
    assert premium is None

def test_find_pe_strike_skips_zero_volume():
    """A higher-premium PE with zero day volume is illiquid → skip it for the next."""
    option_chain = [
        {"strike": 1090, "premium": 11.1, "type": "PE", "volume": 0},     # illiquid
        {"strike": 1050, "premium": 8.5,  "type": "PE", "volume": 1200},  # liquid
    ]
    strike, premium = find_pe_strike(option_chain, ce_sell_premium=13.85)
    assert strike == 1050
    assert premium == 8.5


def test_find_pe_strike_skips_zero_premium():
    """A PE that never traded today (premium 0) is not tradeable."""
    option_chain = [
        {"strike": 1090, "premium": 0,    "type": "PE", "volume": 500},   # no trades
        {"strike": 1050, "premium": 8.5,  "type": "PE", "volume": 1200},
    ]
    strike, premium = find_pe_strike(option_chain, ce_sell_premium=13.85)
    assert strike == 1050


def test_find_pe_candidates_ranked_and_filtered():
    """Candidates are returned highest-premium first, with illiquid strikes dropped."""
    option_chain = [
        {"strike": 1090, "premium": 11.1, "type": "PE", "volume": 500},
        {"strike": 1050, "premium": 8.5,  "type": "PE", "volume": 0},     # illiquid - dropped
        {"strike": 1000, "premium": 5.0,  "type": "PE", "volume": 300},
    ]
    cands = find_pe_candidates(option_chain, ce_sell_premium=13.85)
    assert [c["strike"] for c in cands] == [1090, 1000]


def test_find_pe_candidates_volume_optional():
    """When volume isn't supplied, candidates are treated as liquid (back-compat)."""
    option_chain = [
        {"strike": 1090, "premium": 11.1, "type": "PE"},
        {"strike": 1050, "premium": 8.5,  "type": "PE"},
    ]
    cands = find_pe_candidates(option_chain, ce_sell_premium=13.85)
    assert [c["strike"] for c in cands] == [1090, 1050]


def test_find_pe_candidates_soft_volume_keeps_zero_volume():
    """
    With require_volume=False (the live path, which has a Market Depth backstop) a
    0-volume strike is KEPT — only zero-premium strikes are dropped. 5paisa can
    report 0 volume for a genuinely liquid strike, so the depth check must decide.
    """
    option_chain = [
        {"strike": 1090, "premium": 11.1, "type": "PE", "volume": 0},     # kept now
        {"strike": 1050, "premium": 8.5,  "type": "PE", "volume": 1200},
        {"strike": 1000, "premium": 0,    "type": "PE", "volume": 0},      # no premium -> dropped
    ]
    cands = find_pe_candidates(option_chain, ce_sell_premium=13.85, require_volume=False)
    assert [c["strike"] for c in cands] == [1090, 1050]


def test_find_pe_candidates_default_still_filters_volume():
    """Default (require_volume=True, used by the paper path) still drops 0-volume."""
    option_chain = [
        {"strike": 1090, "premium": 11.1, "type": "PE", "volume": 0},     # dropped
        {"strike": 1050, "premium": 8.5,  "type": "PE", "volume": 1200},
    ]
    cands = find_pe_candidates(option_chain, ce_sell_premium=13.85)
    assert [c["strike"] for c in cands] == [1050]


def test_validate_premium_condition_pass():
    """CE premium > PE premium should pass."""
    assert validate_premium_condition(13.85, 11.1) is True

def test_validate_premium_condition_fail():
    """CE premium <= PE premium should fail."""
    assert validate_premium_condition(11.1, 13.85) is False
    assert validate_premium_condition(11.1, 11.1) is False


# ── P&L Calculation Tests ─────────────────────────────────────────────────────

def test_pnl_profit_scenario():
    """
    Buy futures at 1126 exit at 1150 = +24
    Sell CE at 13.85 exit at 5.0 = +8.85 (sold high, bought back low)
    Buy PE at 11.1 exit at 8.0 = -3.1
    Total = 24 + 8.85 - 3.1 = 29.75
    """
    pnl = calculate_trade_pnl(1126, 1150, 13.85, 5.0, 11.1, 8.0)
    assert pnl == 29.75

def test_pnl_loss_scenario():
    """
    Buy futures at 1126 exit at 1100 = -26
    Sell CE at 13.85 exit at 20.0 = -6.15
    Buy PE at 11.1 exit at 15.0 = +3.9
    Total = -26 - 6.15 + 3.9 = -28.25
    """
    pnl = calculate_trade_pnl(1126, 1100, 13.85, 20.0, 11.1, 15.0)
    assert pnl == -28.25

def test_pnl_with_none_values():
    """None exit prices should be treated as 0 contribution."""
    pnl = calculate_trade_pnl(1126, None, 13.85, None, 11.1, None)
    assert pnl == 0.0


# ── Remote Order ID Tests ─────────────────────────────────────────────────────

def test_remote_order_id_format():
    """Remote order ID should start with stock name."""
    order_id = generate_remote_order_id("INFY_FUT")
    assert order_id.startswith("INFY_FUT_")
    assert len(order_id) > 10

def test_remote_order_ids_are_unique():
    """Two calls should never return the same ID."""
    id1 = generate_remote_order_id("NIFTY")
    id2 = generate_remote_order_id("NIFTY")
    assert id1 != id2


# ── Exchange Calendar Tests ───────────────────────────────────────────────────

def test_expiry_is_tuesday():
    """Fallback expiry should land on a Tuesday (weekday 1) — NSE's expiry day."""
    expiry = get_expiry_date(2026, 5)
    assert expiry.weekday() == 1

def test_expiry_is_last_tuesday():
    """May 2026 last Tuesday should be May 26."""
    expiry = get_expiry_date(2026, 5)
    assert expiry.day == 26

def test_next_expiry_is_after_current():
    """Next expiry should always be after current expiry."""
    current = get_current_expiry()
    nxt = get_next_expiry()
    assert nxt > current

def test_expiry_not_on_weekend():
    """Expiry should never fall on Saturday or Sunday."""
    for month in range(1, 13):
        expiry = get_expiry_date(2026, month)
        assert expiry.weekday() < 5
