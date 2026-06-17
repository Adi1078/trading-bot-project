STRIKE_INTERVALS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "SENSEX": 100,
    "MIDCPNIFTY": 25,
}
DEFAULT_STRIKE_INTERVAL = 50


def get_strike_interval(stock_name):
    return STRIKE_INTERVALS.get(stock_name.upper(), DEFAULT_STRIKE_INTERVAL)


def round_strike_up(price, interval):
    return (int(price / interval) + 1) * interval


def calculate_ce_strike(futures_price, strike_type, strike_value, stock_name):
    """
    Calculate the CE strike to sell.
    - strike_type 'fixed'   -> use strike_value directly as the strike price
    - strike_type 'percent' -> the raw futures_price + strike_value% target.

    For 'percent' we deliberately return the *raw* target and do NOT round it to a
    guessed strike interval. get_option_chain() snaps this to the nearest real
    exchange strike at or above the target, so it always lands on a strike that
    actually exists and trades — regardless of the stock's price/strike spacing.
    The old hardcoded-interval rounding broke low-priced stocks (e.g. PNB ~₹109,
    +2% should be ~₹112.5 but rounded to ₹150, a dead far-OTM strike with no
    premium). stock_name is kept in the signature for callers/back-compat.
    """
    if strike_type == "fixed":
        return float(strike_value)

    return float(futures_price * (1 + strike_value / 100))


def _is_liquid(opt):
    """
    A PE candidate is only worth considering if it has actually traded — a premium
    above 0 and (when the volume is known) non-zero day volume. This drops the
    dead/illiquid far-OTM strikes that 5paisa rejects with "Trading not allowed in
    illiquid contract". Volume is optional so callers/tests that don't supply it
    still work (they're treated as liquid).
    """
    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    if _n(opt.get("premium")) <= 0:
        return False
    if "volume" in opt and _n(opt.get("volume")) <= 0:
        return False
    return True


def find_pe_candidates(option_chain, ce_sell_premium):
    """
    All tradeable PE strikes whose premium is below the CE sell premium, ranked from
    highest premium (closest to CE) down. The caller can walk this list and confirm
    real order-book liquidity (Market Depth) on each before placing, falling through
    to the next-best one if a strike has no live offers.

    option_chain: [{"strike", "premium", "type", "volume"(optional)}, ...]
    Returns: list of dicts sorted by premium descending (may be empty).
    """
    valid = [
        opt for opt in option_chain
        if opt["type"] == "PE" and opt["premium"] < ce_sell_premium and _is_liquid(opt)
    ]
    return sorted(valid, key=lambda x: x["premium"], reverse=True)


def find_pe_strike(option_chain, ce_sell_premium):
    """
    Find the best (highest-premium, still-below-CE) tradeable PE strike to buy.

    option_chain: list of dicts -> [{"strike": 1090, "premium": 11.1, "type": "PE"}, ...]
    Returns: (strike, premium) or (None, None) if no valid PE found
    """
    candidates = find_pe_candidates(option_chain, ce_sell_premium)
    if not candidates:
        return None, None
    best_pe = candidates[0]
    return best_pe["strike"], best_pe["premium"]


def validate_premium_condition(ce_sell_premium, pe_buy_premium):
    """PE buy premium must be lower than CE sell premium."""
    return ce_sell_premium > pe_buy_premium
