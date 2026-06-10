from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from datetime import date
from database import get_db
from models.settings import Settings
from models.log import Log
from broker.fivepaisa import get_access_token
from config import CALLBACK_URL

router = APIRouter()

FIVEPAISA_LOGIN_URL = "https://dev-openapi.5paisa.com/WebVendorLogin/VLogin/Index"


@router.get("/login")
def login(db: Session = Depends(get_db)):
    """Redirect client to 5paisa OAuth login page."""
    settings = db.query(Settings).first()
    if not settings or not settings.app_key:
        return {"success": False, "error": "App key not configured. Please set it in admin panel."}

    login_url = f"{FIVEPAISA_LOGIN_URL}?VendorKey={settings.app_key}&ResponseURL={CALLBACK_URL}"

    # DEBUG: Log the exact URL being generated
    print("\n" + "="*80)
    print("DEBUG: OAuth Login URL Generation")
    print("="*80)
    print(f"CALLBACK_URL from config: {CALLBACK_URL}")
    print(f"Full login_url sent to browser: {login_url}")
    print(f"ResponseURL parameter: {CALLBACK_URL}")
    if "8181" in login_url:
        print("✓ login_url CONTAINS port 8181")
    if "8001" in login_url:
        print("✗ login_url CONTAINS port 8001 (BAD)")
    print("="*80 + "\n")

    return RedirectResponse(url=login_url)


@router.get("/callback")
def callback(RequestToken: str, db: Session = Depends(get_db)):
    """5paisa redirects here after login with the RequestToken."""
    result = get_access_token(RequestToken)

    if not result["success"]:
        log = Log(level="ERROR", message=f"Failed to get access token: {result['error']}")
        db.add(log)
        db.commit()
        return RedirectResponse(url="/?error=broker_auth_failed")

    access_token = result.get("access_token", "")
    client_code = result.get("client_code", "")
    client_name = result.get("client_name", "")

    if not access_token:
        log = Log(level="ERROR", message=f"Access token is empty after OAuth. Full result: {result}")
        db.add(log)
        db.commit()
        return RedirectResponse(url="/?error=broker_auth_failed")

    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)

    settings.access_token = access_token
    settings.client_code = client_code
    settings.token_date = str(date.today())

    log = Log(level="INFO", message=f"Broker connected. Client: {client_name} ({client_code})")
    db.add(log)
    db.commit()

    return RedirectResponse(url="/")


@router.get("/status")
def auth_status(db: Session = Depends(get_db)):
    """Check if broker is connected today."""
    settings = db.query(Settings).first()
    if not settings or not settings.access_token:
        return {"connected": False}

    connected = settings.token_date == str(date.today())
    return {"connected": connected}
