import logging
import time
import traceback
from database import SessionLocal
from models.trade import Trade
from models.fixed_trades import FixedTrade
from models.watchlist import Watchlist
from models.settings import Settings
from models.log import Log
from broker import fivepaisa
from bot.strike_calculator import calculate_ce_strike, find_pe_strike, find_pe_candidates
from utils.exchange_calendar import get_current_expiry, get_next_expiry
from utils.helpers import generate_remote_order_id, calculate_trade_pnl, get_ist_now, to_naive

logger = logging.getLogger(__name__)

# 5paisa rejects plain market orders ("Kindly place algo limit order"), so every
# real order is placed as a *marketable limit* order: the price is set slightly in
# the favourable direction of the live price so it fills immediately, while capping
# the worst-case price at this buffer.
#
# Tiered by price: cheap instruments (<= ₹100, e.g. low-premium options) use a
# wider 1% so the ₹0.05 tick rounding doesn't shrink the reach to zero; pricier
# instruments (> ₹100, e.g. futures) use a tighter 0.5% since 0.5% there is
# already several ticks of reach.
LIMIT_ORDER_BUFFER_CHEAP = 0.01      # 1%  for LTP <= ₹100
LIMIT_ORDER_BUFFER = 0.005           # 0.5% for LTP >  ₹100
CHEAP_PRICE_THRESHOLD = 100

# After placing the exit (square-off) orders we wait this long before re-reading the
# broker's net positions, so the exchange has time to fill the marketable-limit
# orders. We trust the *position* read (not the "order accepted" reply) as the
# authoritative proof that a leg is actually flat before marking a trade closed.
SQUAREOFF_SETTLE_SECONDS = 3

# When a square-off can't be confirmed flat at the broker we retry — but never on
# every ~10s monitor cycle. We retry at most SQUAREOFF_MAX_ATTEMPTS times, spaced at
# least SQUAREOFF_RETRY_SECONDS apart, and only while a target is still hit (the
# monitor re-checks live P&L each cycle, so a price that drifts back to neutral
# simply stops the retries and the trade behaves like a normal open trade again).
# The client is emailed only once — on the first failure.
SQUAREOFF_RETRY_SECONDS = 120     # 2 minutes between retry attempts
SQUAREOFF_MAX_ATTEMPTS = 3        # "2-3 times" then leave it for manual close


def _round_tick(price):
    """Round to the nearest ₹0.05 tick (exchange requirement) and 2 decimals."""
    return round(round(price / 0.05) * 0.05, 2)


def _parse_close_time(value):
    """Parse an 'HH:MM' close time into (hour, minute). Falls back to 12:00."""
    try:
        h, m = str(value).split(":")
        return int(h), int(m)
    except (AttributeError, ValueError):
        return 12, 0


def _marketable_limit_price(ltp, side):
    """
    Limit price that behaves like a market order but caps slippage.
    Buy ("B"): a buffer above LTP; Sell ("S"): a buffer below. Rounded to tick.
    Falls back to 0 (market) only if LTP is unavailable.
    """
    try:
        ltp = float(ltp)
    except (TypeError, ValueError):
        return 0
    if ltp <= 0:
        return 0
    buffer = LIMIT_ORDER_BUFFER_CHEAP if ltp <= CHEAP_PRICE_THRESHOLD else LIMIT_ORDER_BUFFER
    factor = (1 + buffer) if side == "B" else (1 - buffer)
    return _round_tick(ltp * factor)


def _leg_ltp(db, settings, scrip_code, leg=""):
    """
    Fetch the current LTP for a single F&O leg, or None. Every failure mode is
    logged with a [SQUAREOFF:LTP] tag so a missing exit price can be traced to the
    exact leg and reason (no quote / fetch failed / empty result).
    """
    tag = f"[SQUAREOFF:LTP {leg} ({scrip_code})]"
    if not scrip_code:
        _save_log(db, "WARNING", f"{tag} no scrip code - cannot fetch exit LTP")
        return None
    try:
        q = fivepaisa.get_market_quote(
            settings.access_token,
            [{"exchange": "N", "exchange_type": "D", "scrip_code": scrip_code}]
        )
    except Exception as e:
        _save_log(db, "ERROR", f"{tag} get_market_quote CRASHED - {_exc_detail(e)}")
        return None
    if not q.get("success"):
        _save_log(db, "ERROR", f"{tag} quote fetch FAILED - {q.get('error')}")
        return None
    if not q.get("quotes"):
        _save_log(db, "ERROR", f"{tag} quote fetch returned NO quotes - cannot price exit")
        return None
    ltp = q["quotes"][0]["LastRate"]
    _save_log(db, "INFO", f"{tag} LTP = {ltp}")
    return ltp


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _save_log(db, level: str, message: str):
    log = Log(level=level, message=message)
    db.add(log)
    db.commit()
    logger.info(f"[{level}] {message}")


def _exc_detail(e) -> str:
    """
    Format an unexpected exception with its full traceback for logging, so a
    real-trade failure can be traced to the exact line. Use in `except` blocks:
    _save_log(db, "ERROR", f"... {_exc_detail(e)}").
    """
    return f"{e}\n{traceback.format_exc()}"


def _get_settings(db):
    return db.query(Settings).first()


def _is_in_watchlist(db, stock_name: str) -> bool:
    return db.query(Watchlist).filter(Watchlist.stock_name.ilike(stock_name)).first() is not None


def _record_screener_attempt(db, stock_name: str, is_paper: bool):
    """
    Mark that we attempted a screener trade for this stock today. Written once we
    commit to attempting (after watchlist + dedup checks), so a stock whose order
    is later rejected by the broker (e.g. insufficient margin) is NOT re-tried on
    every 5-minute cycle. A manual "Run Chartink Trades" click bypasses this.
    """
    mode = "PAPER" if is_paper else "LIVE"
    _save_log(db, "INFO", f"Screener attempt recorded: {stock_name.upper()} [{mode}]")


def _screener_attempted_today(db, stock_name: str, is_paper: bool) -> bool:
    """True if we already attempted this stock today in this mode (success OR failure)."""
    today_str = str(get_ist_now().date())
    mode = "PAPER" if is_paper else "LIVE"
    marker = f"Screener attempt recorded: {stock_name.upper()} [{mode}]"
    return db.query(Log).filter(
        Log.message == marker,
        Log.created_at >= today_str
    ).count() > 0


def _get_expiry(month_type: str):
    return get_next_expiry() if month_type == "next" else get_current_expiry()


def _extract_ce(option_chain):
    """
    Return the single CE option the chain was built around.
    get_option_chain() already selects the CE at the nearest valid strike to the
    requested one, so we use that directly instead of exact-matching a strike that
    may not exist on the exchange (e.g. a typed fixed strike of 1223 when only
    1220/1230 exist).
    """
    return next((o for o in option_chain if o.get("CPType") == "CE"), None)


def _fetch_futures_price(settings, futures_scrip_code):
    """Return the live futures contract price (LTP) for entry recording, or None."""
    if not futures_scrip_code:
        return None
    q = fivepaisa.get_market_quote(
        settings.access_token,
        [{"exchange": "N", "exchange_type": "D", "scrip_code": futures_scrip_code}]
    )
    if q["success"] and q.get("quotes"):
        return q["quotes"][0]["LastRate"]
    return None


def _fetch_spot_price(settings, scrip_code):
    """Return the live equity spot price (LTP, ExchType 'C') for a stock, or None.
    Used to compute a percent-based strike for the naked-CE option path."""
    if not scrip_code:
        return None
    q = fivepaisa.get_market_quote(
        settings.access_token,
        [{"exchange": "N", "exchange_type": "C", "scrip_code": scrip_code}]
    )
    if q["success"] and q.get("quotes"):
        return q["quotes"][0]["LastRate"]
    return None


def _confirm_fills(settings, db, stock_name, legs):
    """
    Check fill status for each leg immediately after placement (1-second delay
    gives the exchange time to process marketable-limit orders).

    legs: list of (remote_order_id, leg_name)
    Returns (filled_legs, unfilled_legs) where each item is a leg_name string.

    Status "Fully Executed" → filled. Anything else (Pending, Rejected by Exch,
    Cancelled, etc.) → unfilled → client must handle manually.
    """
    time.sleep(1)  # give the exchange 1 second to process
    filled, unfilled = [], []
    for remote_order_id, leg_name in legs:
        try:
            result = fivepaisa.get_order_status(
                settings.access_token, settings.client_code, "N", remote_order_id
            )
            if not result["success"]:
                _save_log(db, "ERROR",
                    f"{stock_name}: [{leg_name}] order-status check failed — {result['error']}")
                unfilled.append(f"{leg_name} (status-check failed: {result['error'][:80]})")
                continue

            orders = result.get("orders", [])
            if not orders:
                _save_log(db, "INFO",
                    f"{stock_name}: [{leg_name}] order status = no records (RemoteOrderID not found yet)")
                unfilled.append(f"{leg_name} (no status record)")
                continue
            # 5paisa may return multiple history entries per order (e.g. Modified → Fully Executed).
            # Check all entries — if any shows "Fully Executed" the order is filled.
            if any(o.get("Status") == "Fully Executed" for o in orders):
                final_status = "Fully Executed"
            else:
                final_status = orders[-1].get("Status", "Unknown")  # most recent entry last
            _save_log(db, "INFO",
                f"{stock_name}: [{leg_name}] order status = {final_status} ({len(orders)} record(s))")
            if final_status == "Fully Executed":
                filled.append(leg_name)
            else:
                unfilled.append(f"{leg_name} (status: {final_status})")
        except Exception as e:
            _save_log(db, "ERROR",
                f"{stock_name}: [{leg_name}] order-status check crashed — {_exc_detail(e)}")
            unfilled.append(f"{leg_name} (status-check crashed)")

    return filled, unfilled


def _pick_liquid_pe(db, settings, stock_name, option_chain, ce_premium, max_checks=6):
    """
    Pick the highest-premium PE (still below the CE premium) that is actually
    TRADEABLE, so the buy never gets rejected as an "illiquid contract".

    Two-stage liquidity guard:
      1. Keep every PE strike priced below the CE premium (premium > 0). Day volume
         is only a SOFT signal here (require_volume=False): 5paisa's feed can report
         0 volume even for a liquid strike, so we must NOT drop those before the
         authoritative depth check below.
      2. For the candidates (best premium first), confirm via the level-5 Market
         Depth order book that there are live SELL-side offers (BbBuySellFlag 83,
         qty > 0) to buy from; take the first that passes. This is the real gate.

    Only the PE leg uses this — futures and the CE sell are unchanged.
    Returns (pe_strike, pe_premium, pe_scrip_code) or (None, None, None).
    """
    pe_inputs = [
        {"strike": o["StrikeRate"], "premium": o.get("LastRate", 0),
         "type": "PE", "volume": o.get("TotalQty", 0)}
        for o in option_chain if o.get("CPType") == "PE"
    ]
    # require_volume=False: the Market Depth check (stage 2) is the real liquidity
    # gate, so a 0-volume reading must not eliminate an otherwise-tradeable strike.
    candidates = find_pe_candidates(pe_inputs, ce_premium, require_volume=False)
    if not candidates:
        _save_log(db, "ERROR",
            f"{stock_name}: no PE strike priced below CE premium {ce_premium}")
        return None, None, None

    scrip_by_strike = {
        o["StrikeRate"]: str(o.get("Scripcode", ""))
        for o in option_chain if o.get("CPType") == "PE"
    }

    for cand in candidates[:max_checks]:
        strike = cand["strike"]
        scrip_code = scrip_by_strike.get(strike, "")
        if not scrip_code:
            continue
        depth = fivepaisa.get_market_depth(
            settings.access_token, settings.client_code, "N", "D", scrip_code)
        if not depth["success"]:
            _save_log(db, "INFO",
                f"{stock_name}: PE {strike} skipped — depth check failed ({depth['error'][:80]})")
            continue
        # BbBuySellFlag 83 = sell-side/offers (66 = bids). We BUY the PE, so we need
        # offers to lift. Flag may arrive as int/float/string — compare numerically.
        def _is_offer(d):
            try:
                return int(float(d.get("BbBuySellFlag", 0))) == 83 and (d.get("Quantity") or 0) > 0
            except (ValueError, TypeError):
                return False
        offers = [d for d in depth["depth"] if _is_offer(d)]
        if offers:
            _save_log(db, "INFO",
                f"{stock_name}: PE {strike} confirmed tradeable — {len(offers)} ask level(s), premium {cand['premium']}")
            return strike, cand["premium"], scrip_code
        _save_log(db, "INFO",
            f"{stock_name}: PE {strike} skipped — no live sell-side offers (illiquid)")

    _save_log(db, "ERROR",
        f"{stock_name}: no tradeable PE confirmed via market depth below CE premium {ce_premium}")
    return None, None, None


def _finalize_live_collar(db, settings, *, stock_name, trade_source, fixed_trade_id,
                          month_type, lot_size, expiry_date, profit_target, loss_limit,
                          legs, ce_strike=None, pe_strike=None):
    """
    Save a real (live) collar trade tracking ONLY the legs that actually opened.

    For each attempted leg we check two things: (1) broker RMS acceptance from the
    place_order result and (2) the real exchange fill via OrderStatus. We then save
    a Trade populated with ONLY the confirmed-open legs — any leg that was rejected
    or never filled is left empty, exactly like a naked-CE trade. This means a
    partial fill is no longer thrown away: the legs that DID open are tracked
    normally (live P&L, profit-target / stop / expiry close), and the client is
    emailed about the leg(s) that didn't open so they can add them manually.

    legs: list of dicts, each {"name","side","scrip","entry","result","remote"}.
          "result" is the place_order dict; "remote" the RemoteOrderID used.
    Returns the saved Trade, or None if nothing opened.
    """
    # 1. Broker (RMS) acceptance — split into accepted vs rejected.
    accepted, problems = [], []
    for lg in legs:
        if lg["result"]["success"]:
            accepted.append((lg["remote"], lg["name"]))
        else:
            problems.append(f"{lg['name']}: {lg['result']['error']}")

    # 2. Real exchange fill confirmation for the accepted legs.
    filled = []
    if accepted:
        filled, unfilled = _confirm_fills(settings, db, stock_name, accepted)
        if unfilled:
            problems.append(f"not fully executed: {', '.join(unfilled)}")

    if not filled:
        _save_log(db, "ERROR",
            f"{stock_name}: no legs opened — {'; '.join(problems) or 'unknown reason'}. Nothing to track.")
        if problems:
            try:
                from notifications.email import send_partial_fill_alert
                send_partial_fill_alert(stock_name, [], problems)
            except Exception as e:
                _save_log(db, "ERROR", f"{stock_name}: partial-fill alert email FAILED - {_exc_detail(e)}")
        return None

    by = {lg["name"]: lg for lg in legs}

    def _field(name, key):
        return by[name][key] if (name in filled and name in by) else None

    def _order_id(name):
        return str(by[name]["result"]["broker_order_id"]) if (name in filled and name in by) else None

    trade = Trade(
        stock_name=stock_name.upper(),
        trade_source=trade_source,
        fixed_trade_id=fixed_trade_id,
        is_paper_trade=False,
        month_type=month_type,
        lot_size=lot_size,
        futures_scrip_code=_field("FUT", "scrip"),
        ce_scrip_code=_field("CE", "scrip"),
        pe_scrip_code=_field("PE", "scrip"),
        futures_broker_order_id=_order_id("FUT"),
        ce_broker_order_id=_order_id("CE"),
        pe_broker_order_id=_order_id("PE"),
        futures_entry_price=_field("FUT", "entry"),
        ce_entry_price=_field("CE", "entry"),
        pe_entry_price=_field("PE", "entry"),
        expiry_date=expiry_date,
        profit_target=profit_target,
        loss_limit=loss_limit,
        status="open",
    )
    db.add(trade)

    if problems:
        _save_log(db, "ERROR",
            f"{stock_name}: PARTIAL FILL — tracking open legs {filled}; NOT opened: {'; '.join(problems)}. "
            f"The open legs are now tracked normally; client adds the missing leg(s) manually. Alert email sent.")
    else:
        _save_log(db, "INFO",
            f"{stock_name}: collar placed (LIVE) — legs {filled}, "
            f"Futures@{_field('FUT', 'entry')}, CE {ce_strike}@{_field('CE', 'entry')} sold, "
            f"PE {pe_strike}@{_field('PE', 'entry')} bought, lot {lot_size}")
    db.commit()

    # Notify: partial → action-needed alert; full → trade-opened confirmation.
    if problems:
        side_word = {"B": "Buy", "S": "Sell"}
        filled_desc = [
            f"{n} ({side_word.get(by[n]['side'], by[n]['side'])}) — scrip {by[n]['scrip']}"
            for n in filled
        ]
        try:
            from notifications.email import send_partial_fill_alert
            send_partial_fill_alert(stock_name, filled_desc, problems)
        except Exception as e:
            _save_log(db, "ERROR", f"{stock_name}: partial-fill alert email FAILED - {_exc_detail(e)}")
    else:
        try:
            from notifications.email import send_trade_opened_email
            send_trade_opened_email(stock_name, _field("FUT", "entry") or 0, ce_strike or 0,
                                    _field("CE", "entry") or 0, pe_strike or 0, _field("PE", "entry") or 0)
        except Exception as e:
            _save_log(db, "WARNING", f"{stock_name}: trade-opened email failed - {_exc_detail(e)}")

    return trade


# ─── Fixed Trades (Number 3) ───────────────────────────────────────────────────

def run_fixed_trades():
    """
    Process all active fixed trades. Called by scheduler at 9:30 AM.
    Checks watchlist, calculates strikes, places orders.
    """
    db = SessionLocal()
    try:
        settings = _get_settings(db)

        if not settings or not settings.is_trading:
            _save_log(db, "INFO", "Fixed trades skipped: trading is stopped")
            return

        if not settings.access_token:
            _save_log(db, "ERROR", "Fixed trades skipped: broker not connected")
            return

        fixed_trades = db.query(FixedTrade).filter(FixedTrade.is_active == True).all()
        _save_log(db, "INFO", f"Processing {len(fixed_trades)} fixed trades")

        for ft in fixed_trades:
            try:
                _process_fixed_trade(db, settings, ft)
            except Exception as e:
                _save_log(db, "ERROR", f"Unexpected error for {ft.stock_name}: {_exc_detail(e)}")
    finally:
        db.close()


def run_single_fixed_trade(trade_id: int):
    """
    Manually fire ONE fixed trade by id, right now. Uses the exact same guards and
    the same _process_fixed_trade path the scheduler uses at the start time — so the
    watchlist check, the "skip if an open trade already exists" de-dup, the PE
    liquidity selection, partial-fill tracking and real order placement are all
    identical. Only the scope (one row vs all) differs.
    """
    db = SessionLocal()
    try:
        settings = _get_settings(db)

        if not settings or not settings.is_trading:
            _save_log(db, "INFO", "Manual run skipped: trading is stopped")
            return

        if not settings.access_token:
            _save_log(db, "ERROR", "Manual run skipped: broker not connected")
            return

        ft = db.query(FixedTrade).filter(FixedTrade.id == trade_id).first()
        if not ft:
            _save_log(db, "ERROR", f"Manual run skipped: fixed trade {trade_id} not found")
            return
        if not ft.is_active:
            _save_log(db, "INFO", f"{ft.stock_name} manual run skipped: trade is not active")
            return

        _save_log(db, "INFO", f"Manually running fixed trade: {ft.stock_name}")
        try:
            _process_fixed_trade(db, settings, ft)
        except Exception as e:
            _save_log(db, "ERROR", f"Unexpected error for {ft.stock_name}: {_exc_detail(e)}")
    finally:
        db.close()


def _process_fixed_trade(db, settings, ft: FixedTrade):
    """Decide how to handle a single fixed trade row."""

    # Check Number 2 watchlist first
    if not _is_in_watchlist(db, ft.stock_name):
        _save_log(db, "INFO", f"{ft.stock_name} skipped: not in watchlist")
        return

    # Skip if already have an open trade for this stock
    existing = db.query(Trade).filter(
        Trade.stock_name.ilike(ft.stock_name),
        Trade.status == "open"
    ).first()
    if existing:
        _save_log(db, "INFO", f"{ft.stock_name} skipped: open trade already exists")
        return

    if ft.month_type == "option":
        _place_option_trade(db, settings, ft)
    elif not ft.is_trade:
        _place_paper_trade(db, settings, ft)
    else:
        _place_collar_trade(db, settings, ft)


def _place_collar_trade(db, settings, ft: FixedTrade):
    """Buy Futures + Sell CE + Buy PE (the main 3-legged strategy)."""

    expiry = _get_expiry(ft.month_type)
    expiry_str = expiry.strftime("%Y%m%d")

    watchlist_stock = db.query(Watchlist).filter(Watchlist.stock_name.ilike(ft.stock_name)).first()

    # Step 1: Get live spot price (equity scrip code from watchlist, ExchType C)
    quote_result = fivepaisa.get_market_quote(
        settings.access_token,
        [{"exchange": "N", "exchange_type": "C", "scrip_code": watchlist_stock.scrip_code}]
    )
    if not quote_result["success"]:
        _save_log(db, "ERROR", f"{ft.stock_name}: failed to get futures price - {quote_result['error']}")
        return

    quotes = quote_result.get("quotes", [])
    if not quotes:
        _save_log(db, "ERROR", f"{ft.stock_name}: market quote returned empty data — check scrip code in watchlist")
        return
    futures_price = quotes[0]["LastRate"]

    # Step 2: Calculate CE strike
    ce_strike = calculate_ce_strike(futures_price, ft.strike_type, ft.strike_value, ft.stock_name)

    # Step 3: Get option chain
    chain_result = fivepaisa.get_option_chain(settings.access_token, ft.stock_name, expiry, ce_strike)
    if not chain_result["success"]:
        _save_log(db, "ERROR", f"{ft.stock_name}: failed to get option chain - {chain_result['error']}")
        return

    option_chain = chain_result["option_chain"]

    # Step 4: Use the CE the chain selected (nearest valid strike) — extract scrip code and premium
    ce_option = _extract_ce(option_chain)
    if not ce_option:
        _save_log(db, "ERROR", f"{ft.stock_name}: no CE option returned in chain near strike {ce_strike}")
        return

    ce_strike = ce_option.get("StrikeRate", ce_strike)  # actual exchange strike
    ce_scrip_code = str(ce_option.get("Scripcode", ""))
    ce_premium = ce_option.get("LastRate", 0)

    if not ce_scrip_code:
        _save_log(db, "ERROR", f"{ft.stock_name}: CE option has no scrip code in chain response")
        return

    if not ce_premium or ce_premium <= 0:
        _save_log(db, "ERROR",
            f"{ft.stock_name}: CE strike {ce_strike} has no premium (0) — strike is "
            f"illiquid / too far OTM; check the strike settings for this stock")
        return

    # Step 5: Find best PE strike (premium must be lower than CE premium)
    # Steps 5-7: pick the highest-premium PE below the CE premium that is actually
    # tradeable (skips zero-volume strikes + confirms live offers via market depth),
    # so the PE buy never gets rejected as an illiquid contract.
    pe_strike, pe_premium, pe_scrip_code = _pick_liquid_pe(
        db, settings, ft.stock_name, option_chain, ce_premium)
    if pe_strike is None:
        return  # _pick_liquid_pe already logged the reason

    # Step 8: Resolve the real futures contract scrip code (not the equity code)
    futures_scrip_code = fivepaisa.get_futures_scrip_code(ft.stock_name, expiry)
    if not futures_scrip_code:
        _save_log(db, "ERROR", f"{ft.stock_name}: futures contract not found in scrip master for expiry {expiry}")
        return

    # Record the actual futures contract price for entry (matches what the monitor reads)
    fut_price = _fetch_futures_price(settings, futures_scrip_code)
    if fut_price is not None:
        futures_price = fut_price

    # Total quantity (shares) = number of lots × contract size from the scrip master.
    # This drives both the order quantity and the rupee P&L.
    contract_size = fivepaisa.get_lot_size(ft.stock_name, expiry) or 1
    lot_size = (ft.lot_size or 1) * contract_size

    # Fire all 3 legs at once (same as screener path).
    fut_remote_id = generate_remote_order_id(ft.stock_name + "_FUT")
    ce_remote_id  = generate_remote_order_id(ft.stock_name + "_CE")
    pe_remote_id  = generate_remote_order_id(ft.stock_name + "_PE")

    futures_result = fivepaisa.place_order(
        settings.access_token, "N", "D", futures_scrip_code, "B",
        _marketable_limit_price(futures_price, "B"), lot_size, False, fut_remote_id
    )
    ce_result = fivepaisa.place_order(
        settings.access_token, "N", "D", ce_scrip_code, "S",
        _marketable_limit_price(ce_premium, "S"), lot_size, False, ce_remote_id
    )
    pe_result = fivepaisa.place_order(
        settings.access_token, "N", "D", pe_scrip_code, "B",
        _marketable_limit_price(pe_premium, "B"), lot_size, False, pe_remote_id
    )

    # Check which were accepted by the broker (RMS-level check)
    # Save & track whatever actually opened. A partial fill is no longer discarded:
    # the legs that filled are tracked normally, the rest are left empty (like a
    # naked-CE trade) and the client is alerted to handle them manually.
    _finalize_live_collar(
        db, settings,
        stock_name=ft.stock_name,
        trade_source="fixed",
        fixed_trade_id=ft.id,
        month_type=ft.month_type,
        lot_size=lot_size,
        expiry_date=chain_result.get("expiry"),
        profit_target=ft.profit_target,
        loss_limit=ft.loss_limit,
        legs=[
            {"name": "FUT", "side": "B", "scrip": futures_scrip_code, "entry": futures_price, "result": futures_result, "remote": fut_remote_id},
            {"name": "CE",  "side": "S", "scrip": ce_scrip_code,      "entry": ce_premium,    "result": ce_result,      "remote": ce_remote_id},
            {"name": "PE",  "side": "B", "scrip": pe_scrip_code,      "entry": pe_premium,    "result": pe_result,      "remote": pe_remote_id},
        ],
        ce_strike=ce_strike,
        pe_strike=pe_strike,
    )


def _place_option_trade(db, settings, ft: FixedTrade):
    """
    Sell CE only (no futures, no PE). Used for month_type = "option".
    Supports BOTH fixed strike and percent (X% above live spot) — the same strike
    logic as the collar/screener paths. (Previously this path ignored strike_type
    and always treated the value as a literal strike, so "percent" silently broke.)
    """
    expiry = get_current_expiry()

    # Compute the CE strike. "fixed" uses the value directly; "percent" needs the
    # live spot price (X% above spot), so fetch it first.
    spot = None
    if ft.strike_type == "percent":
        spot = _fetch_spot_price(settings, ft.scrip_code)
        if spot is None:
            _save_log(db, "ERROR", f"{ft.stock_name}: could not get spot price for percent strike — skipping")
            return
    ce_strike = calculate_ce_strike(spot or 0, ft.strike_type, ft.strike_value, ft.stock_name)

    # Fetch option chain to get the real numeric scrip code for this CE strike
    chain_result = fivepaisa.get_option_chain(settings.access_token, ft.stock_name, expiry, ce_strike)
    if not chain_result["success"]:
        _save_log(db, "ERROR", f"{ft.stock_name}: option trade failed to get chain - {chain_result['error']}")
        return

    ce_option = _extract_ce(chain_result["option_chain"])
    if not ce_option:
        _save_log(db, "ERROR", f"{ft.stock_name}: no CE option returned in chain near strike {ce_strike}")
        return

    ce_strike = ce_option.get("StrikeRate", ce_strike)  # actual exchange strike
    ce_scrip_code = str(ce_option.get("Scripcode", ""))
    if not ce_scrip_code:
        _save_log(db, "ERROR", f"{ft.stock_name}: CE option has no scrip code")
        return

    contract_size = fivepaisa.get_lot_size(ft.stock_name, expiry) or 1
    lot_size = (ft.lot_size or 1) * contract_size

    # Respect the Paper/Live setting — a naked-CE row marked Paper must NOT place a
    # real order (previously this path ignored ft.is_trade and always traded live).
    is_paper = not ft.is_trade
    ce_premium = ce_option.get("LastRate", 0)

    if not ce_premium or ce_premium <= 0:
        _save_log(db, "ERROR",
            f"{ft.stock_name}: CE strike {ce_strike} has no premium (0) — strike is "
            f"illiquid / too far OTM; check the strike settings for this stock")
        return

    ce_order_id = None
    if not is_paper:
        ce_result = fivepaisa.place_order(
            settings.access_token, "N", "D", ce_scrip_code, "S",
            _marketable_limit_price(ce_premium, "S"), lot_size, False,
            generate_remote_order_id(ft.stock_name + "_CE")
        )
        if not ce_result["success"]:
            _save_log(db, "ERROR", f"{ft.stock_name}: option CE sell failed - {ce_result['error']}")
            return
        ce_order_id = str(ce_result["broker_order_id"])

    trade = Trade(
        stock_name=ft.stock_name,
        trade_source="fixed",
        fixed_trade_id=ft.id,
        is_paper_trade=is_paper,
        month_type="option",
        lot_size=lot_size,
        ce_scrip_code=ce_scrip_code,
        ce_broker_order_id=ce_order_id,
        ce_entry_price=ce_option.get("LastRate", ce_strike),
        expiry_date=chain_result.get("expiry"),
        profit_target=ft.profit_target,
        loss_limit=ft.loss_limit,
        status="open"
    )
    db.add(trade)
    mode = "PAPER" if is_paper else "LIVE"
    _save_log(db, "INFO", f"{ft.stock_name}: option CE sell ({mode}) at strike {ce_strike}, scrip {ce_scrip_code}")
    db.commit()


def _place_paper_trade(db, settings, ft: FixedTrade):
    """Record prices without placing real orders. Used when Trade = No."""

    # Try to get live price; fall back to configured strike for fixed type
    futures_price = None
    watchlist_stock = db.query(Watchlist).filter(Watchlist.stock_name.ilike(ft.stock_name)).first()
    if watchlist_stock:
        quote_result = fivepaisa.get_market_quote(
            settings.access_token,
            [{"exchange": "N", "exchange_type": "C", "scrip_code": watchlist_stock.scrip_code}]
        )
        if quote_result["success"]:
            quotes = quote_result.get("quotes", [])
            if quotes:
                futures_price = quotes[0]["LastRate"]

    if futures_price is None:
        if ft.strike_type == "fixed":
            # For fixed strike, use the configured strike as the reference price
            futures_price = ft.strike_value
            _save_log(db, "WARNING", f"{ft.stock_name}: live price unavailable, using configured strike {futures_price} as paper trade entry")
        else:
            _save_log(db, "ERROR", f"{ft.stock_name}: paper trade skipped — live price unavailable and strike_type is percent (cannot calculate strike without live price)")
            return

    ce_strike = calculate_ce_strike(futures_price, ft.strike_type, ft.strike_value, ft.stock_name)
    expiry = _get_expiry(ft.month_type)

    # Resolve real scrip codes so the monitor can track live P&L on the paper trade
    futures_scrip_code = fivepaisa.get_futures_scrip_code(ft.stock_name, expiry)
    # Record the actual futures contract price as the entry (matches what the monitor reads)
    fut_price = _fetch_futures_price(settings, futures_scrip_code)
    if fut_price is not None:
        futures_price = fut_price

    # Total quantity (shares) = number of lots × contract size, so paper P&L is in real rupees
    contract_size = fivepaisa.get_lot_size(ft.stock_name, expiry) or 1
    total_qty = (ft.lot_size or 1) * contract_size

    # Try to fetch option chain to get real CE and PE premiums + scrip codes
    ce_premium = ce_strike  # fallback to strike value if chain unavailable
    pe_premium = None
    ce_scrip_code = None
    pe_scrip_code = None
    chain_result = fivepaisa.get_option_chain(settings.access_token, ft.stock_name, expiry, ce_strike)
    if chain_result["success"]:
        option_chain = chain_result["option_chain"]
        ce_option = _extract_ce(option_chain)
        if ce_option:
            ce_strike = ce_option.get("StrikeRate", ce_strike)  # actual exchange strike
            ce_premium = ce_option.get("LastRate", ce_strike)
            ce_scrip_code = str(ce_option.get("Scripcode", "")) or None

        pe_options = [
            {"strike": o["StrikeRate"], "premium": o["LastRate"], "type": "PE",
             "volume": o.get("TotalQty", 0)}
            for o in option_chain if o.get("CPType") == "PE"
        ]
        pe_strike, pe_prem = find_pe_strike(pe_options, ce_premium)
        if pe_strike is not None:
            pe_premium = pe_prem
            pe_option = next(
                (o for o in option_chain if o.get("StrikeRate") == pe_strike and o.get("CPType") == "PE"),
                None
            )
            if pe_option:
                pe_scrip_code = str(pe_option.get("Scripcode", "")) or None

    trade = Trade(
        stock_name=ft.stock_name,
        trade_source="fixed",
        fixed_trade_id=ft.id,
        is_paper_trade=True,
        month_type=ft.month_type,
        lot_size=total_qty,
        expiry_date=chain_result.get("expiry"),
        futures_scrip_code=futures_scrip_code,
        ce_scrip_code=ce_scrip_code,
        pe_scrip_code=pe_scrip_code,
        futures_entry_price=futures_price,
        ce_entry_price=ce_premium,
        pe_entry_price=pe_premium,
        profit_target=ft.profit_target,
        loss_limit=ft.loss_limit,
        status="open"
    )
    db.add(trade)
    _save_log(db, "INFO", f"{ft.stock_name}: paper trade recorded — entry {futures_price}, CE {ce_strike}@{ce_premium}, PE @{pe_premium}")
    db.commit()


# ─── Webhook Trades (Number 4) ────────────────────────────────────────────────

def run_webhook_trade(stock_name: str, force: bool = False):
    """
    Place a trade triggered by a Chartink screener match.
    Uses the global webhook configuration from Settings — same config applies to ALL signals.

    force=True (manual "Run Chartink Trades" button) bypasses the once-per-day
    attempted guard so a stock can be retried after, e.g., margin is topped up.
    The open-trade dedup still applies even when forced — we never stack two open
    collars on the same stock.
    """
    db = SessionLocal()
    try:
        settings = _get_settings(db)

        if not settings or not settings.is_trading or not settings.access_token:
            _save_log(db, "ERROR", f"Webhook trade for {stock_name} skipped: trading stopped or broker disconnected")
            return

        # Paper mode for screener trades — when ON, NO real orders are ever placed;
        # the trade is recorded on real live data only (same as fixed-trade paper).
        is_paper = bool(getattr(settings, "webhook_is_paper", False))

        existing = db.query(Trade).filter(
            Trade.stock_name.ilike(stock_name),
            Trade.status == "open"
        ).first()
        if existing:
            _save_log(db, "INFO", f"Screener trade for {stock_name} skipped: open trade already exists")
            return

        # Once per day per stock — if we already ATTEMPTED this stock today (a
        # successful trade OR an order rejected by the broker), do not try again
        # today. This stops a stock whose order keeps getting rejected (e.g.
        # insufficient margin) from being re-fired every 5-minute scan. A manual
        # "Run Chartink Trades" click passes force=True to override this.
        if not force and _screener_attempted_today(db, stock_name, is_paper):
            _save_log(db, "INFO", f"Screener trade for {stock_name} skipped: already attempted today (once per day)")
            return

        watchlist_stock = db.query(Watchlist).filter(Watchlist.stock_name.ilike(stock_name)).first()
        if not watchlist_stock:
            _save_log(db, "INFO", f"Screener trade for {stock_name} skipped: not in watchlist")
            return

        # We are now committed to attempting this stock — record it so automatic
        # scans won't retry it today regardless of the outcome below.
        _record_screener_attempt(db, stock_name, is_paper)

        # Read global webhook config from settings
        trade_type   = settings.webhook_trade_type or "collar"    # "collar" or "option"
        strike_type  = settings.webhook_strike_type or "percent"
        strike_value = settings.webhook_strike_value or 2
        num_lots     = settings.webhook_lot_size or 1
        month_type   = settings.webhook_month_type or "current"
        profit_target = settings.webhook_profit_target or 15000
        loss_limit    = settings.webhook_loss_limit or 12000

        expiry = _get_expiry(month_type)

        # Total quantity (shares) = number of lots × contract size from the scrip master
        lot_size = num_lots * (fivepaisa.get_lot_size(stock_name, expiry) or 1)

        # Get current futures price
        quote_result = fivepaisa.get_market_quote(
            settings.access_token,
            [{"exchange": "N", "exchange_type": "C", "scrip_code": watchlist_stock.scrip_code}]
        )
        if not quote_result["success"]:
            _save_log(db, "ERROR", f"Webhook {stock_name}: failed to get futures price")
            return

        quotes = quote_result.get("quotes", [])
        if not quotes:
            _save_log(db, "ERROR", f"Webhook {stock_name}: market quote returned empty data — check scrip code in watchlist")
            return
        futures_price = quotes[0]["LastRate"]
        ce_strike = calculate_ce_strike(futures_price, strike_type, strike_value, stock_name)

        # Get option chain to find real scrip codes and premiums
        chain_result = fivepaisa.get_option_chain(settings.access_token, stock_name, expiry, ce_strike)
        if not chain_result["success"]:
            _save_log(db, "ERROR", f"Webhook {stock_name}: failed to get option chain")
            return

        option_chain = chain_result["option_chain"]

        ce_option = _extract_ce(option_chain)
        if not ce_option:
            _save_log(db, "ERROR", f"Webhook {stock_name}: no CE option returned in chain near strike {ce_strike}")
            return

        ce_strike = ce_option.get("StrikeRate", ce_strike)  # actual exchange strike
        ce_scrip_code = str(ce_option.get("Scripcode", ""))
        ce_premium = ce_option.get("LastRate", 0)

        if not ce_scrip_code:
            _save_log(db, "ERROR", f"Webhook {stock_name}: CE option has no scrip code")
            return

        if not ce_premium or ce_premium <= 0:
            _save_log(db, "ERROR",
                f"Webhook {stock_name}: CE strike {ce_strike} has no premium (0) — strike is "
                f"illiquid / too far OTM; check the strike settings for this stock")
            return

        # ── Option trade: naked CE sell only ────────────────────────────────
        if trade_type == "option":
            ce_order_id = None
            if not is_paper:
                ce_result = fivepaisa.place_order(
                    settings.access_token, "N", "D", ce_scrip_code, "S",
                    _marketable_limit_price(ce_premium, "S"), lot_size, False,
                    generate_remote_order_id(stock_name + "_CE")
                )
                if not ce_result["success"]:
                    _save_log(db, "ERROR", f"Screener {stock_name}: CE sell failed - {ce_result['error']}")
                    return
                ce_order_id = str(ce_result["broker_order_id"])

            trade = Trade(
                stock_name=stock_name.upper(),
                trade_source="webhook",
                is_paper_trade=is_paper,
                month_type=month_type,
                lot_size=lot_size,
                ce_scrip_code=ce_scrip_code,
                ce_broker_order_id=ce_order_id,
                ce_entry_price=ce_premium,
                expiry_date=chain_result.get("expiry"),
                profit_target=profit_target,
                loss_limit=loss_limit,
                status="open"
            )
            db.add(trade)
            mode = "PAPER" if is_paper else "LIVE"
            _save_log(db, "INFO", f"Screener {stock_name}: naked CE sell ({mode}) — strike {ce_strike}, premium {ce_premium}, lot {lot_size}")
            db.commit()
            return

        # ── Collar trade: Buy Futures + Sell CE + Buy PE ────────────────────
        # Pick the highest-premium PE below the CE premium that is actually tradeable
        # (skips zero-volume strikes + confirms live offers via market depth), so the
        # PE buy never gets rejected as an illiquid contract.
        pe_strike, pe_premium, pe_scrip_code = _pick_liquid_pe(
            db, settings, stock_name, option_chain, ce_premium)
        if pe_strike is None:
            return  # _pick_liquid_pe already logged the reason

        futures_scrip_code = fivepaisa.get_futures_scrip_code(stock_name, expiry)
        if not futures_scrip_code:
            _save_log(db, "ERROR", f"Webhook {stock_name}: futures contract not found in scrip master for expiry {expiry}")
            return

        # Record the actual futures contract price for entry (matches what the monitor reads)
        fut_price = _fetch_futures_price(settings, futures_scrip_code)
        if fut_price is not None:
            futures_price = fut_price

        if not is_paper:
            # Capture remote IDs before placement — same IDs reused for fill-confirmation
            fut_remote_id = generate_remote_order_id(stock_name + "_FUT")
            ce_remote_id  = generate_remote_order_id(stock_name + "_CE")
            pe_remote_id  = generate_remote_order_id(stock_name + "_PE")

            futures_result = fivepaisa.place_order(
                settings.access_token, "N", "D", futures_scrip_code, "B",
                _marketable_limit_price(futures_price, "B"), lot_size, False, fut_remote_id
            )
            ce_result = fivepaisa.place_order(
                settings.access_token, "N", "D", ce_scrip_code, "S",
                _marketable_limit_price(ce_premium, "S"), lot_size, False, ce_remote_id
            )
            pe_result = fivepaisa.place_order(
                settings.access_token, "N", "D", pe_scrip_code, "B",
                _marketable_limit_price(pe_premium, "B"), lot_size, False, pe_remote_id
            )

            # Save & track whatever actually opened — partial fills are kept (only the
            # filled legs are tracked), and the client is alerted about the rest.
            _finalize_live_collar(
                db, settings,
                stock_name=stock_name,
                trade_source="webhook",
                fixed_trade_id=None,
                month_type=month_type,
                lot_size=lot_size,
                expiry_date=chain_result.get("expiry"),
                profit_target=profit_target,
                loss_limit=loss_limit,
                legs=[
                    {"name": "FUT", "side": "B", "scrip": futures_scrip_code, "entry": futures_price, "result": futures_result, "remote": fut_remote_id},
                    {"name": "CE",  "side": "S", "scrip": ce_scrip_code,      "entry": ce_premium,    "result": ce_result,      "remote": ce_remote_id},
                    {"name": "PE",  "side": "B", "scrip": pe_scrip_code,      "entry": pe_premium,    "result": pe_result,      "remote": pe_remote_id},
                ],
                ce_strike=ce_strike,
                pe_strike=pe_strike,
            )
            return

        # Paper trade — no real order, record all legs at their reference prices.
        trade = Trade(
            stock_name=stock_name.upper(),
            trade_source="webhook",
            is_paper_trade=True,
            month_type=month_type,
            lot_size=lot_size,
            futures_scrip_code=futures_scrip_code,
            ce_scrip_code=ce_scrip_code,
            pe_scrip_code=pe_scrip_code,
            futures_entry_price=futures_price,
            ce_entry_price=ce_premium,
            pe_entry_price=pe_premium,
            expiry_date=chain_result.get("expiry"),
            profit_target=profit_target,
            loss_limit=loss_limit,
            status="open"
        )
        db.add(trade)
        _save_log(db, "INFO",
            f"Screener {stock_name}: collar placed (PAPER) — Futures@{futures_price}, "
            f"CE {ce_strike}@{ce_premium} sold, PE {pe_strike}@{pe_premium} bought, lot {lot_size}")
        db.commit()

    finally:
        db.close()


def run_chartink_cycle(force: bool = False):
    """
    One Chartink polling cycle. Called by the scheduler every few minutes
    (force=False) and by the manual "Run Chartink Trades" button (force=True).
    Fires a configured trade for each watchlist match that hasn't already been
    traded/attempted today.

    Stocks already handled today are filtered out BEFORE the loop — those with an
    open trade (always), and those already attempted today (unless forced). This
    is what stops the cycle from re-processing, and re-logging "already attempted",
    the same stocks every 5 minutes. When nothing new remains, the cycle stays
    silent (no repeated summary spam).
    """
    db = SessionLocal()
    try:
        settings = _get_settings(db)
        if not settings or not settings.is_trading or not settings.access_token:
            return
        watchlist_names = {w.stock_name.upper() for w in db.query(Watchlist).all()}
        is_paper = bool(getattr(settings, "webhook_is_paper", False))
    finally:
        db.close()

    if not watchlist_names:
        return

    from bot.chartink_scanner import run_chartink_scan
    symbols = run_chartink_scan()
    matched = [s for s in symbols if s.upper() in watchlist_names]
    not_in_watchlist = [s for s in symbols if s.upper() not in watchlist_names]

    if not_in_watchlist:
        _log_not_in_watchlist_once(not_in_watchlist)

    if not matched:
        return

    # Drop stocks already handled today so we don't re-process / re-log them every
    # cycle: ones holding an open trade (always), and ones already attempted today
    # (unless this is a manual forced run, which intentionally retries).
    db = SessionLocal()
    try:
        open_names = {t.stock_name.upper() for t in db.query(Trade).filter(Trade.status == "open").all()}
        matched = [
            s for s in matched
            if s.upper() not in open_names
            and (force or not _screener_attempted_today(db, s, is_paper))
        ]
    finally:
        db.close()

    if not matched:
        return   # everything already handled today — nothing new, stay quiet

    mode = " (manual)" if force else ""
    _log_simple("INFO", f"Chartink scan{mode}: {len(matched)} new stock(s) to trade: {matched}")

    for sym in matched:
        try:
            run_webhook_trade(sym, force=force)
        except Exception as e:
            _log_simple("ERROR", f"Chartink: trade for {sym} failed - {_exc_detail(e)}")


def _log_simple(level: str, message: str):
    """Write a log row using a fresh session (for use outside a request/db scope)."""
    db = SessionLocal()
    try:
        db.add(Log(level=level, message=message))
        db.commit()
    finally:
        db.close()


def _log_not_in_watchlist_once(stocks):
    """
    Log each screener stock that isn't in the watchlist AT MOST ONCE PER DAY.
    The screener returns the same stocks every 5-minute cycle, so logging them
    each cycle spams the log — we only log a stock the first time it's seen today.
    """
    today = str(get_ist_now().date())
    db = SessionLocal()
    try:
        for s in stocks:
            marker = f"Chartink: {s.upper()} not in watchlist — skipped"
            already = db.query(Log).filter(
                Log.message == marker,
                Log.created_at >= today
            ).count()
            if not already:
                db.add(Log(level="INFO", message=marker))
        db.commit()
    finally:
        db.close()


# ─── Monitor & Close ──────────────────────────────────────────────────────────

def monitor_open_trades():
    """
    Check all open trades every minute.
    Close if profit target, loss limit, or close time is hit.
    """
    db = SessionLocal()
    try:
        settings = _get_settings(db)
        if not settings or not settings.access_token:
            return

        open_trades = db.query(Trade).filter(Trade.status == "open").all()

        for trade in open_trades:
            try:
                _check_and_close_if_needed(db, settings, trade)
            except Exception as e:
                _save_log(db, "ERROR", f"Monitor error for trade {trade.id} ({trade.stock_name}): {_exc_detail(e)}")
    finally:
        db.close()


def _fetch_current_prices(db, settings, trade: Trade):
    """
    Fetch current live prices for all legs by their scrip codes. Returns
    (futures, ce, pe) or None. Works for both real and paper trades, since paper
    trades now also store the real leg scrip codes.
    """
    scrip_list = [
        {"exchange": "N", "exchange_type": "D", "scrip_code": code}
        for code in [trade.futures_scrip_code, trade.ce_scrip_code, trade.pe_scrip_code]
        if code
    ]
    if not scrip_list:
        return None
    quote_result = fivepaisa.get_market_quote(settings.access_token, scrip_list)
    if not quote_result["success"]:
        return None
    quotes = {str(q["ScripCode"]): q["LastRate"] for q in quote_result["quotes"]}
    return (
        quotes.get(str(trade.futures_scrip_code), trade.futures_entry_price),
        quotes.get(str(trade.ce_scrip_code), trade.ce_entry_price),
        quotes.get(str(trade.pe_scrip_code), trade.pe_entry_price)
    )


def _check_and_close_if_needed(db, settings, trade: Trade):
    """Evaluate a single trade and close it if any exit condition is met."""

    now = get_ist_now()

    # Force close at the configured close time, but ONLY on the trade's own expiry
    # date (the real contract expiry stored when the trade opened). The
    # `today_str >= trade.expiry_date` gate is what guarantees this NEVER closes on
    # the same trading day — only on (or after) the expiry date. A trade otherwise
    # stays open until target/stop-loss/manual close. There is no same-day close.
    today_str = now.strftime("%Y-%m-%d")
    close_h, close_m = _parse_close_time(settings.trade_close_time)
    if (trade.expiry_date and today_str >= trade.expiry_date
            and (now.hour, now.minute) >= (close_h, close_m)):
        current_prices = _fetch_current_prices(db, settings, trade)
        _close_trade(db, settings, trade, reason="expiry", current_prices=current_prices)
        return

    # Get current prices for all legs (paper trades are tracked the same way —
    # _close_trade simply won't place real square-off orders for them).
    scrip_list = [
        {"exchange": "N", "exchange_type": "D", "scrip_code": code}
        for code in [trade.futures_scrip_code, trade.ce_scrip_code, trade.pe_scrip_code]
        if code
    ]
    quote_result = fivepaisa.get_market_quote(settings.access_token, scrip_list)
    if not quote_result["success"]:
        _save_log(db, "WARNING", f"Could not fetch prices for trade {trade.id} ({trade.stock_name})")
        return

    quotes = {str(q["ScripCode"]): q["LastRate"] for q in quote_result["quotes"]}

    current_futures = quotes.get(str(trade.futures_scrip_code), trade.futures_entry_price)
    current_ce = quotes.get(str(trade.ce_scrip_code), trade.ce_entry_price)
    current_pe = quotes.get(str(trade.pe_scrip_code), trade.pe_entry_price)

    current_pnl = calculate_trade_pnl(
        trade.futures_entry_price, current_futures,
        trade.ce_entry_price, current_ce,
        trade.pe_entry_price, current_pe,
        lot_size=trade.lot_size or 1
    )

    if current_pnl >= trade.profit_target:
        _save_log(db, "INFO",
            f"[MONITOR {trade.stock_name} #{trade.id}] PROFIT target hit: "
            f"P&L {current_pnl} >= target {trade.profit_target} -> closing")
        _close_trade(db, settings, trade, reason="profit",
                     current_prices=(current_futures, current_ce, current_pe))
    elif current_pnl <= -trade.loss_limit:
        _save_log(db, "INFO",
            f"[MONITOR {trade.stock_name} #{trade.id}] LOSS limit hit: "
            f"P&L {current_pnl} <= -limit {trade.loss_limit} -> closing")
        _close_trade(db, settings, trade, reason="loss",
                     current_prices=(current_futures, current_ce, current_pe))


def _open_scrip_codes(db, settings, stock_name):
    """
    Ground-truth set of scrip codes that still hold a NON-ZERO net position at
    5paisa (V2/NetPositionNetWise) — the same signal position_sync uses. This is how
    we confirm a square-off ACTUALLY happened rather than trusting the broker's
    "order accepted" reply (accepted != filled).

    Returns:
      set(...) -> scrip codes currently open (may be empty = everything flat).
      None     -> the position list could NOT be fetched. Callers must treat None as
                  "unknown" and NOT mark a trade closed on it.
    """
    tag = f"[SQUAREOFF:POSCHECK {stock_name}]"
    try:
        result = fivepaisa.get_positions(settings.access_token, settings.client_code)
    except Exception as e:
        _save_log(db, "ERROR", f"{tag} get_positions CRASHED - {_exc_detail(e)}")
        return None
    if not result.get("success"):
        _save_log(db, "ERROR", f"{tag} get_positions FAILED - {result.get('error')}")
        return None
    open_codes = set()
    for position in result.get("positions", []):
        code = str(position.get("ScripCode", ""))
        net_qty = position.get("NetQty", 0)
        if code and net_qty != 0:
            open_codes.add(code)
    _save_log(db, "INFO", f"{tag} broker open net positions: {sorted(open_codes) or 'none'}")
    return open_codes


def _square_off_legs(db, settings, trade: Trade) -> bool:
    """
    Close a filled position by placing opposite-side marketable-limit orders AND
    confirming each leg is actually flat at 5paisa before reporting success.

    Filled positions can't be "cancelled" — they must be reversed with an opposite
    order (priced as a marketable limit per the broker's no-market-order rule).
    Entry sides are fixed by the strategy:
        Futures = Buy, CE = Sell, PE = Buy
    so the exits are the opposite:
        Futures = Sell, CE = Buy, PE = Sell
    Only legs that were actually opened (scrip code present) are squared off, so this
    also handles the naked-CE trade.

    Returns:
      True  -> every opened leg is VERIFIED flat at the broker (NetQty 0 / absent).
      False -> at least one leg could NOT be confirmed closed (order rejected, not
               filled, price/quote failure, or broker unreachable). The caller MUST
               keep the trade OPEN so the dashboard matches reality and the de-dup
               guard keeps blocking a duplicate real-money trade.

    Idempotent: a leg already flat at the broker is NOT re-squared, so a retry can
    never flip the position to the opposite side. Every step is logged with a
    [SQUAREOFF] tag so a failure can be traced to the exact leg and reason.
    """
    tag = f"[SQUAREOFF {trade.stock_name} #{trade.id}]"
    lot_size = trade.lot_size or 1
    _save_log(db, "INFO",
        f"{tag} STEP 1/5 start. lot_size={lot_size}, legs: "
        f"FUT={trade.futures_scrip_code}, CE={trade.ce_scrip_code}, PE={trade.pe_scrip_code}")

    square_offs = [
        (trade.futures_scrip_code, "S", "FUT"),
        (trade.ce_scrip_code, "B", "CE"),
        (trade.pe_scrip_code, "S", "PE"),
    ]
    legs = [(str(code), side, leg) for code, side, leg in square_offs if code]
    if not legs:
        _save_log(db, "WARNING", f"{tag} no legs with a scrip code - nothing to square off (treating as flat)")
        return True

    # STEP 2 — ground truth BEFORE we touch anything: which legs are still open.
    _save_log(db, "INFO", f"{tag} STEP 2/5 reading broker positions (pre-square-off)")
    open_codes = _open_scrip_codes(db, settings, trade.stock_name)
    if open_codes is None:
        _save_log(db, "ERROR",
            f"{tag} STEP 2/5 FAILED - cannot read positions; aborting square-off, trade stays OPEN")
        return False

    # STEP 3 — place an opposite order for every leg that is STILL open.
    for scrip_code, side, leg in legs:
        leg_tag = f"{tag} STEP 3/5 [{leg} {scrip_code} {side}]"
        if scrip_code not in open_codes:
            _save_log(db, "INFO", f"{leg_tag} already flat at broker - skipping (idempotent)")
            continue
        ltp = _leg_ltp(db, settings, scrip_code, leg)
        price = _marketable_limit_price(ltp, side)
        if price == 0:
            _save_log(db, "ERROR",
                f"{leg_tag} could not derive a valid exit price (ltp={ltp}); a 0 limit would not "
                "fill - skipping placement, will retry next monitor cycle")
            continue
        _save_log(db, "INFO", f"{leg_tag} placing exit order @ {price} (ltp={ltp})")
        try:
            result = fivepaisa.place_order(
                settings.access_token, "N", "D", scrip_code, side, price, lot_size, False,
                generate_remote_order_id(f"{trade.stock_name}_{leg}_EXIT")
            )
        except Exception as e:
            _save_log(db, "ERROR", f"{leg_tag} place_order CRASHED - {_exc_detail(e)}")
            continue
        if not result.get("success"):
            _save_log(db, "ERROR", f"{leg_tag} exit order REJECTED by broker - {result.get('error')}")
        else:
            _save_log(db, "INFO",
                f"{leg_tag} exit order accepted (broker_order_id={result.get('broker_order_id')}) "
                "- accepted is NOT a fill, confirming via positions next")

    # STEP 4 — let the exchange settle, then re-read positions: the AUTHORITATIVE
    # check. An accepted order is not a guaranteed fill, so we only trust positions.
    _save_log(db, "INFO",
        f"{tag} STEP 4/5 waiting {SQUAREOFF_SETTLE_SECONDS}s then re-reading positions (post-square-off)")
    time.sleep(SQUAREOFF_SETTLE_SECONDS)
    open_codes = _open_scrip_codes(db, settings, trade.stock_name)
    if open_codes is None:
        _save_log(db, "ERROR",
            f"{tag} STEP 4/5 FAILED - cannot re-read positions to confirm; trade stays OPEN")
        return False

    # STEP 5 — verdict: every leg must be flat at the broker.
    still_open = [leg for scrip_code, _side, leg in legs if scrip_code in open_codes]
    if still_open:
        _save_log(db, "ERROR",
            f"{tag} STEP 5/5 square-off NOT confirmed - still OPEN at broker: {', '.join(still_open)}. "
            "Trade kept OPEN, will retry next monitor cycle.")
        return False
    _save_log(db, "INFO", f"{tag} STEP 5/5 SUCCESS - all legs confirmed flat at broker.")
    return True


def _close_trade(db, settings, trade: Trade, reason: str, current_prices=None):
    """
    Square off all open legs on 5paisa and mark the trade closed ONLY when the
    broker position is verified flat. A real trade whose square-off cannot be
    confirmed is kept OPEN (and the client alerted) so the bot never reports a
    position closed while it is still live at the broker — which previously let a
    duplicate real-money trade slip past the de-dup guard.
    """
    tag = f"[CLOSE {trade.stock_name} #{trade.id}]"

    if not trade.is_paper_trade:
        # ── Retry gating ──────────────────────────────────────────────────────
        # Don't hammer a failing square-off every ~10s. Retry at most
        # SQUAREOFF_MAX_ATTEMPTS times, spaced SQUAREOFF_RETRY_SECONDS apart. We are
        # only called while a target is still hit (the monitor re-checks live P&L
        # each cycle), so if the price drifts back to neutral we simply stop being
        # called and the trade behaves like a normal open trade.
        now = get_ist_now()
        attempts = trade.squareoff_attempts or 0

        if attempts >= SQUAREOFF_MAX_ATTEMPTS:
            _save_log(db, "WARNING",
                f"{tag} square-off already attempted {attempts}x (max {SQUAREOFF_MAX_ATTEMPTS}) - "
                "not retrying automatically; awaiting manual close / 5-min position sync")
            return

        if attempts >= 1 and trade.last_squareoff_attempt_at:
            # to_naive() because last_squareoff_attempt_at loaded from SQLite is
            # tz-naive while get_ist_now() is tz-aware (can't subtract directly).
            elapsed = (to_naive(now) - to_naive(trade.last_squareoff_attempt_at)).total_seconds()
            if elapsed < SQUAREOFF_RETRY_SECONDS:
                _save_log(db, "INFO",
                    f"{tag} square-off cooldown: {int(elapsed)}s since last attempt "
                    f"(retry after {SQUAREOFF_RETRY_SECONDS}s) - skipping this cycle")
                return

        # Record this attempt before placing anything.
        trade.squareoff_attempts = attempts + 1
        trade.last_squareoff_attempt_at = now
        db.commit()
        _save_log(db, "INFO",
            f"{tag} exit triggered. reason={reason}, square-off attempt "
            f"{trade.squareoff_attempts}/{SQUAREOFF_MAX_ATTEMPTS}")

        squared_off = _square_off_legs(db, settings, trade)
        if not squared_off:
            _save_log(db, "ERROR",
                f"{tag} square-off attempt {trade.squareoff_attempts}/{SQUAREOFF_MAX_ATTEMPTS} "
                "NOT confirmed - trade kept OPEN.")
            # Alert the client ONCE — on the first failure only.
            if not trade.squareoff_alerted:
                trade.squareoff_alerted = True
                db.commit()
                try:
                    from notifications.email import send_squareoff_failed_email
                    send_squareoff_failed_email(trade.stock_name, reason)
                    _save_log(db, "INFO", f"{tag} square-off-failed alert emailed to client (first failure)")
                except Exception as e:
                    _save_log(db, "WARNING", f"{tag} square-off-failed alert email failed - {_exc_detail(e)}")
            db.commit()
            return
    else:
        _save_log(db, "INFO", f"{tag} exit triggered. reason={reason}, paper=True")

    if current_prices:
        trade.futures_exit_price, trade.ce_exit_price, trade.pe_exit_price = current_prices
        trade.pnl = calculate_trade_pnl(
            trade.futures_entry_price, trade.futures_exit_price,
            trade.ce_entry_price, trade.ce_exit_price,
            trade.pe_entry_price, trade.pe_exit_price,
            lot_size=trade.lot_size or 1
        )

    trade.status = "closed"
    trade.close_reason = reason
    trade.closed_at = get_ist_now()

    _save_log(db, "INFO", f"{tag} CONFIRMED CLOSED. reason={reason}, P&L={trade.pnl}")
    db.commit()

    try:
        from notifications.email import send_trade_closed_email
        send_trade_closed_email(trade.stock_name, reason, trade.pnl)
    except Exception as e:
        _save_log(db, "WARNING", f"{tag} email failed for trade close - {_exc_detail(e)}")


# ─── Safety Check ─────────────────────────────────────────────────────────────

def safety_check(trigger: str = "3:40 PM"):
    """
    Email a summary of the still-open trades with their current P&L. Used by both
    the scheduled 3:40 PM job and the manual 'Send Report Now' button. Does NOT
    stop trading and does NOT close anything — trades are held until target /
    stop-loss or their expiry date.

    Returns the number of open trades reported (0 = none open, no email sent).
    Re-raises on email failure so a manual caller can surface the error.
    """
    db = SessionLocal()
    try:
        open_trades = db.query(Trade).filter(Trade.status == "open").all()
        if not open_trades:
            return 0

        settings = _get_settings(db)
        summary = []
        for t in open_trades:
            pnl = None
            if settings and settings.access_token:
                prices = _fetch_current_prices(db, settings, t)
                if prices:
                    cf, cce, cpe = prices
                    pnl = calculate_trade_pnl(
                        t.futures_entry_price, cf,
                        t.ce_entry_price, cce,
                        t.pe_entry_price, cpe,
                        lot_size=t.lot_size or 1
                    )
            summary.append({
                "stock_name": t.stock_name,
                "is_paper": t.is_paper_trade,
                "pnl": pnl,
                "target": t.profit_target,
                "loss": t.loss_limit,
            })

        try:
            from notifications.email import send_safety_alert_email
            send_safety_alert_email(summary)
        except Exception as e:
            _save_log(db, "ERROR", f"Open-trades summary email failed: {str(e)}")
            raise

        _save_log(db, "INFO", f"Open-trades summary ({trigger}): {len(open_trades)} open trade(s) emailed")
        return len(open_trades)
    finally:
        db.close()
