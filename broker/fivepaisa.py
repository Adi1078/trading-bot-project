import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://Openapi.5paisa.com/VendorsAPI/Service1.svc"


def _creds():
    """Read broker credentials from the database (set via Admin → Settings)."""
    try:
        from database import SessionLocal
        from models.settings import Settings
        db = SessionLocal()
        try:
            s = db.query(Settings).first()
            if s:
                return (
                    s.app_key or "",
                    s.encry_key or "",
                    s.user_id or "",
                    s.algo_id or "0",
                    s.client_code or ""
                )
        finally:
            db.close()
    except Exception:
        pass
    return "", "", "", "0", ""


def _get_headers(access_token=None):
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"bearer {access_token}"
    return headers


def _head_ok(data):
    """5paisa uses head.Status or head.status (case varies), integer 0 or string '0'."""
    head = data.get("head", {})
    status = head.get("Status", head.get("status", -1))
    return str(status) == "0"


def _head_error(data):
    head = data.get("head", {})
    return head.get("StatusDescription") or head.get("statusDescription") or "Unknown error"


def get_access_token(request_token):
    """Exchange request token (from OAuth login) for access token."""
    app_key, encry_key, user_id, _, _ = _creds()
    url = f"{BASE_URL}/GetAccessToken"
    payload = {
        "head": {"key": app_key},
        "body": {
            "RequestToken": request_token,
            "EncryKey": encry_key,
            "UserId": user_id
        }
    }
    try:
        response = requests.post(url, json=payload, headers=_get_headers(), timeout=10)
        response.raise_for_status()
        data = response.json()

        body = data.get("body", {})
        # 5paisa uses a two-level status: head.Status=0 means the HTTP call arrived,
        # body.Status=0 means authentication actually succeeded.
        if body.get("Status") == 0:
            return {
                "success": True,
                "access_token": body.get("AccessToken", ""),
                "client_code": body.get("ClientCode", ""),
                "client_name": body.get("ClientName", "")
            }

        error_msg = body.get("Message") or data.get("head", {}).get("StatusDescription", "Unknown error")
        logger.error(f"get_access_token failed: {error_msg}")
        return {"success": False, "error": error_msg}

    except Exception as e:
        logger.error(f"get_access_token exception: {str(e)}")
        return {"success": False, "error": str(e)}


def place_order(access_token, exchange, exchange_type, scrip_code, order_type,
                price, qty, is_intraday, remote_order_id, stop_loss_price=0):
    """
    Place a buy or sell order on 5paisa.
    order_type: "B" for buy, "S" for sell
    price: 0 for market order, actual price for limit order
    is_intraday: False for NRML (holding till expiry), True for MIS
    """
    app_key, _, _, algo_id, _ = _creds()
    url = f"{BASE_URL}/V1/PlaceOrderRequest"
    payload = {
        "head": {"key": app_key},
        "body": {
            "Exchange": exchange,
            "ExchangeType": exchange_type,
            "ScripCode": scrip_code,
            "OrderType": order_type,
            "Price": price,
            "Qty": qty,
            "StopLossPrice": stop_loss_price,
            "IsIntraday": is_intraday,
            "RemoteOrderID": remote_order_id,
            "DisQty": 0,
            "AHPlaced": "N",
            "AlgoID": algo_id or 0
        }
    }
    try:
        response = requests.post(url, json=payload, headers=_get_headers(access_token), timeout=10)
        response.raise_for_status()
        data = response.json()

        if data["body"]["Status"] == 0:
            return {
                "success": True,
                "broker_order_id": data["body"]["BrokerOrderID"],
                "message": data["body"]["Message"]
            }

        logger.error(f"place_order failed for scrip {scrip_code}: {data['body']['Message']}")
        return {"success": False, "error": data["body"]["Message"]}

    except Exception as e:
        logger.error(f"place_order exception for scrip {scrip_code}: {str(e)}")
        return {"success": False, "error": str(e)}


def cancel_order(access_token, broker_order_id, scrip_code, exchange, exchange_type):
    """Cancel an open order on 5paisa."""
    app_key, _, _, _, _ = _creds()
    url = f"{BASE_URL}/V1/CancelOrderRequest"
    payload = {
        "head": {"key": app_key},
        "body": {
            "BrokerOrderID": broker_order_id,
            "ScripCode": scrip_code,
            "Exchange": exchange,
            "ExchangeType": exchange_type
        }
    }
    try:
        response = requests.post(url, json=payload, headers=_get_headers(access_token), timeout=10)
        response.raise_for_status()
        data = response.json()

        if _head_ok(data):
            return {"success": True}

        logger.error(f"cancel_order failed for order {broker_order_id}: {_head_error(data)}")
        return {"success": False, "error": _head_error(data)}

    except Exception as e:
        logger.error(f"cancel_order exception for order {broker_order_id}: {str(e)}")
        return {"success": False, "error": str(e)}


def get_positions(access_token, client_code):
    """Fetch all open positions from 5paisa."""
    app_key, _, _, _, _ = _creds()
    url = f"{BASE_URL}/V2/NetPositionNetWise"
    payload = {
        "head": {"key": app_key},
        "body": {"ClientCode": client_code}
    }
    try:
        response = requests.post(url, json=payload, headers=_get_headers(access_token), timeout=10)
        response.raise_for_status()
        data = response.json()

        if _head_ok(data):
            return {"success": True, "positions": data["body"]["NetPositionDetail"]}

        logger.error(f"get_positions failed: {_head_error(data)}")
        return {"success": False, "error": _head_error(data)}

    except Exception as e:
        logger.error(f"get_positions exception: {str(e)}")
        return {"success": False, "error": str(e)}


def get_order_status(access_token, remote_order_id):
    """Check the status of a placed order using our RemoteOrderID."""
    app_key, _, _, _, _ = _creds()
    url = f"{BASE_URL}/V1/OrderStatus"
    payload = {
        "head": {"key": app_key},
        "body": {"RemoteOrderID": remote_order_id}
    }
    try:
        response = requests.post(url, json=payload, headers=_get_headers(access_token), timeout=10)
        response.raise_for_status()
        data = response.json()

        if _head_ok(data):
            return {"success": True, "status": data["body"]}

        logger.error(f"get_order_status failed for {remote_order_id}: {_head_error(data)}")
        return {"success": False, "error": _head_error(data)}

    except Exception as e:
        logger.error(f"get_order_status exception for {remote_order_id}: {str(e)}")
        return {"success": False, "error": str(e)}


def get_market_quote(access_token, scrip_list):
    """
    Get live price quotes for a list of scrips.
    scrip_list: [{"exchange": "N", "exchange_type": "D", "scrip_code": "1660"}, ...]
    """
    app_key, _, _, _, client_code = _creds()
    url = f"{BASE_URL}/MarketSnapshot"
    data_items = [
        {
            "Exchange": s["exchange"],
            "ExchangeType": s["exchange_type"],
            "ScripCode": str(s["scrip_code"]),
            "ScripData": ""
        }
        for s in scrip_list
    ]
    payload = {
        "head": {"key": app_key},
        "body": {
            "ClientCode": client_code,
            "Data": data_items
        }
    }

    print(f"[DEBUG] MarketSnapshot request payload: {payload}", flush=True)

    try:
        response = requests.post(url, json=payload, headers=_get_headers(access_token), timeout=10)
        response.raise_for_status()
        data = response.json()

        print(f"[DEBUG] MarketSnapshot raw response: {data}", flush=True)
        print(f"[DEBUG] MarketSnapshot head: {data.get('head')}", flush=True)
        print(f"[DEBUG] MarketSnapshot body keys: {list(data.get('body', {}).keys())}", flush=True)
        body_data = data.get("body", {}).get("Data", [])
        print(f"[DEBUG] MarketSnapshot Data array length: {len(body_data) if body_data else 0}", flush=True)

        if _head_ok(data):
            # Normalize LastTradedPrice → LastRate for consistent usage across the codebase
            quotes = []
            for q in data["body"]["Data"]:
                q["LastRate"] = q.get("LastTradedPrice") or q.get("LastRate", 0)
                quotes.append(q)
            return {"success": True, "quotes": quotes}

        logger.error(f"get_market_quote failed: {_head_error(data)}")
        return {"success": False, "error": _head_error(data)}

    except Exception as e:
        logger.error(f"get_market_quote exception: {str(e)}")
        return {"success": False, "error": str(e)}


_raw_scrip_cache = None  # full scrip master with all columns, cached once per session
_option_cache = {}      # option instruments by (stock, expiry_str)


def _load_raw_scrip_master():
    """Download scrip master CSV, keeping only NSE derivative options to save memory."""
    global _raw_scrip_cache
    if _raw_scrip_cache is not None:
        return _raw_scrip_cache
    url = "https://images.5paisa.com/website/scripmaster-csv-format.csv"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        lines = response.text.strip().split("\n")
        headers = [h.strip() for h in lines[0].split(",")]
        rows = []
        for line in lines[1:]:
            values = line.split(",")
            if len(values) < len(headers):
                continue
            row = dict(zip(headers, [v.strip() for v in values]))
            if row.get("Exch") == "N" and row.get("ExchType") == "D" and row.get("CpType") in ("CE", "PE"):
                rows.append(row)
        _raw_scrip_cache = rows
        logger.info(f"Option scrip master loaded: {len(rows)} NSE F&O option rows")
        return rows
    except Exception as e:
        logger.error(f"Failed to load raw scrip master: {str(e)}")
        return []


def get_option_chain(access_token, stock_name, expiry_date, strike_price):
    """
    Build option chain from scrip master + MarketSnapshot.
    Replaces the unavailable V1/OptionChain endpoint.
    expiry_date: datetime.date object
    strike_price: approximate CE strike to look for
    """
    global _option_cache
    expiry_str = expiry_date.strftime("%Y-%m-%d")
    cache_key = f"{stock_name.upper()}_{expiry_str}"

    if cache_key not in _option_cache:
        rows = _load_raw_scrip_master()
        options = []
        for row in rows:
            if (row.get("Exch") != "N" or
                    row.get("ExchType") != "D" or
                    row.get("Root", "").upper() != stock_name.upper() or
                    row.get("CpType") not in ("CE", "PE") or
                    expiry_str not in row.get("Expiry", "")):
                continue
            try:
                options.append({
                    "ScripCode": row.get("Scripcode", ""),
                    "StrikeRate": float(row.get("StrikeRate", 0)),
                    "CpType": row.get("CpType", ""),
                })
            except (ValueError, TypeError):
                continue
        _option_cache[cache_key] = options
        logger.info(f"Option cache built for {stock_name} {expiry_str}: {len(options)} contracts")

    instruments = _option_cache.get(cache_key, [])
    if not instruments:
        return {"success": False, "error": f"No options found for {stock_name} expiry {expiry_str} in scrip master"}

    # Find CE at nearest available strike >= strike_price
    ce_candidates = sorted(
        [o for o in instruments if o["CpType"] == "CE" and o["StrikeRate"] >= float(strike_price)],
        key=lambda o: o["StrikeRate"]
    )
    ce_instrument = ce_candidates[0] if ce_candidates else None

    # Find PE options at strikes below or equal to CE strike
    actual_ce_strike = ce_instrument["StrikeRate"] if ce_instrument else float(strike_price)
    pe_instruments = sorted(
        [o for o in instruments if o["CpType"] == "PE" and o["StrikeRate"] <= actual_ce_strike],
        key=lambda o: o["StrikeRate"], reverse=True
    )[:10]

    # Fetch live prices via MarketSnapshot
    scrip_list = []
    if ce_instrument:
        scrip_list.append({"exchange": "N", "exchange_type": "D", "scrip_code": ce_instrument["ScripCode"]})
    for pe in pe_instruments:
        scrip_list.append({"exchange": "N", "exchange_type": "D", "scrip_code": pe["ScripCode"]})

    if not scrip_list:
        return {"success": False, "error": f"No option scrip codes found for {stock_name}"}

    quote_result = get_market_quote(access_token, scrip_list)
    if not quote_result["success"]:
        return {"success": False, "error": f"MarketSnapshot failed for options: {quote_result['error']}"}

    quotes = {str(q["ScripCode"]): q["LastRate"] for q in quote_result["quotes"]}

    option_chain = []
    if ce_instrument:
        option_chain.append({
            "StrikeRate": ce_instrument["StrikeRate"],
            "CPType": "CE",
            "Scripcode": ce_instrument["ScripCode"],
            "LastRate": quotes.get(str(ce_instrument["ScripCode"]), 0)
        })
    for pe in pe_instruments:
        option_chain.append({
            "StrikeRate": pe["StrikeRate"],
            "CPType": "PE",
            "Scripcode": pe["ScripCode"],
            "LastRate": quotes.get(str(pe["ScripCode"]), 0)
        })

    return {"success": True, "option_chain": option_chain}


_scrip_cache = None  # downloaded once per server session, never on every search


def get_scrip_master():
    """
    Download the full instrument list from 5paisa (cached in memory after first call).
    Filters to NSE-only instruments and normalises column names for the frontend.
    """
    global _scrip_cache
    if _scrip_cache is not None:
        return {"success": True, "instruments": _scrip_cache}

    url = "https://images.5paisa.com/website/scripmaster-csv-format.csv"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        lines = response.text.strip().split("\n")
        headers = [h.strip() for h in lines[0].split(",")]
        instruments = []

        for line in lines[1:]:
            values = line.split(",")
            if len(values) < len(headers):
                continue
            row = dict(zip(headers, [v.strip() for v in values]))

            if row.get("Exch") != "N":  # NSE instruments only
                continue

            instruments.append({
                "Symbol":   row.get("Name", ""),
                "FullName": row.get("FullName", ""),
                "ScripCode": row.get("Scripcode", ""),
                "ExchType": row.get("ExchType", ""),
                "Series":   row.get("Series", ""),
                "CpType":   row.get("CpType", ""),
            })

        _scrip_cache = instruments
        logger.info(f"Scrip master loaded: {len(instruments)} NSE instruments cached")
        return {"success": True, "instruments": instruments}

    except Exception as e:
        logger.error(f"get_scrip_master exception: {str(e)}")
        return {"success": False, "error": str(e)}
