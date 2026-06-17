from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_columns():
    """
    Add columns introduced after a database was first created. SQLite's
    create_all() only creates missing tables, never alters existing ones, so new
    columns on the live DB need an explicit (additive, safe) ALTER TABLE.
    """
    from sqlalchemy import text
    migrations = {
        "settings": {
            "webhook_is_paper": "BOOLEAN DEFAULT 0",
            "screener_1_url": "VARCHAR",
            "screener_1_clause": "TEXT",
            "screener_2_url": "VARCHAR",
            "screener_2_clause": "TEXT",
            "screener_3_url": "VARCHAR",
            "screener_3_clause": "TEXT",
            "totp_secret": "VARCHAR",
            "login_pin": "VARCHAR",
            "ui_username": "VARCHAR",
            "ui_password_hash": "VARCHAR",
            "ui_session_secret": "VARCHAR",
        },
        "trades": {
            "expiry_date": "VARCHAR",
        },
    }
    try:
        with engine.connect() as conn:
            for table, cols in migrations.items():
                existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                for col, ddl in cols.items():
                    if col not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                conn.commit()
    except Exception:
        # Never let a migration hiccup stop the app from starting
        pass


def init_db():
    from models import trade, watchlist, fixed_trades, settings, log
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
