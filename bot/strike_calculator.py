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
    - strike_type 'percent' -> futures_price + strike_value%, rounded up to nearest strike
    """
    if strike_type == "fixed":
        return float(strike_value)

    interval = get_strike_interval(stock_name)
    raw_strike = futures_price * (1 + strike_value / 100)
    return float(round_strike_up(raw_strike, interval))


def find_pe_strike(option_chain, ce_sell_premium):
    """
    Find the best PE strike to buy.
    Rule: PE buy premium must be lower than CE sell premium.
    Among all valid PE options, pick the one with the highest premium
    (closest to CE premium but still below it).

    option_chain: list of dicts -> [{"strike": 1090, "premium": 11.1, "type": "PE"}, ...]
    Returns: (strike, premium) or (None, None) if no valid PE found
    """
    valid_pe_options = [
        opt for opt in option_chain
        if opt["type"] == "PE" and opt["premium"] < ce_sell_premium
    ]

    if not valid_pe_options:
        return None, None

    best_pe = max(valid_pe_options, key=lambda x: x["premium"])
    return best_pe["strike"], best_pe["premium"]


def validate_premium_condition(ce_sell_premium, pe_buy_premium):
    """PE buy premium must be lower than CE sell premium."""
    return ce_sell_premium > pe_buy_premium
