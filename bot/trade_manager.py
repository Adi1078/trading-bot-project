import logging
from database import SessionLocal
from models.trade import Trade
from models.fixed_trades import FixedTrade
from models.watchlist import Watchlist
from models.settings import Settings
from models.log import Log
from broker import fivepaisa
from bot.strike_calculator import calculate_ce_strike, find_pe_strike, validate_premium_condition
from utils.exchange_calendar import get_current_expiry, get_next_expiry, is_last_trading_day
from utils.helpers import generate_remote_order_id, calculate_trade_pnl, get_ist_now

logger = logging.getLogger(__name__)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _save_log(db, level: str, message: str):
    log = Log(level=level, message=message)
    db.add(log)
    db.commit()
    logger.info(f"[{level}] {message}")


def _get_settings(db):
    return db.query(Settings).first()


def _is_in_watchlist(db, stock_name: str) -> bool:
    return db.query(Watchlist).filter(Watchlist.stock_name.ilike(stock_name)).first() is not None


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


def _unwind_placed_legs(db, settings, stock_name, placed_legs, lot_size):
    """
    Square off legs that were already placed when a later leg of a collar fails.
    A half-built collar is an unhedged position with open risk, so if we can't
    complete all three legs we reverse whatever we did place.
    placed_legs: list of (scrip_code, entry_side, leg_name); we send the opposite side.
    """
    opposite = {"B": "S", "S": "B"}
    for scrip_code, entry_side, leg in placed_legs:
        result = fivepaisa.place_order(
            settings.access_token, "N", "D", scrip_code, opposite[entry_side], 0, lot_size, False,
            generate_remote_order_id(f"{stock_name}_{leg}_UNWIND")
        )
        if not result["success"]:
            _save_log(db, "ERROR", f"{stock_name}: CRITICAL — could not unwind {leg} leg ({scrip_code}) after partial collar - {result['error']}. Check position manually!")
        else:
            _save_log(db, "WARNING", f"{stock_name}: unwound {leg} leg ({scrip_code}) after partial collar failure")


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
                _save_log(db, "ERROR", f"Unexpected error for {ft.stock_name}: {str(e)}")
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

    # Step 5: Find best PE strike (premium must be lower than CE premium)
    pe_options = [
        {"strike": o["StrikeRate"], "premium": o["LastRate"], "type": "PE"}
        for o in option_chain if o.get("CPType") == "PE"
    ]
    pe_strike, pe_premium = find_pe_strike(pe_options, ce_premium)

    if pe_strike is None:
        _save_log(db, "ERROR", f"{ft.stock_name}: no valid PE strike found below CE premium {ce_premium}")
        return

    # Step 6: Validate CE premium > PE premium
    if not validate_premium_condition(ce_premium, pe_premium):
        _save_log(db, "ERROR", f"{ft.stock_name}: premium condition failed - CE {ce_premium} <= PE {pe_premium}")
        return

    # Step 7: Get PE scrip code from option chain
    pe_option = next(
        (o for o in option_chain if o.get("StrikeRate") == pe_strike and o.get("CPType") == "PE"),
        None
    )
    pe_scrip_code = str(pe_option.get("Scripcode", "")) if pe_option else ""

    if not pe_scrip_code:
        _save_log(db, "ERROR", f"{ft.stock_name}: PE option has no scrip code in chain response")
        return

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

    # Place all 3 orders using real numeric scrip codes
    futures_result = fivepaisa.place_order(
        settings.access_token, "N", "D", futures_scrip_code, "B", 0, lot_size, False,
        generate_remote_order_id(ft.stock_name + "_FUT")
    )
    if not futures_result["success"]:
        _save_log(db, "ERROR", f"{ft.stock_name}: futures order failed - {futures_result['error']}")
        return
    placed_legs = [(futures_scrip_code, "B", "FUT")]

    ce_result = fivepaisa.place_order(
        settings.access_token, "N", "D", ce_scrip_code, "S", 0, lot_size, False,
        generate_remote_order_id(ft.stock_name + "_CE")
    )
    if not ce_result["success"]:
        _save_log(db, "ERROR", f"{ft.stock_name}: CE sell order failed - {ce_result['error']}")
        _unwind_placed_legs(db, settings, ft.stock_name, placed_legs, lot_size)
        return
    placed_legs.append((ce_scrip_code, "S", "CE"))

    pe_result = fivepaisa.place_order(
        settings.access_token, "N", "D", pe_scrip_code, "B", 0, lot_size, False,
        generate_remote_order_id(ft.stock_name + "_PE")
    )
    if not pe_result["success"]:
        _save_log(db, "ERROR", f"{ft.stock_name}: PE buy order failed - {pe_result['error']}")
        _unwind_placed_legs(db, settings, ft.stock_name, placed_legs, lot_size)
        return

    # Step 9: Save trade to database
    trade = Trade(
        stock_name=ft.stock_name,
        trade_source="fixed",
        fixed_trade_id=ft.id,
        is_paper_trade=False,
        month_type=ft.month_type,
        lot_size=lot_size,
        futures_scrip_code=futures_scrip_code,
        ce_scrip_code=ce_scrip_code,
        pe_scrip_code=pe_scrip_code,
        futures_broker_order_id=str(futures_result["broker_order_id"]),
        ce_broker_order_id=str(ce_result["broker_order_id"]),
        pe_broker_order_id=str(pe_result["broker_order_id"]),
        futures_entry_price=futures_price,
        ce_entry_price=ce_premium,
        pe_entry_price=pe_premium,
        profit_target=ft.profit_target,
        loss_limit=ft.loss_limit,
        status="open"
    )
    db.add(trade)
    _save_log(db, "INFO", f"{ft.stock_name}: collar trade placed - Futures@{futures_price}, CE {ce_strike}@{ce_premium} sold, PE {pe_strike}@{pe_premium} bought")
    db.commit()

    try:
        from notifications.email import send_trade_opened_email
        send_trade_opened_email(ft.stock_name, futures_price, ce_strike, ce_premium, pe_strike, pe_premium)
    except Exception as e:
        _save_log(db, "WARNING", f"{ft.stock_name}: email notification failed - {str(e)}")


def _place_option_trade(db, settings, ft: FixedTrade):
    """Sell CE at fixed strike only. No futures, no PE. Used for month_type = option."""

    ce_strike = ft.strike_value
    expiry = get_current_expiry()

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
    ce_result = fivepaisa.place_order(
        settings.access_token, "N", "D", ce_scrip_code, "S", 0, lot_size, False,
        generate_remote_order_id(ft.stock_name + "_CE")
    )
    if not ce_result["success"]:
        _save_log(db, "ERROR", f"{ft.stock_name}: option CE sell failed - {ce_result['error']}")
        return

    trade = Trade(
        stock_name=ft.stock_name,
        trade_source="fixed",
        fixed_trade_id=ft.id,
        is_paper_trade=False,
        month_type="option",
        lot_size=lot_size,
        ce_scrip_code=ce_scrip_code,
        ce_broker_order_id=str(ce_result["broker_order_id"]),
        ce_entry_price=ce_option.get("LastRate", ce_strike),
        profit_target=ft.profit_target,
        loss_limit=ft.loss_limit,
        status="open"
    )
    db.add(trade)
    _save_log(db, "INFO", f"{ft.stock_name}: option CE sell placed at strike {ce_strike}, scrip {ce_scrip_code}")
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
            {"strike": o["StrikeRate"], "premium": o["LastRate"], "type": "PE"}
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

def run_webhook_trade(stock_name: str):
    """
    Place a trade triggered by a Chartink webhook signal.
    Uses the global webhook configuration from Settings — same config applies to ALL signals.
    """
    db = SessionLocal()
    try:
        settings = _get_settings(db)

        if not settings or not settings.is_trading or not settings.access_token:
            _save_log(db, "ERROR", f"Webhook trade for {stock_name} skipped: trading stopped or broker disconnected")
            return

        existing = db.query(Trade).filter(
            Trade.stock_name.ilike(stock_name),
            Trade.status == "open"
        ).first()
        if existing:
            _save_log(db, "INFO", f"Screener trade for {stock_name} skipped: open trade already exists")
            return

        # Once per day per stock — if we already opened a trade for this stock today
        # (even if it has since closed on target/SL), do not enter it again today.
        today = get_ist_now().date()
        recent = (db.query(Trade)
                  .filter(Trade.stock_name.ilike(stock_name))
                  .order_by(Trade.id.desc())
                  .limit(10).all())
        if any(t.placed_at and t.placed_at.date() == today and not t.is_paper_trade for t in recent):
            _save_log(db, "INFO", f"Screener trade for {stock_name} skipped: already traded today (once per day)")
            return

        watchlist_stock = db.query(Watchlist).filter(Watchlist.stock_name.ilike(stock_name)).first()
        if not watchlist_stock:
            _save_log(db, "INFO", f"Webhook trade for {stock_name} skipped: not in watchlist")
            return

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

        # ── Option trade: naked CE sell only ────────────────────────────────
        if trade_type == "option":
            ce_result = fivepaisa.place_order(
                settings.access_token, "N", "D", ce_scrip_code, "S", 0, lot_size, False,
                generate_remote_order_id(stock_name + "_CE")
            )
            if not ce_result["success"]:
                _save_log(db, "ERROR", f"Webhook {stock_name}: CE sell failed - {ce_result['error']}")
                return

            trade = Trade(
                stock_name=stock_name.upper(),
                trade_source="webhook",
                is_paper_trade=False,
                month_type=month_type,
                lot_size=lot_size,
                ce_scrip_code=ce_scrip_code,
                ce_broker_order_id=str(ce_result["broker_order_id"]),
                ce_entry_price=ce_premium,
                profit_target=profit_target,
                loss_limit=loss_limit,
                status="open"
            )
            db.add(trade)
            _save_log(db, "INFO", f"Webhook {stock_name}: naked CE sell placed — strike {ce_strike}, premium {ce_premium}, lot {lot_size}")
            db.commit()
            return

        # ── Collar trade: Buy Futures + Sell CE + Buy PE ────────────────────
        pe_options = [
            {"strike": o["StrikeRate"], "premium": o["LastRate"], "type": "PE"}
            for o in option_chain if o.get("CPType") == "PE"
        ]
        pe_strike, pe_premium = find_pe_strike(pe_options, ce_premium)

        if pe_strike is None or not validate_premium_condition(ce_premium, pe_premium):
            _save_log(db, "ERROR", f"Webhook {stock_name}: no valid PE strike found (CE premium {ce_premium})")
            return

        pe_option = next(
            (o for o in option_chain if o.get("StrikeRate") == pe_strike and o.get("CPType") == "PE"),
            None
        )
        pe_scrip_code = str(pe_option.get("Scripcode", "")) if pe_option else ""

        if not pe_scrip_code:
            _save_log(db, "ERROR", f"Webhook {stock_name}: PE option has no scrip code")
            return

        futures_scrip_code = fivepaisa.get_futures_scrip_code(stock_name, expiry)
        if not futures_scrip_code:
            _save_log(db, "ERROR", f"Webhook {stock_name}: futures contract not found in scrip master for expiry {expiry}")
            return

        # Record the actual futures contract price for entry (matches what the monitor reads)
        fut_price = _fetch_futures_price(settings, futures_scrip_code)
        if fut_price is not None:
            futures_price = fut_price

        futures_result = fivepaisa.place_order(
            settings.access_token, "N", "D", futures_scrip_code, "B", 0, lot_size, False,
            generate_remote_order_id(stock_name + "_FUT")
        )
        ce_result = fivepaisa.place_order(
            settings.access_token, "N", "D", ce_scrip_code, "S", 0, lot_size, False,
            generate_remote_order_id(stock_name + "_CE")
        )
        pe_result = fivepaisa.place_order(
            settings.access_token, "N", "D", pe_scrip_code, "B", 0, lot_size, False,
            generate_remote_order_id(stock_name + "_PE")
        )

        if not all([futures_result["success"], ce_result["success"], pe_result["success"]]):
            errors = []
            placed_legs = []
            if futures_result["success"]:
                placed_legs.append((futures_scrip_code, "B", "FUT"))
            else:
                errors.append(f"Futures: {futures_result['error']}")
            if ce_result["success"]:
                placed_legs.append((ce_scrip_code, "S", "CE"))
            else:
                errors.append(f"CE: {ce_result['error']}")
            if pe_result["success"]:
                placed_legs.append((pe_scrip_code, "B", "PE"))
            else:
                errors.append(f"PE: {pe_result['error']}")
            _save_log(db, "ERROR", f"Webhook {stock_name}: order(s) failed — {'; '.join(errors)}")
            if placed_legs:
                _unwind_placed_legs(db, settings, stock_name, placed_legs, lot_size)
            return

        trade = Trade(
            stock_name=stock_name.upper(),
            trade_source="webhook",
            is_paper_trade=False,
            month_type=month_type,
            lot_size=lot_size,
            futures_scrip_code=futures_scrip_code,
            ce_scrip_code=ce_scrip_code,
            pe_scrip_code=pe_scrip_code,
            futures_broker_order_id=str(futures_result["broker_order_id"]),
            ce_broker_order_id=str(ce_result["broker_order_id"]),
            pe_broker_order_id=str(pe_result["broker_order_id"]),
            futures_entry_price=futures_price,
            ce_entry_price=ce_premium,
            pe_entry_price=pe_premium,
            profit_target=profit_target,
            loss_limit=loss_limit,
            status="open"
        )
        db.add(trade)
        _save_log(db, "INFO",
            f"Webhook {stock_name}: collar placed — Futures@{futures_price}, "
            f"CE {ce_strike}@{ce_premium} sold, PE {pe_strike}@{pe_premium} bought, lot {lot_size}")
        db.commit()

    finally:
        db.close()


def run_chartink_cycle():
    """
    One Chartink polling cycle (called by the scheduler every few minutes):
    run the screeners, keep only symbols that are in the watchlist, and fire a
    configured trade for each. run_webhook_trade() applies its own guards
    (watchlist, open-trade dedup, once-per-day), so this is safe to call repeatedly.
    """
    db = SessionLocal()
    try:
        settings = _get_settings(db)
        if not settings or not settings.is_trading or not settings.access_token:
            return
        watchlist_names = {w.stock_name.upper() for w in db.query(Watchlist).all()}
    finally:
        db.close()

    if not watchlist_names:
        return

    from bot.chartink_scanner import run_chartink_scan
    symbols = run_chartink_scan()
    matched = [s for s in symbols if s.upper() in watchlist_names]

    if not matched:
        return

    _log_simple("INFO", f"Chartink scan: {len(symbols)} stocks found, {len(matched)} in watchlist: {matched}")

    for sym in matched:
        try:
            run_webhook_trade(sym)
        except Exception as e:
            _log_simple("ERROR", f"Chartink: trade for {sym} failed - {str(e)}")


def _log_simple(level: str, message: str):
    """Write a log row using a fresh session (for use outside a request/db scope)."""
    db = SessionLocal()
    try:
        db.add(Log(level=level, message=message))
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
                _save_log(db, "ERROR", f"Monitor error for trade {trade.id} ({trade.stock_name}): {str(e)}")
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

    # Force close on expiry day at 12:00 PM
    if is_last_trading_day() and now.hour >= 12:
        current_prices = _fetch_current_prices(db, settings, trade)
        _close_trade(db, settings, trade, reason="expiry", current_prices=current_prices)
        return

    # Force close at configured close time
    close_hour, close_minute = map(int, (settings.trade_close_time or "12:00").split(":"))
    if now.hour > close_hour or (now.hour == close_hour and now.minute >= close_minute):
        current_prices = _fetch_current_prices(db, settings, trade)
        _close_trade(db, settings, trade, reason="time", current_prices=current_prices)
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
        _close_trade(db, settings, trade, reason="profit",
                     current_prices=(current_futures, current_ce, current_pe))
    elif current_pnl <= -trade.loss_limit:
        _close_trade(db, settings, trade, reason="loss",
                     current_prices=(current_futures, current_ce, current_pe))


def _square_off_legs(db, settings, trade: Trade):
    """
    Close a filled position by placing opposite-side MARKET orders (square off).

    The collar legs are market orders that fill instantly, so they cannot be
    "cancelled" — they must be reversed. Entry sides are fixed by the strategy:
        Futures = Buy, CE = Sell, PE = Buy
    so the exits are the opposite:
        Futures = Sell, CE = Buy, PE = Sell
    Same scrip code, same quantity, same product type (delivery, IsIntraday=False)
    so the broker nets each leg to zero. Only legs that were actually opened
    (scrip code present) are squared off, so this also handles the naked-CE trade.
    """
    lot_size = trade.lot_size or 1
    square_offs = [
        (trade.futures_scrip_code, "S", "FUT"),
        (trade.ce_scrip_code, "B", "CE"),
        (trade.pe_scrip_code, "S", "PE"),
    ]
    for scrip_code, side, leg in square_offs:
        if not scrip_code:
            continue
        result = fivepaisa.place_order(
            settings.access_token, "N", "D", scrip_code, side, 0, lot_size, False,
            generate_remote_order_id(f"{trade.stock_name}_{leg}_EXIT")
        )
        if not result["success"]:
            _save_log(db, "ERROR", f"{trade.stock_name}: failed to square off {leg} leg ({scrip_code}) - {result['error']}")
        else:
            _save_log(db, "INFO", f"{trade.stock_name}: squared off {leg} leg ({scrip_code}) with {side}")


def _close_trade(db, settings, trade: Trade, reason: str, current_prices=None):
    """Square off all open legs on 5paisa and mark trade as closed in database."""

    if not trade.is_paper_trade:
        _square_off_legs(db, settings, trade)

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

    _save_log(db, "INFO", f"Trade {trade.id} ({trade.stock_name}) closed. Reason: {reason}. P&L: {trade.pnl}")
    db.commit()

    try:
        from notifications.email import send_trade_closed_email
        send_trade_closed_email(trade.stock_name, reason, trade.pnl)
    except Exception as e:
        _save_log(db, "WARNING", f"Email failed for trade close {trade.stock_name}: {str(e)}")


# ─── Safety Check ─────────────────────────────────────────────────────────────

def safety_check():
    """
    Run at 3:40 PM. If any trades are still open, send alert email and stop trading.
    """
    db = SessionLocal()
    try:
        open_trades = db.query(Trade).filter(Trade.status == "open").all()
        if not open_trades:
            return

        settings = _get_settings(db)
        stock_names = [t.stock_name for t in open_trades]

        _save_log(db, "WARNING", f"Safety check: {len(open_trades)} trades still open at 3:40 PM: {stock_names}")

        if settings:
            settings.is_trading = False
            db.commit()

        try:
            from notifications.email import send_safety_alert_email
            send_safety_alert_email(stock_names)
        except Exception as e:
            _save_log(db, "ERROR", f"Safety alert email failed: {str(e)}")
    finally:
        db.close()
