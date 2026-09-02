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



class Mandate(Base):
    __tablename__ = "mandates"
    id                 = Column(String, primary_key=True)
    agent_id           = Column(String, nullable=False)
    max_amount_paise   = Column(Integer, nullable=False)
    allowed_categories = Column(JSON, default=list)
    expires_at         = Column(DateTime, nullable=False)
    signature          = Column(String, nullable=False)
    status             = Column(String, default="active")
    parent_mandate_id  = Column(String, nullable=True)
    customer_id = Column(String, nullable=True)

class Order(Base):
    __tablename__ = "orders"
    id                = Column(String, primary_key=True)
    mandate_id        = Column(String, nullable=False)
    trace_id          = Column(String, nullable=False)
    items             = Column(JSON, nullable=False)
    total_paise       = Column(Integer, nullable=False)
    razorpay_order_id = Column(String, nullable=True)
    payment_link_id   = Column(String, nullable=True)
    payment_url       = Column(String, nullable=True)
    status            = Column(String, default="created")
    created_at        = Column(DateTime, default=datetime.utcnow)




class Customer(Base):
    __tablename__ = "customers"
    id            = Column(String, primary_key=True)
    credential_id = Column(String, nullable=True)
    public_key    = Column(String, nullable=True)
    sign_count    = Column(Integer, default=0)