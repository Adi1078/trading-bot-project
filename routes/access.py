"""
Login page backend (UI access control). Separate from broker OAuth (routes/auth.py).

Flow:
  GET  /api/access/status      -> {"configured": bool}  (is a login set up yet?)
  POST /api/access/setup       -> first-time create of username/password (only when
                                  none configured), then logs the device in.
  POST /api/access/login       -> verify username/password, set trusted-device cookie.
  POST /api/access/credentials -> change username/password (must be logged in and
                                  supply the current password). Does NOT log other
                                  devices out — existing cookies stay valid.
"""
import secrets

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models.settings import Settings
from models.log import Log
from utils.uiauth import (
    COOKIE_NAME,
    COOKIE_MAX_AGE,
    hash_password,
    verify_password,
    make_token,
    verify_token,
)

router = APIRouter()


class Credentials(BaseModel):
    username: str
    password: str


class ChangeCredentials(BaseModel):
    current_password: str
    new_username: str
    new_password: str


def _get_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _ensure_secret(db: Session, settings: Settings) -> str:
    if not settings.ui_session_secret:
        settings.ui_session_secret = secrets.token_hex(32)
        db.commit()
    return settings.ui_session_secret


def _set_cookie(response, secret: str, username: str):
    """Attach the long-lived trusted-device cookie. secure=False because the server
    is served over plain HTTP; samesite=lax so the cookie rides normal navigation."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_token(secret, username),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


@router.get("/status")
def status(db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    configured = bool(settings and settings.ui_username and settings.ui_password_hash)
    return {"configured": configured}


@router.post("/setup")
def setup(creds: Credentials, request: Request, db: Session = Depends(get_db)):
    settings = _get_settings(db)
    if settings.ui_username and settings.ui_password_hash:
        return {"success": False, "error": "Login is already configured. Please sign in."}

    username = creds.username.strip()
    if len(username) < 3 or len(creds.password) < 6:
        return {"success": False, "error": "Username must be 3+ chars and password 6+ chars."}

    settings.ui_username = username
    settings.ui_password_hash = hash_password(creds.password)
    secret = _ensure_secret(db, settings)
    db.add(Log(level="INFO", message=f"UI login configured for user '{username}'"))
    db.commit()

    from fastapi.responses import JSONResponse
    resp = JSONResponse({"success": True})
    _set_cookie(resp, secret, username)
    return resp


@router.post("/login")
def login(creds: Credentials, db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    if not settings or not settings.ui_username or not settings.ui_password_hash:
        return {"success": False, "error": "No login configured yet."}

    if (creds.username.strip().lower() != (settings.ui_username or "").lower()
            or not verify_password(creds.password, settings.ui_password_hash)):
        db.add(Log(level="WARNING", message=f"Failed UI login attempt for '{creds.username}'"))
        db.commit()
        return {"success": False, "error": "Invalid username or password."}

    secret = _ensure_secret(db, settings)
    db.add(Log(level="INFO", message=f"UI login success for '{settings.ui_username}' (new device)"))
    db.commit()

    from fastapi.responses import JSONResponse
    resp = JSONResponse({"success": True})
    _set_cookie(resp, secret, settings.ui_username)
    return resp


@router.post("/credentials")
def change_credentials(body: ChangeCredentials, request: Request, db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    if not settings or not settings.ui_password_hash:
        return {"success": False, "error": "No login configured."}

    # Must be an authenticated device.
    token = request.cookies.get(COOKIE_NAME, "")
    if not settings.ui_session_secret or not verify_token(settings.ui_session_secret, token):
        return {"success": False, "error": "Not authenticated."}

    if not verify_password(body.current_password, settings.ui_password_hash):
        return {"success": False, "error": "Current password is incorrect."}

    username = body.new_username.strip()
    if len(username) < 3 or len(body.new_password) < 6:
        return {"success": False, "error": "Username must be 3+ chars and password 6+ chars."}

    settings.ui_username = username
    settings.ui_password_hash = hash_password(body.new_password)
    # Note: signing secret is intentionally left unchanged so already-trusted
    # devices stay logged in.
    db.add(Log(level="INFO", message=f"UI login credentials updated (user now '{username}')"))
    db.commit()
    return {"success": True, "message": "Login updated. This and other signed-in devices stay logged in."}
