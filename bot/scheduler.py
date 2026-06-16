import logging
import threading
import time
import traceback
from datetime import date
from utils.helpers import get_ist_now, is_market_hours, is_safety_check_time
from database import SessionLocal
from models.settings import Settings
from models.log import Log

logger = logging.getLogger(__name__)

_scheduler_running = False
_scheduler_thread = None
_last_totp_attempt = None          # throttles auto-login retries
_already_connected_logged = None   # date we last logged the "already connected" skip

# Auto-login fires from this time onward (well before the 9:15 AM market open).
AUTO_LOGIN_FROM = (8, 0)


def _auto_login_if_needed(now):
    """
    Automated daily broker login via TOTP. On a trading day, from AUTO_LOGIN_FROM
    onward, if the TOTP credentials are configured and today's token isn't valid
    yet, generate the TOTP and log in — so the broker is connected before market
    open with no manual action. Retries (throttled to every 2 min) until it
    succeeds. The manual Connect Broker button remains as a fallback.
    """
    global _last_totp_attempt, _already_connected_logged
    from utils.exchange_calendar import is_trading_day

    if not is_trading_day(now.date()):
        return
    if (now.hour, now.minute) < AUTO_LOGIN_FROM:
        return

    today = str(date.today())
    db = SessionLocal()
    try:
        s = db.query(Settings).first()
        # Need all three TOTP credentials; otherwise rely on manual Connect Broker.
        if not s or not s.totp_secret or not s.client_code or not s.login_pin:
            return
        # Already connected for today — log once (not every loop), then skip.
        if s.access_token and s.token_date == today:
            if _already_connected_logged != today:
                _save_log("INFO", "Scheduler: broker already connected for today — skipping auto-login")
                _already_connected_logged = today
            return
        secret, client_code, pin = s.totp_secret, s.client_code, s.login_pin
    finally:
        db.close()

    # Throttle: at most one attempt every 2 minutes (avoids hammering on failure).
    if _last_totp_attempt and (now - _last_totp_attempt).total_seconds() < 120:
        return
    _last_totp_attempt = now

    _save_log("INFO", "Scheduler: attempting TOTP auto-login")
    try:
        from broker.fivepaisa import connect_via_totp
        result = connect_via_totp(secret, client_code, pin)
    except Exception as e:
        import traceback
        _save_log("ERROR", f"Scheduler: TOTP auto-login exception - {e}\n{traceback.format_exc()}")
        return

    if result.get("success"):
        db = SessionLocal()
        try:
            s2 = db.query(Settings).first()
            s2.access_token = result.get("access_token", "")
            if result.get("client_code"):
                s2.client_code = result["client_code"]
            s2.token_date = today
            db.commit()
        finally:
            db.close()
        _save_log("INFO", f"Scheduler: TOTP auto-login SUCCESS (client {result.get('client_code')})")
    else:
        _save_log("ERROR", f"Scheduler: TOTP auto-login failed - {result.get('error')}")


def _save_log(level: str, message: str):
    db = SessionLocal()
    try:
        log = Log(level=level, message=message)
        db.add(log)
        db.commit()
    finally:
        db.close()


def _get_settings():
    db = SessionLocal()
    try:
        return db.query(Settings).first()
    finally:
        db.close()


def _fixed_trades_fired_for(start_time_str):
    """
    True if fixed trades already fired today for THIS entry time. Keyed by the
    entry time, so changing the entry time later in the day re-arms them to fire
    again at the new time. Survives restarts (checks the DB log).
    """
    db = SessionLocal()
    try:
        today = str(date.today())
        msg = f"Scheduler: triggering fixed trades @{start_time_str}"
        count = db.query(Log).filter(
            Log.level == "INFO",
            Log.message == msg,
            Log.created_at >= today
        ).count()
        return count > 0
    except Exception:
        return False
    finally:
        db.close()


def _past_entry_time(now, start_time_str):
    """True if the current time has reached the configured entry time."""
    try:
        h, m = map(int, start_time_str.split(":"))
    except Exception:
        return False
    entry = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return now >= entry


def _safety_check_ran_today():
    """Check DB to see if safety check already ran today."""
    db = SessionLocal()
    try:
        today = str(date.today())
        count = db.query(Log).filter(
            Log.level == "INFO",
            Log.message == "Scheduler: running 3:40 PM safety check",
            Log.created_at >= today
        ).count()
        return count > 0
    except Exception:
        return False
    finally:
        db.close()


def _run_loop():
    """
    Main scheduler loop. Runs every 10 seconds during market hours so open trades
    are checked for target/stop-loss promptly (≤10s lag instead of ~60s). 10s sits
    just above 5paisa's 5-second price cache, so every check gets fresh data.
    Uses the database to track what ran today — no in-memory flags,
    so restarts and time changes work correctly.
    """
    last_sync_time = None
    last_chartink_scan = None

    _save_log("INFO", "Scheduler started")

    while _scheduler_running:
        try:
            now = get_ist_now()
            settings = _get_settings()

            # ── End-of-day open-trades summary at 3:40 PM. This runs AFTER market
            #    close (3:30 PM), so it MUST sit outside the is_market_hours() gate
            #    below — otherwise the loop bails out before reaching it and the
            #    summary email never sends. Only on trading days; once per day. ──
            if is_safety_check_time() and not _safety_check_ran_today():
                from utils.exchange_calendar import is_trading_day
                if is_trading_day(now.date()):
                    _save_log("INFO", "Scheduler: running 3:40 PM safety check")
                    try:
                        from bot.trade_manager import safety_check
                        safety_check()
                    except Exception as e:
                        _save_log("ERROR", f"Scheduler: safety_check failed - {str(e)}")

            # ── Automated daily broker login via TOTP (from 8:00 AM). Runs OUTSIDE
            #    the market-hours gate so it connects before the 9:15 AM open. ──
            try:
                _auto_login_if_needed(now)
            except Exception as e:
                _save_log("ERROR", f"Scheduler: auto-login check failed - {str(e)}")

            # Only run during market hours
            if not is_market_hours():
                time.sleep(60)
                continue

            # Check if master trading switch is on
            if not settings or not settings.is_trading:
                time.sleep(60)
                continue

            trade_start = settings.trade_start_time or "09:30"

            # ── Place fixed trades when the clock reaches the entry time ──
            # Fires once per entry-time value: if the entry time is changed later
            # in the day, it fires again at the new time. No looping/re-entry.
            if _past_entry_time(now, trade_start) and not _fixed_trades_fired_for(trade_start):
                _save_log("INFO", f"Scheduler: triggering fixed trades @{trade_start}")
                try:
                    from bot.trade_manager import run_fixed_trades
                    run_fixed_trades()
                except Exception as e:
                    _save_log("ERROR", f"Scheduler: run_fixed_trades failed - {e}\n{traceback.format_exc()}")

            # ── Monitor open trades every loop (~10s) ──
            try:
                from bot.trade_manager import monitor_open_trades
                monitor_open_trades()
            except Exception as e:
                _save_log("ERROR", f"Scheduler: monitor_open_trades failed - {e}\n{traceback.format_exc()}")

            # ── Position sync every 5 minutes ──
            if last_sync_time is None or (now - last_sync_time).seconds >= 300:
                try:
                    from bot.position_sync import sync_positions
                    sync_positions()
                    last_sync_time = now
                except Exception as e:
                    _save_log("ERROR", f"Scheduler: position sync failed - {e}\n{traceback.format_exc()}")

            # ── Chartink screener scan every 5 minutes (runs in background so it
            #    never blocks the monitor loop). run_chartink_cycle has its own guards. ──
            if last_chartink_scan is None or (now - last_chartink_scan).seconds >= 300:
                try:
                    from bot.trade_manager import run_chartink_cycle
                    threading.Thread(target=run_chartink_cycle, daemon=True).start()
                    last_chartink_scan = now
                except Exception as e:
                    _save_log("ERROR", f"Scheduler: chartink scan failed - {e}\n{traceback.format_exc()}")

        except Exception as e:
            _save_log("ERROR", f"Scheduler loop error: {e}\n{traceback.format_exc()}")

        time.sleep(10)

    _save_log("INFO", "Scheduler stopped")


def start_scheduler():
    """Start the scheduler in a background thread."""
    global _scheduler_running, _scheduler_thread

    if _scheduler_running:
        logger.warning("Scheduler is already running")
        return

    _scheduler_running = True
    _scheduler_thread = threading.Thread(target=_run_loop, daemon=True)
    _scheduler_thread.start()
    logger.info("Scheduler thread started")


def stop_scheduler():
    """Stop the scheduler loop."""
    global _scheduler_running
    _scheduler_running = False
    logger.info("Scheduler stop requested")
