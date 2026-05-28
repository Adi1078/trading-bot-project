from fastapi import APIRouter, Depends
from typing import Optional
from sqlalchemy.orm import Session
from database import get_db
from models.log import Log

router = APIRouter()


@router.get("/")
def get_logs(level: Optional[str] = None, limit: int = 100, db: Session = Depends(get_db)):
    """
    Get logs ordered by latest first.
    Optionally filter by level: INFO, ERROR, WARNING
    """
    query = db.query(Log).order_by(Log.created_at.desc())

    if level:
        query = query.filter(Log.level == level.upper())

    logs = query.limit(limit).all()

    return {
        "logs": [
            {
                "id": log.id,
                "level": log.level,
                "message": log.message,
                "created_at": str(log.created_at)
            }
            for log in logs
        ]
    }


@router.delete("/clear")
def clear_logs(db: Session = Depends(get_db)):
    """Clear all logs from the database."""
    count = db.query(Log).count()
    db.query(Log).delete()
    db.commit()
    return {"success": True, "deleted": count}
