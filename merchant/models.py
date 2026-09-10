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

    # ── what a machine needs in order to choose correctly ──
    # "Rs 800" means nothing without knowing Rs 800 of what.
    unit          = Column(String, default="box")   # box | kg | piece
    net_weight_g  = Column(Integer, nullable=True)  # None where it makes no sense
    # A buyer may say "cashew barfi" or "ಕಾಜು ಕತ್ಲಿ" and mean sw_001.
    # Matching belongs to the catalog, not to whichever model is reading it.
    aliases       = Column(JSON, default=list)
    # Same sweet, several sizes: one group, one label each.
    variant_group = Column(String, nullable=True)
    variant_label = Column(String, nullable=True)
    # What the shopkeeper would offer alongside this — product ids the
    # merchant itself declares. Not a recommender: the shop knows what
    # goes with what, it just had no way to say so to a machine.
    cross_sell    = Column(JSON, default=list)


class Hold(Base):
    """Items set aside at a fixed price for a short time.

    The shopkeeper putting two boxes on the counter while you find your
    wallet. Stock is already taken off the shelf; the price cannot move.
    A hold either becomes an order, is released, or expires by itself.
    """
    __tablename__ = "holds"
    id             = Column(String, primary_key=True)
    trace_id       = Column(String, index=True, nullable=False)
    lines          = Column(JSON, nullable=False)   # priced at hold time
    items_paise    = Column(Integer, nullable=False)
    delivery_paise = Column(Integer, nullable=False)
    total_paise    = Column(Integer, nullable=False)
    status         = Column(String, default="active")  # active|consumed|released|expired
    expires_at     = Column(DateTime, nullable=False)
    created_at     = Column(DateTime, default=datetime.utcnow)

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
    # HMAC over the mandate's terms. Proves the row was not edited after
    # it was issued. EVERY mandate has one, passkey-approved included.
    signature          = Column(String, nullable=False)
    # WebAuthn assertion, when a human approved this on their own device.
    # Proves who said yes; says nothing about whether the terms still match.
    passkey_signature  = Column(String, nullable=True)
    status             = Column(String, default="active")
    parent_mandate_id  = Column(String, nullable=True)
    customer_id        = Column(String, nullable=True)

class Order(Base):
    __tablename__ = "orders"
    id                = Column(String, primary_key=True)
    hold_id           = Column(String, nullable=True)
    idempotency_key   = Column(String, index=True, nullable=True)
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