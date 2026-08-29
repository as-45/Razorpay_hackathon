from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import uuid
from datetime import datetime, timedelta
from .models import Product, Mandate, Order
from .mandate import sign, verify

from .db import get_db, Base, engine
from .audit import log

Base.metadata.create_all(engine)
app = FastAPI(title="Sharma Sweets — merchant API")


@app.get("/.well-known/agent-catalog")
def agent_manifest():
    return {
        "merchant": "Sharma Sweets",
        "currency": "INR",
        "amounts": "integer paise",
        "endpoints": {
            "catalog": "GET /catalog",
            "quote":   "POST /quote",
            "order":   "POST /orders   (requires mandate_id)",
            "audit":   "GET /audit/{trace_id}"
        },
        "mandate_required": True,
        "categories": ["sweets", "premium", "addons"]
    }
    
class Line(BaseModel):
    id: str
    qty: int

class QuoteRequest(BaseModel):
    trace_id: str
    items: list[Line]

DELIVERY_PAISE = 4000

@app.get("/catalog")
def catalog(trace_id: str = "anon", db: Session = Depends(get_db)):
    rows = db.query(Product).all()
    log(db, trace_id, "merchant", "catalog_served", "ok",
        f"{len(rows)} products")
    return [{"id": p.id, "name": p.name, "price_paise": p.price_paise,
             "stock": p.stock, "category": p.category,
             "description": p.description, "reviews": p.reviews}
            for p in rows]

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
    items: list[Line]

@app.post("/orders")
def create_order(req: OrderRequest, db: Session = Depends(get_db)):
    def refuse(code, detail,amt=None):
        log(db, req.trace_id, "merchant", "order_refused", "refused", detail,amt)
        raise HTTPException(403, code)

    m = db.get(Mandate, req.mandate_id)

    if m is None:
        refuse("unknown_mandate", f"no mandate {req.mandate_id}")
    if m.status != "active":
        refuse("mandate_revoked", f"mandate {m.id} is {m.status}")
    if not verify(m):
        refuse("bad_signature", f"mandate {m.id} failed signature check")
    if m.expires_at < datetime.utcnow():
        refuse("mandate_expired", f"expired {m.expires_at.isoformat()}")

    lines, items_total, total, categories = price_items(req.items, db)

    if total > m.max_amount_paise:
        refuse("exceeds_cap",
               f"Rs {total/100:.0f} over cap Rs {m.max_amount_paise/100:.0f}",total)
    blocked = categories - set(m.allowed_categories)
    if blocked:
        refuse("category_blocked", f"not allowed: {', '.join(blocked)}")

    log(db, req.trace_id, "merchant", "mandate_verified", "ok",
        f"Rs {total/100:.0f} within cap Rs {m.max_amount_paise/100:.0f}", total)

    oid = f"ord_{uuid.uuid4().hex[:10]}"
    db.add(Order(id=oid, mandate_id=m.id, trace_id=req.trace_id,
                 items=lines, total_paise=total))
    db.commit()
    log(db, req.trace_id, "merchant", "order_created", "ok", oid, total)
    return {"order_id": oid, "total_paise": total, "lines": lines}