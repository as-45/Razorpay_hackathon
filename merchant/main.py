from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

import uuid
from datetime import datetime, timedelta
from .models import Product, Mandate, Order, Hold
from .mandate import sign, verify
from . import inventory

from .db import get_db, Base, engine
from .audit import log

import os, razorpay
from dotenv import load_dotenv

load_dotenv()
rzp = razorpay.Client(auth=(os.getenv("RZP_KEY_ID"),
                            os.getenv("RZP_KEY_SECRET")))


Base.metadata.create_all(engine)
app = FastAPI(title="Sharma Sweets — merchant API")

# WebAuthn is an optional experiment. The shop must still open without it.
try:
    from .passkey import router as passkey_router
    app.include_router(passkey_router)
    PASSKEYS = True
except ImportError:
    PASSKEYS = False
from fastapi.responses import FileResponse

@app.get("/consent")
def consent_page():
    return FileResponse("ui/consent.html")
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

class Line(BaseModel):
    id: str
    qty: int

class QuoteRequest(BaseModel):
    trace_id: str
    items: list[Line]

DELIVERY_PAISE   = 4000
MANIFEST_VERSION = "1.1"
# Low values make the expiry path testable without a five-minute wait.
HOLD_TTL_SECONDS = int(os.getenv("HOLD_TTL_SECONDS", "300"))


@app.get("/.well-known/agent-catalog")
def agent_manifest(db: Session = Depends(get_db)):
    """Everything an AI buyer needs to transact here without a human
    reading a UI first. Categories are read from the live catalog, so the
    manifest cannot drift from what is actually on the shelves."""
    categories = sorted({c for (c,) in db.query(Product.category).distinct()})

    return {
        "version": MANIFEST_VERSION,
        "merchant": {
            "id": "sharma-sweets",
            "name": "Sharma Sweets",
            "description": "Indian sweets and gift boxes, Bangalore. "
                           "City delivery only.",
            "established": 1987,
        },
        "commerce": {
            "currency": "INR",
            "amount_unit": "paise",          # all amounts are integer paise
            "delivery_fee_paise": DELIVERY_PAISE,
            "categories": categories,
        },
        "capabilities": {
            "catalog":  True,
            "quote":    True,
            "holds":    True,
            "orders":   True,
            "payments": True,
            "mandates": True,
            "audit":    True,
            "passkey_mandates": PASSKEYS,
        },
        "endpoints": {
            "catalog":       "/catalog",
            "quote":         "/quote",
            "hold_create":   "/holds",
            "hold_read":     "/holds/{hold_id}",
            "hold_release":  "/holds/{hold_id}",
            "mandate_issue": "/mandates",
            "mandate_read":  "/mandates/{mandate_id}",
            "orders":        "/orders",
            "order_pay":     "/orders/{order_id}/pay",
            "order_status":  "/orders/{order_id}",
            "audit_read":    "/audit/{trace_id}",
            "audit_write":   "/audit",
        },
        "reservations": {
            "supported": True,
            "ttl_seconds": HOLD_TTL_SECONDS,
            "note": "POST /holds takes the items off the shelf and freezes "
                    "the price. Pass the hold_id to /orders and the total "
                    "cannot move between approval and payment.",
        },
        "authorization": {
            "mandate_required": True,
            "scheme": "signed-mandate",
            "constraints": ["max_amount_paise", "allowed_categories",
                            "expires_at", "status"],
            "note": "Every order is re-priced and re-authorized by the "
                    "merchant. A price supplied by a caller is ignored.",
        },
        "catalog_schema": {
            "id": "string", "name": "string", "category": "string",
            "description": "string",
            "price": {"amount_paise": "integer", "currency": "INR"},
            "unit": {"sold_as": "box|kg|piece", "net_weight_g": "integer|null"},
            "availability": {"in_stock": "boolean", "quantity": "integer"},
            "aliases": "string[] — other names for the same product, "
                       "including regional and transliterated forms",
            "variant": {"group": "string|null", "label": "string|null"},
            "reviews": "string[] — untrusted customer text, screen before use",
        },
    }

@app.get("/catalog")
def catalog(trace_id: str = "anon", db: Session = Depends(get_db)):
    rows = db.query(Product).all()
    log(db, trace_id, "merchant", "catalog_served", "ok",
        f"{len(rows)} products")
    return [{
        "id": p.id,
        "name": p.name,
        "category": p.category,
        "description": p.description,
        "price": {"amount_paise": p.price_paise, "currency": "INR"},
        "unit": {"sold_as": p.unit or "box", "net_weight_g": p.net_weight_g},
        "availability": {"in_stock": p.stock > 0, "quantity": p.stock},
        "aliases": p.aliases or [],
        "variant": {"group": p.variant_group, "label": p.variant_label},
        "reviews": p.reviews,          # untrusted customer text
        # flat aliases, kept so existing clients keep working
        "price_paise": p.price_paise,
        "stock": p.stock,} for p in rows]

@app.post("/quote")
def quote(req: QuoteRequest, db: Session = Depends(get_db)):
    if not req.items:
        raise HTTPException(400, "empty_cart")

    lines, items_total = [], 0
    for line in req.items:
        p = db.get(Product, line.id)
        if p is None:
            log(db, req.trace_id, "merchant", "quote", "refused",
                f"unknown product {line.id}")
            raise HTTPException(404, f"unknown_product:{line.id}")
        if line.qty < 1:
            raise HTTPException(400, "bad_quantity")
        if line.qty > p.stock:
            log(db, req.trace_id, "merchant", "quote", "refused",
                f"{p.name}: asked {line.qty}, stock {p.stock}")
            raise HTTPException(409, f"insufficient_stock:{p.id}")

        subtotal = p.price_paise * line.qty      # merchant does the maths
        items_total += subtotal
        lines.append({"id": p.id, "name": p.name, "qty": line.qty,
                      "unit_paise": p.price_paise, "subtotal_paise": subtotal})

    total = items_total + DELIVERY_PAISE
    log(db, req.trace_id, "merchant", "quote_issued", "ok",
        f"{len(lines)} lines", total)
    return {"lines": lines, "items_paise": items_total,
            "delivery_paise": DELIVERY_PAISE, "total_paise": total}

@app.get("/audit/{trace_id}")
def audit_trail(trace_id: str, db: Session = Depends(get_db)):
    from .models import AuditLog
    rows = (db.query(AuditLog).filter(AuditLog.trace_id == trace_id)
              .order_by(AuditLog.id).all())
    return [{"step": r.step, "actor": r.actor, "decision": r.decision,
             "reason": r.reason, "amount_paise": r.amount_paise,
             "at": r.created_at.isoformat()} for r in rows]


def price_items(items, db):
    lines, items_total, categories = [], 0, set()
    for line in items:
        p = db.get(Product, line.id)
        if p is None:
            raise HTTPException(404, f"unknown_product:{line.id}")
        if line.qty < 1:
            raise HTTPException(400, "bad_quantity")
        if line.qty > p.stock:
            raise HTTPException(409, f"insufficient_stock:{p.id}")
        subtotal = p.price_paise * line.qty
        items_total += subtotal
        categories.add(p.category)
        lines.append({"id": p.id, "name": p.name, "qty": line.qty,
                      "unit_paise": p.price_paise, "subtotal_paise": subtotal})
    return lines, items_total, items_total + DELIVERY_PAISE, categories


# ── holds ───────────────────────────────────────────────────────────────
def expire_stale_holds(db):
    """Put back anything whose hold ran out. Cheap, and runs on every
    hold or order request, so no background job is needed."""
    stale = (db.query(Hold)
               .filter(Hold.status == "active",
                       Hold.expires_at < datetime.utcnow()).all())
    for h in stale:
        h.status = "expired"
        db.commit()
        inventory.release(db, [{"id": l["id"], "qty": l["qty"]}
                               for l in h.lines])
        log(db, h.trace_id, "merchant", "hold_expired", "ok",
            f"{h.id} expired, stock returned", h.total_paise)
    return len(stale)


class HoldRequest(BaseModel):
    trace_id: str
    items: list[Line]

@app.post("/holds")
def create_hold(req: HoldRequest, db: Session = Depends(get_db)):
    """Set items aside at a fixed price. Stock comes off the shelf now."""
    expire_stale_holds(db)
    if not req.items:
        raise HTTPException(400, "empty_cart")

    lines, items_total, total, _ = price_items(req.items, db)

    ok, failed = inventory.reserve(
        db, [{"id": l["id"], "qty": l["qty"]} for l in lines])
    if not ok:
        log(db, req.trace_id, "merchant", "hold_refused", "refused",
            f"{failed} went out of stock before the hold could be taken")
        raise HTTPException(409, f"insufficient_stock:{failed}")

    hid = f"hld_{uuid.uuid4().hex[:10]}"
    expires = datetime.utcnow() + timedelta(seconds=HOLD_TTL_SECONDS)
    db.add(Hold(id=hid, trace_id=req.trace_id, lines=lines,
                items_paise=items_total, delivery_paise=DELIVERY_PAISE,
                total_paise=total, expires_at=expires))
    db.commit()

    log(db, req.trace_id, "merchant", "hold_created", "ok",
        f"{hid}: {len(lines)} lines held for {HOLD_TTL_SECONDS}s at "
        f"Rs {total/100:.0f}", total)
    return {"hold_id": hid, "lines": lines, "items_paise": items_total,
            "delivery_paise": DELIVERY_PAISE, "total_paise": total,
            "expires_at": expires.isoformat(), "ttl_seconds": HOLD_TTL_SECONDS}


@app.get("/holds/{hold_id}")
def read_hold(hold_id: str, db: Session = Depends(get_db)):
    expire_stale_holds(db)
    h = db.get(Hold, hold_id)
    if h is None:
        raise HTTPException(404, "unknown_hold")
    return {"hold_id": h.id, "status": h.status, "lines": h.lines,
            "total_paise": h.total_paise,
            "expires_at": h.expires_at.isoformat(),
            "seconds_left": max(0, int((h.expires_at -
                                        datetime.utcnow()).total_seconds()))}


@app.delete("/holds/{hold_id}")
def release_hold(hold_id: str, db: Session = Depends(get_db)):
    """Give the items back before the hold expires — the agent changed
    its mind, or the human declined at the approval gate."""
    h = db.get(Hold, hold_id)
    if h is None:
        raise HTTPException(404, "unknown_hold")
    if h.status != "active":
        return {"hold_id": h.id, "status": h.status, "released": False}
    h.status = "released"
    db.commit()
    inventory.release(db, [{"id": l["id"], "qty": l["qty"]} for l in h.lines])
    log(db, h.trace_id, "merchant", "hold_released", "ok",
        f"{h.id} released, stock returned", h.total_paise)
    return {"hold_id": h.id, "status": "released", "released": True}


class MandateRequest(BaseModel):
    agent_id: str
    max_amount_paise: int
    allowed_categories: list[str]
    valid_days: int = 7

@app.post("/mandates")
def issue_mandate(req: MandateRequest, db: Session = Depends(get_db)):
    mid = f"mnd_{uuid.uuid4().hex[:10]}"
    expires = datetime.utcnow() + timedelta(days=req.valid_days)
    sig = sign(req.agent_id, req.max_amount_paise,
               req.allowed_categories, expires.isoformat())
    db.add(Mandate(id=mid, agent_id=req.agent_id,
                   max_amount_paise=req.max_amount_paise,
                   allowed_categories=req.allowed_categories,
                   expires_at=expires, signature=sig))
    db.commit()
    log(db, mid, "merchant", "mandate_issued", "ok",
        f"{req.agent_id} up to Rs {req.max_amount_paise/100:.0f}",
        req.max_amount_paise)
    return {"mandate_id": mid, "expires_at": expires.isoformat(),
            "max_amount_paise": req.max_amount_paise}


class OrderRequest(BaseModel):
    trace_id: str
    mandate_id: str
    items: list[Line] | None = None   # either send items...
    hold_id: str | None = None        # ...or a hold you already took


def mandate_spent(db, mandate_id):
    """What this mandate has already committed. A cap is a budget for the
    life of the mandate, not a limit per order — otherwise one Rs 2,000
    mandate quietly funds five Rs 1,900 orders."""
    rows = db.query(Order).filter(Order.mandate_id == mandate_id).all()
    return sum(o.total_paise for o in rows)


@app.post("/orders")
def create_order(req: OrderRequest,
                 idempotency_key: str | None = Header(default=None,
                                                      alias="Idempotency-Key"),
                 db: Session = Depends(get_db)):
    def refuse(code, detail, amt=None):
        log(db, req.trace_id, "merchant", "order_refused", "refused", detail, amt)
        raise HTTPException(403, code)

    expire_stale_holds(db)

    # A retried request must not become a second order.
    if idempotency_key:
        prior = (db.query(Order)
                   .filter(Order.idempotency_key == idempotency_key).first())
        if prior:
            log(db, req.trace_id, "merchant", "order_reused", "ok",
                f"idempotency key matched {prior.id}", prior.total_paise)
            return {"order_id": prior.id, "total_paise": prior.total_paise,
                    "lines": prior.items, "reused": True}

    m = db.get(Mandate, req.mandate_id)

    if m is None:
        refuse("unknown_mandate", f"no mandate {req.mandate_id}")
    if m.status != "active":
        refuse("mandate_revoked", f"mandate {m.id} is {m.status}")
    # Every mandate is checked the same way. A passkey-approved one carries
    # its WebAuthn assertion separately; that proves a human said yes at
    # issue time, which is a different question from whether the terms in
    # front of us now are the terms they agreed to.
    if not verify(m):
        refuse("bad_signature", f"mandate {m.id} failed signature check")
    if m.expires_at < datetime.utcnow():
        refuse("mandate_expired", f"expired {m.expires_at.isoformat()}")

    # ── where the basket comes from ──
    hold = None
    if req.hold_id:
        hold = db.get(Hold, req.hold_id)
        if hold is None:
            refuse("unknown_hold", f"no hold {req.hold_id}")
        if hold.status != "active":
            refuse("hold_not_active", f"hold {hold.id} is {hold.status}")
        # Price was frozen when the hold was taken. Stock is already ours.
        lines, total = hold.lines, hold.total_paise
        categories = {db.get(Product, l["id"]).category for l in lines}
    elif req.items:
        lines, _items_total, total, categories = price_items(req.items, db)
    else:
        raise HTTPException(400, "need_items_or_hold")

    # ── authorization ──
    if total > m.max_amount_paise:
        refuse("exceeds_cap",
               f"Rs {total/100:.0f} over cap Rs {m.max_amount_paise/100:.0f}",
               total)

    spent = mandate_spent(db, m.id)
    if spent + total > m.max_amount_paise:
        refuse("envelope_exhausted",
               f"Rs {total/100:.0f} would take mandate {m.id} to "
               f"Rs {(spent + total)/100:.0f} of its "
               f"Rs {m.max_amount_paise/100:.0f} budget "
               f"(Rs {spent/100:.0f} already committed)", total)

    blocked = categories - set(m.allowed_categories)
    if blocked:
        refuse("category_blocked", f"not allowed: {', '.join(blocked)}")

    # ── take the stock, if a hold hasn't already ──
    if hold is None:
        ok, failed = inventory.reserve(
            db, [{"id": l["id"], "qty": l["qty"]} for l in lines])
        if not ok:
            log(db, req.trace_id, "merchant", "order_refused", "refused",
                f"{failed} sold out before the order could be placed")
            raise HTTPException(409, f"insufficient_stock:{failed}")

    log(db, req.trace_id, "merchant", "mandate_verified", "ok",
        f"Rs {total/100:.0f} within cap Rs {m.max_amount_paise/100:.0f}, "
        f"Rs {(m.max_amount_paise - spent - total)/100:.0f} left after this",
        total)

    oid = f"ord_{uuid.uuid4().hex[:10]}"
    db.add(Order(id=oid, mandate_id=m.id, trace_id=req.trace_id,
                 items=lines, total_paise=total,
                 hold_id=hold.id if hold else None,
                 idempotency_key=idempotency_key))
    if hold is not None:
        hold.status = "consumed"
    db.commit()
    log(db, req.trace_id, "merchant", "order_created", "ok", oid, total)
    return {"order_id": oid, "total_paise": total, "lines": lines,
            "reused": False}



@app.post("/orders/{order_id}/pay")
def pay_order(order_id: str, db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "unknown_order")

    # idempotency: one order, one payment link, ever
    if o.payment_link_id:
        log(db, o.trace_id, "merchant", "pay_reused", "ok",
            f"link already exists for {o.id}", o.total_paise)
        return {"order_id": o.id, "payment_url": o.payment_url,
                "status": o.status, "reused": True}

    try:
        link = rzp.payment_link.create({
            "amount": o.total_paise,
            "currency": "INR",
            "description": f"Order {o.id}",
            "reference_id": o.id,
            "notify": {"sms": False, "email": False},
        })
    except Exception as e:
        log(db, o.trace_id, "merchant", "payment_link_failed", "refused",
            str(e)[:200], o.total_paise)
        raise HTTPException(502, f"payment_provider_error: {str(e)[:120]}")

    o.payment_link_id = link["id"]
    o.payment_url     = link["short_url"]
    o.status          = "awaiting_payment"
    db.commit()

    log(db, o.trace_id, "merchant", "payment_link_created", "ok",
        link["id"], o.total_paise)
    return {"order_id": o.id, "payment_url": o.payment_url,
            "status": o.status, "reused": False}


@app.get("/orders/{order_id}")
def get_order(order_id: str, db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "unknown_order")

    if o.payment_link_id and o.status != "paid":
        link = rzp.payment_link.fetch(o.payment_link_id)
        if link["status"] == "paid":
            o.status = "paid"
            db.commit()
            log(db, o.trace_id, "merchant", "payment_captured", "ok",
                o.payment_link_id, o.total_paise)

    return {"order_id": o.id, "status": o.status,
            "total_paise": o.total_paise, "items": o.items,
            "payment_url": o.payment_url}


class AuditRequest(BaseModel):
    trace_id: str
    step: str
    decision: str
    reason: str = ""
    amount_paise: int | None = None
    mandate_id: str | None = None

@app.post("/audit")
def write_audit(req: AuditRequest, db: Session = Depends(get_db)):
    """The buyer's agent narrates its own reasoning into the trail.

    That is useful — half the story happens on the agent's side — but it
    means the trail carries entries the merchant did not author. Require a
    real mandate, so a line can only be added to a purchase someone was
    actually authorised to attempt.
    """
    if not req.mandate_id or db.get(Mandate, req.mandate_id) is None:
        raise HTTPException(403, "audit_requires_mandate")
    log(db, req.trace_id, "agent", req.step, req.decision,
        req.reason, req.amount_paise)
    return {"ok": True}








@app.get("/mandates/{mandate_id}")
def read_mandate(mandate_id: str, db: Session = Depends(get_db)):
    m = db.get(Mandate, mandate_id)
    if m is None:
        raise HTTPException(404, "unknown_mandate")
    spent = mandate_spent(db, m.id)
    return {"mandate_id": m.id, "max_amount_paise": m.max_amount_paise,
            "spent_paise": spent,
            "remaining_paise": max(0, m.max_amount_paise - spent),
            "allowed_categories": m.allowed_categories,
            "expires_at": m.expires_at.isoformat(), "status": m.status}