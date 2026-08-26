from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db, Base, engine
from .models import Product
from .audit import log

Base.metadata.create_all(engine)
app = FastAPI(title="Sharma Sweets — merchant API")

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