"""
Tests for the Chartink webhook endpoint.
Tests that stocks are correctly checked against the watchlist
and that the trading switch is respected.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base, get_db
from main import app
from models.watchlist import Watchlist
from models.settings import Settings

# ── Test Database Setup ───────────────────────────────────────────────────────

TEST_DB_URL = "sqlite:///./test_webhook.db"
engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = sessionmaker(bind=engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def seed_watchlist(stocks: list):
    db = TestSession()
    for name in stocks:
        db.add(Watchlist(stock_name=name, scrip_code="1234", exchange="N", exchange_type="D"))
    db.commit()
    db.close()


def set_trading(is_trading: bool):
    db = TestSession()
    settings = Settings(is_trading=is_trading, access_token="fake_token", user_id="TEST001")
    db.add(settings)
    db.commit()
    db.close()


# ── Webhook Tests ─────────────────────────────────────────────────────────────

def test_webhook_stock_in_watchlist_gets_triggered():
    """Stock from Chartink that exists in watchlist should be triggered."""
    seed_watchlist(["ONGC", "INFY"])
    set_trading(True)

    response = client.post("/api/webhook/chartink", json={"stocks": "ONGC,TATASTEEL"})
    data = response.json()

    assert response.status_code == 200
    assert data["success"] is True
    assert "ONGC" in data["triggered"]
    assert "TATASTEEL" in data["skipped"]

def test_webhook_stock_not_in_watchlist_is_skipped():
    """Stock from Chartink not in watchlist should be skipped."""
    seed_watchlist(["INFY"])
    set_trading(True)

    response = client.post("/api/webhook/chartink", json={"stocks": "RELIANCE"})
    data = response.json()

    assert response.status_code == 200
    assert "RELIANCE" in data["skipped"]
    assert len(data["triggered"]) == 0

def test_webhook_ignored_when_trading_stopped():
    """Webhook should be ignored when master trading switch is OFF."""
    seed_watchlist(["ONGC"])
    set_trading(False)

    response = client.post("/api/webhook/chartink", json={"stocks": "ONGC"})
    data = response.json()

    assert response.status_code == 200
    assert data["success"] is False
    assert "stopped" in data["error"].lower()

def test_webhook_handles_list_format():
    """Chartink may send stocks as a list instead of comma-separated string."""
    seed_watchlist(["INFY", "ONGC"])
    set_trading(True)

    response = client.post("/api/webhook/chartink", json={"stocks": ["INFY", "ONGC", "HDFC"]})
    data = response.json()

    assert "INFY" in data["triggered"]
    assert "ONGC" in data["triggered"]
    assert "HDFC" in data["skipped"]

def test_webhook_invalid_payload():
    """Invalid JSON should return a clean error."""
    response = client.post("/api/webhook/chartink", content="not json",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False

def test_webhook_empty_stocks():
    """Empty stocks field should return clean error."""
    seed_watchlist(["INFY"])
    set_trading(True)

    response = client.post("/api/webhook/chartink", json={"stocks": ""})
    data = response.json()
    assert data["success"] is False
