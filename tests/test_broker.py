"""
Tests for broker/fivepaisa.py functions.
All actual HTTP calls are mocked — no real credentials needed.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock
from broker.fivepaisa import (
    get_access_token,
    place_order,
    cancel_order,
    get_positions,
    get_market_quote
)


# ── Mock Responses ────────────────────────────────────────────────────────────

def mock_access_token_success():
    return {
        "head": {"Status": 0, "StatusDescription": "Success"},
        "body": {
            "Status": 0,
            "AccessToken": "fake_access_token_123",
            "ClientCode": "TEST001",
            "ClientName": "Test Client",
            "Message": ""
        }
    }

def mock_access_token_failure():
    return {
        "head": {"Status": 0, "StatusDescription": "Success"},
        "body": {
            "Status": 1,
            "AccessToken": "",
            "ClientCode": "",
            "ClientName": "",
            "Message": "Invalid Vendor EncryKey."
        }
    }

def mock_place_order_success():
    return {
        "head": {"status": "0", "statusDescription": "Success"},
        "body": {
            "BrokerOrderID": 123456789,
            "ClientCode": "TEST001",
            "Message": "Success",
            "Status": 0
        }
    }

def mock_place_order_failure():
    return {
        "head": {"status": "0", "statusDescription": "Success"},
        "body": {
            "BrokerOrderID": 0,
            "Message": "Authentication Fails",
            "Status": 9
        }
    }

def mock_cancel_order_success():
    return {"head": {"Status": 0, "StatusDescription": "Success"},
            "body": {"Status": 0, "Message": "Success"}}


def mock_cancel_order_body_fail():
    # head OK but body-level failure (5paisa's head-vs-body trap)
    return {"head": {"Status": 0, "StatusDescription": "Success"},
            "body": {"Status": 2, "Message": "Invalid Input Parameters."}}

def mock_positions_success():
    return {
        "head": {"Status": 0, "StatusDescription": "Success"},
        "body": {
            "NetPositionDetail": [
                {"ScripCode": "1660", "NetQty": 1, "OrderID": "123456789"}
            ]
        }
    }

def mock_market_quote_success():
    return {
        "head": {"Status": 0, "StatusDescription": "Success"},
        "body": {
            "Data": [
                {"ScripCode": "1660", "LastRate": 1126.3}
            ]
        }
    }


# ── get_access_token Tests ────────────────────────────────────────────────────

@patch("broker.fivepaisa.requests.post")
def test_get_access_token_success(mock_post):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: mock_access_token_success(),
        raise_for_status=lambda: None
    )
    result = get_access_token("fake_request_token")
    assert result["success"] is True
    assert result["access_token"] == "fake_access_token_123"
    assert result["client_code"] == "TEST001"

@patch("broker.fivepaisa.requests.post")
def test_get_access_token_failure(mock_post):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: mock_access_token_failure(),
        raise_for_status=lambda: None
    )
    result = get_access_token("fake_request_token")
    assert result["success"] is False
    assert "error" in result

@patch("broker.fivepaisa.requests.post")
def test_get_access_token_network_error(mock_post):
    mock_post.side_effect = Exception("Connection timeout")
    result = get_access_token("fake_request_token")
    assert result["success"] is False
    assert "Connection timeout" in result["error"]


# ── place_order Tests ─────────────────────────────────────────────────────────

@patch("broker.fivepaisa.requests.post")
def test_place_order_success(mock_post):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: mock_place_order_success(),
        raise_for_status=lambda: None
    )
    result = place_order(
        access_token="fake_token",
        exchange="N",
        exchange_type="D",
        scrip_code="INFY_20260528",
        order_type="B",
        price=0,
        qty=1,
        is_intraday=False,
        remote_order_id="INFY_TEST_001"
    )
    assert result["success"] is True
    assert result["broker_order_id"] == 123456789

@patch("broker.fivepaisa.requests.post")
def test_place_order_auth_failure(mock_post):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: mock_place_order_failure(),
        raise_for_status=lambda: None
    )
    result = place_order(
        access_token="expired_token",
        exchange="N", exchange_type="D",
        scrip_code="INFY_20260528",
        order_type="B", price=0, qty=1,
        is_intraday=False,
        remote_order_id="INFY_TEST_002"
    )
    assert result["success"] is False

@patch("broker.fivepaisa.requests.post")
def test_cancel_order_success(mock_post):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: mock_cancel_order_success(),
        raise_for_status=lambda: None
    )
    result = cancel_order("fake_token", "2400000016897929")
    assert result["success"] is True


@patch("broker.fivepaisa.requests.post")
def test_cancel_order_body_status_failure(mock_post):
    """head.Status '0' but body.Status != 0 must be treated as a FAILED cancel."""
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: mock_cancel_order_body_fail(),
        raise_for_status=lambda: None
    )
    result = cancel_order("fake_token", "2400000016897929")
    assert result["success"] is False

@patch("broker.fivepaisa.requests.post")
def test_get_positions_success(mock_post):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: mock_positions_success(),
        raise_for_status=lambda: None
    )
    result = get_positions("fake_token", "TEST001")
    assert result["success"] is True
    assert len(result["positions"]) == 1
    assert result["positions"][0]["ScripCode"] == "1660"

@patch("broker.fivepaisa.requests.post")
def test_get_positions_invalid_session_is_failure(mock_post):
    """
    On an expired token 5paisa returns head.status '0' (looks OK) but body.Status 9
    'Invalid Session' with an EMPTY NetPositionDetail. That empty list is identical
    to a flat account, so get_positions MUST report failure — otherwise the
    square-off / sync logic would treat 'couldn't read' as 'no open position' and
    wrongly mark live trades closed.
    """
    invalid_session = {
        "head": {"responseCode": "5PNPNWV2", "status": "0", "statusDescription": "Invalid Session"},
        "body": {"Message": "Invalid Session", "NetPositionDetail": [], "Status": 9},
    }
    mock_post.return_value = MagicMock(
        status_code=200, json=lambda: invalid_session, raise_for_status=lambda: None
    )
    result = get_positions("expired_token", "TEST001")
    assert result["success"] is False
    assert "Invalid Session" in result["error"]


@patch("broker.fivepaisa.requests.post")
def test_get_positions_no_record_is_flat(mock_post):
    """body.Status 1 = 'No record found' is a genuinely flat account → success with
    an empty positions list (NOT an error)."""
    no_record = {
        "head": {"status": "0", "statusDescription": "Success"},
        "body": {"Message": "No record found", "NetPositionDetail": [], "Status": 1},
    }
    mock_post.return_value = MagicMock(
        status_code=200, json=lambda: no_record, raise_for_status=lambda: None
    )
    result = get_positions("fake_token", "TEST001")
    assert result["success"] is True
    assert result["positions"] == []


@patch("broker.fivepaisa.requests.post")
def test_get_market_quote_success(mock_post):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: mock_market_quote_success(),
        raise_for_status=lambda: None
    )
    result = get_market_quote(
        "fake_token",
        [{"exchange": "N", "exchange_type": "D", "scrip_code": "1660"}]
    )
    assert result["success"] is True
    assert result["quotes"][0]["LastRate"] == 1126.3
