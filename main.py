from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from database import init_db, SessionLocal
from bot.scheduler import start_scheduler, stop_scheduler
from models.settings import Settings
from utils.uiauth import COOKIE_NAME, verify_token

from routes import auth, access, dashboard, watchlist, fixed_trades, settings, logs, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    # Warm the 5paisa instrument cache in the background so the very first stock
    # search is instant (otherwise the first search blocks while the CSV downloads).
    import threading

    def _warm_scrip_cache():
        try:
            from broker.fivepaisa import get_scrip_master
            get_scrip_master()
        except Exception:
            pass

    threading.Thread(target=_warm_scrip_cache, daemon=True).start()
    yield
    stop_scheduler()


app = FastAPI(title="Trading Bot", lifespan=lifespan)


# ── UI access gate ────────────────────────────────────────────────────────────
# Everything is behind the login page EXCEPT the login page itself, its API, and
# static assets (the login page needs its CSS/JS). A trusted device carries the
# signed cookie and never sees the login page again until its cache is cleared.
_PUBLIC_PATHS = {
    "/login",
    "/api/access/login",
    "/api/access/setup",
    "/api/access/status",
    "/favicon.ico",
}

# Cache the signing secret so we don't hit the DB on every request.
_secret_cache = {"value": None}


def _get_ui_secret():
    if _secret_cache["value"]:
        return _secret_cache["value"]
    db = SessionLocal()
    try:
        s = db.query(Settings).first()
        if s and s.ui_session_secret:
            _secret_cache["value"] = s.ui_session_secret
        return _secret_cache["value"]
    finally:
        db.close()


def _is_authed(request: Request) -> bool:
    secret = _get_ui_secret()
    if not secret:
        # No login configured yet -> not authed, so page navigations are pushed to
        # /login, which shows the one-time setup screen (its APIs are public).
        return False
    token = request.cookies.get(COOKIE_NAME, "")
    return bool(token) and verify_token(secret, token) is not None


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if (path in _PUBLIC_PATHS or path.startswith("/static")
            or _is_authed(request)):
        return await call_next(request)
    # Not authenticated: APIs get 401 JSON, page navigations get redirected.
    if path.startswith("/api"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse(url="/login")


# Register all routes
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(access.router, prefix="/api/access", tags=["Access"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(watchlist.router, prefix="/api/watchlist", tags=["Watchlist"])
app.include_router(fixed_trades.router, prefix="/api/fixed-trades", tags=["Fixed Trades"])
app.include_router(settings.router, prefix="/api/settings", tags=["Settings"])
app.include_router(logs.router, prefix="/api/logs", tags=["Logs"])
app.include_router(reports.router, prefix="/api/reports", tags=["Reports"])

# Serve frontend files
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/login")
def serve_login():
    return FileResponse("frontend/login.html")


@app.get("/")
def serve_dashboard():
    return FileResponse("frontend/dashboard.html")


@app.get("/admin")
def serve_admin():
    return FileResponse("frontend/admin.html")
