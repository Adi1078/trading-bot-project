from sqlalchemy import Column, Integer, String, DateTime
from database import Base
from utils.helpers import get_ist_now


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    level = Column(String, nullable=False)     # "INFO", "ERROR", "WARNING"
    message = Column(String, nullable=False)
    created_at = Column(DateTime, default=get_ist_now)
