# iAction Pulse — Options Collar Trading Bot

Automated NSE F&O trading bot for the 5paisa broker API.

**Strategy:** a three-legged *collar* — Buy Futures + Sell CE + Buy PE — plus an
optional naked-CE ("Sell CE only") mode. Trades come from two sources: a fixed list
configured in the admin panel, and stocks picked up by Chartink screeners. Positions
are monitored continuously and closed on a profit target, a loss limit, contract
expiry, or manually from the dashboard.

---

## Requirements

- Python 3.11+
- A 5paisa account with API access (App Key, Encryption Key, User ID)
- Linux server for deployment (the bot must run continuously during market hours)

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```ini
# 5paisa API credentials (from the 5paisa developer portal)
APP_KEY=
ENCRY_KEY=
USER_ID=
ALGO_ID=0

# Email notifications (a Gmail App Password, not the account password)
EMAIL_SENDER=
EMAIL_PASSWORD=
EMAIL_RECEIVER=

# Application
DATABASE_URL=sqlite:///./trading_bot.db
SECRET_KEY=<any long random string>
```

Run it:

```bash
uvicorn main:app --host 0.0.0.0 --port 8181
```

- Dashboard: `http://<host>:8181/`
- Admin panel: `http://<host>:8181/admin`

The database is created automatically on first start.

## First-time configuration

1. Open the dashboard — you will be asked to create the **UI login** (username +
   password). This gates the whole site. The password is stored only as a PBKDF2
   hash, never in plain text.
2. Go to **Admin → Settings** and enter the broker credentials, the **TOTP secret**
   and the **login PIN**. These enable the automated daily login and are stored only
   in the database.
3. Add stocks to the **Watchlist** (the bot will only trade stocks listed here).
4. Add rows under **Fixed Trades**, and/or configure the **Chartink** screeners.
5. Turn the **Trade** switch on in the top bar.

> **Security:** the TOTP secret, PIN, broker credentials and access tokens live only
> in `.env` and the SQLite database. Both are excluded from version control by
> `.gitignore` and must never be committed.

## How it runs

A background scheduler starts with the application and:

- performs the **TOTP auto-login** each morning (~8:00 AM IST)
- fires **fixed trades** at the configured start time
- runs the **Chartink screeners** every 5 minutes (from the start time onward)
- **monitors open trades** every ~10 seconds for target / stop-loss / expiry
- **syncs positions** against the broker every 5 minutes
- emails an **open-positions summary** at 3:40 PM IST

The server should be set to **IST (Asia/Kolkata)**, since all trading times and the
NSE holiday calendar assume it.

## Notes on execution

These behaviours were established in live trading and are deliberate:

- **Orders are marketable limit orders**, priced from the live order book (level-5
  market depth) rather than the last traded price, and rounded to each instrument's
  own tick size. A price off the tick grid is rejected by the exchange.
- **An accepted order is not a filled order.** Every leg is confirmed via the
  broker's order status and net positions before the bot relies on it.
- **Square-off is sequential** — CE → Futures → PE — with up to two
  cancel-then-replace attempts per leg, each verified flat against the broker's real
  net position. The short CE is closed first so the position is never left with an
  uncovered short.
- **A trade is only marked closed once the broker confirms it is flat.** If a
  square-off cannot be confirmed the trade stays open and an alert is emailed.
- **Reported P&L is gross** — it excludes brokerage, STT, exchange fees, GST and
  stamp duty, so it will differ from the broker's net figure. The dashboard provides
  a manual adjustment for reconciliation.

## Tests

```bash
python -m pytest tests/ -q
```

The suite runs entirely against a mocked broker — it never places real orders and
needs no credentials.

## Deployment

Run under `systemd` so it restarts automatically:

```ini
[Unit]
Description=Trading Bot
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/trading-bot-project
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8181
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now trading-bot
sudo journalctl -u trading-bot -f     # live logs
```

To update: `git pull` then `sudo systemctl restart trading-bot`.

## Project layout

```
bot/          strategy engine — entry, monitoring, square-off, scheduler,
              Chartink scanner, position sync
broker/       5paisa API client (auth, orders, positions, quotes, market depth)
models/       database tables
routes/       HTTP API (dashboard, admin, auth, reports)
frontend/     dashboard and admin UI (plain HTML/CSS/JS, no build step)
utils/        IST time helpers, NSE holiday calendar, login/session handling
tests/        mocked-broker test suite
```
