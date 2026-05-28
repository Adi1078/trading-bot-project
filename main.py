from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from database import init_db
from bot.scheduler import start_scheduler, stop_scheduler

from routes import auth, dashboard, watchlist, fixed_trades, webhook, settings, logs, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Trading Bot", lifespan=lifespan)


# Register all routes
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(watchlist.router, prefix="/api/watchlist", tags=["Watchlist"])
app.include_router(fixed_trades.router, prefix="/api/fixed-trades", tags=["Fixed Trades"])
app.include_router(webhook.router, prefix="/api/webhook", tags=["Webhook"])
app.include_router(settings.router, prefix="/api/settings", tags=["Settings"])
app.include_router(logs.router, prefix="/api/logs", tags=["Logs"])
app.include_router(reports.router, prefix="/api/reports", tags=["Reports"])

# Serve frontend files
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
def serve_dashboard():
    return FileResponse("frontend/dashboard.html")


@app.get("/admin")
def serve_admin():
    return FileResponse("frontend/admin.html")
