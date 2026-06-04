import logging
import threading
import time
from datetime import date
from utils.helpers import get_ist_now, is_market_hours, is_safety_check_time, is_within_trading_window
from utils.exchange_calendar import is_last_trading_day
from database import SessionLocal
from models.settings import Settings
from models.log import Log

logger = logging.getLogger(__name__)

_scheduler_running = False
_scheduler_thread = None


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


def _fixed_trades_ran_today():
    """Check DB to see if fixed trades already ran today — survives restarts and time changes."""
    db = SessionLocal()
    try:
        today = str(date.today())
        count = db.query(Log).filter(
            Log.level == "INFO",
            Log.message == "Scheduler: triggering fixed trades",
            Log.created_at >= today
        ).count()
        return count > 0
    except Exception:
        return False
    finally:
        db.close()


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


def _token_reminder_sent_today():
    """Check DB to see if 8:30 AM token reminder was already sent today."""
    db = SessionLocal()
    try:
        today = str(date.today())
        count = db.query(Log).filter(
            Log.level == "INFO",
            Log.message == "Scheduler: sent daily token reconnect reminder",
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

    _save_log("INFO", "Scheduler started")

    while _scheduler_running:
        try:
            now = get_ist_now()
            settings = _get_settings()

            # ── Daily 8:30 AM reconnect reminder (runs outside market hours) ──
            if now.hour == 8 and now.minute == 30 and not _token_reminder_sent_today():
                _save_log("INFO", "Scheduler: sent daily token reconnect reminder")
                try:
                    from notifications.email import send_token_expiry_reminder
                    send_token_expiry_reminder()
                except Exception as e:
                    _save_log("ERROR", f"Scheduler: token reminder email failed - {str(e)}")

            # Only run during market hours
            if not is_market_hours():
                time.sleep(60)
                continue

            # Check if master trading switch is on
            if not settings or not settings.is_trading:
                time.sleep(60)
                continue

            trade_start = settings.trade_start_time or "09:30"
            trade_close = settings.trade_close_time or "12:00"

            # ── Place fixed trades once per day at configured start time ──
            if is_within_trading_window(trade_start, trade_close) and not _fixed_trades_ran_today():
                _save_log("INFO", "Scheduler: triggering fixed trades")
                try:
                    from bot.trade_manager import run_fixed_trades
                    run_fixed_trades()
                except Exception as e:
                    _save_log("ERROR", f"Scheduler: run_fixed_trades failed - {str(e)}")

            # ── Monitor open trades every loop (~10s) ──
            try:
                from bot.trade_manager import monitor_open_trades
                monitor_open_trades()
            except Exception as e:
                _save_log("ERROR", f"Scheduler: monitor_open_trades failed - {str(e)}")

            # ── Position sync every 5 minutes ──
            if last_sync_time is None or (now - last_sync_time).seconds >= 300:
                try:
                    from bot.position_sync import sync_positions
                    sync_positions()
                    last_sync_time = now
                except Exception as e:
                    _save_log("ERROR", f"Scheduler: position sync failed - {str(e)}")

            # ── Safety check at 3:40 PM once per day ──
            if is_safety_check_time() and not _safety_check_ran_today():
                _save_log("INFO", "Scheduler: running 3:40 PM safety check")
                try:
                    from bot.trade_manager import safety_check
                    safety_check()
                except Exception as e:
                    _save_log("ERROR", f"Scheduler: safety_check failed - {str(e)}")

        except Exception as e:
            _save_log("ERROR", f"Scheduler loop error: {str(e)}")

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
