from datetime import datetime
from sqlalchemy import Column, String, Integer, JSON, DateTime
from .db import Base

class Product(Base):
    __tablename__ = "products"
    id          = Column(String, primary_key=True)
    name        = Column(String, nullable=False)
    price_paise = Column(Integer, nullable=False)   # never float
    stock       = Column(Integer, nullable=False)
    category    = Column(String, nullable=False)
    description = Column(String, default="")
    reviews     = Column(JSON, default=list)        # untrusted text

class AuditLog(Base):
    __tablename__ = "audit_log"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    trace_id     = Column(String, index=True, nullable=False)
    actor        = Column(String, nullable=False)   # "merchant" | "agent"
    step         = Column(String, nullable=False)
    decision     = Column(String, nullable=False)   # "ok" | "refused"
    reason       = Column(String, default="")
    amount_paise = Column(Integer, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)