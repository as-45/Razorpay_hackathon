from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

import uuid
from datetime import datetime, timedelta
from .models import Product, Mandate, Order, Hold
from .mandate import sign, verify
from . import inventory, ledger, chain

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
# How many extras the shop may offer on one basket. Small on purpose:
# an offer a human has to read is only useful if it is short.
MAX_SUGGESTIONS  = 2


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
            "suggest":  True,
            "orders":   True,
            "payments": True,
            "mandates": True,
            "audit":    True,
            "passkey_mandates": PASSKEYS,
        },
        "endpoints": {
            "catalog":       "/catalog",
            "quote":         "/quote",
            "suggest":       "/suggest",
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
        "upsell": {
            "supported": True,
            "endpoint": "/suggest",
            "max_suggestions": MAX_SUGGESTIONS,
            "policy": "The merchant declares which products pair with "
                      "which. A suggestion is an offer, never an addition: "
                      "the buyer must check it against its own mandate and "
                      "a human decides. Suggested items are priced and "
                      "authorised exactly like anything else.",
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
            "cross_sell": "string[] — product ids the merchant offers "
                          "alongside this one; see /suggest",
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
        "cross_sell": p.cross_sell or [],   # what the shop pairs with this
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

class SuggestRequest(BaseModel):
    trace_id: str
    items: list[Line]

@app.post("/suggest")
def suggest(req: SuggestRequest, db: Session = Depends(get_db)):
    """What the shop would like to sell alongside this basket.

    The merchant owns this, not the buyer's agent — a shopkeeper knows
    that gift wrap goes with a box of sweets, and until now had no way to
    say so to a machine. Nothing here is computed or personalised: these
    are pairings the merchant declared in its own catalog.

    Suggesting is not adding. The reply is an offer; whether it is taken
    is decided by the buyer's mandate and, ultimately, by a human.
    """
    in_basket = {l.id for l in req.items}
    offered, seen = [], set()

    for line in req.items:
        p = db.get(Product, line.id)
        if p is None:
            continue
        for pid in (p.cross_sell or []):
            if pid in in_basket or pid in seen:
                continue          # already buying it, or already offered
            c = db.get(Product, pid)
            if c is None or c.stock < 1:
                continue
            seen.add(pid)
            offered.append({
                "id": c.id, "name": c.name, "category": c.category,
                "price_paise": c.price_paise,
                "unit": {"sold_as": c.unit or "box",
                         "net_weight_g": c.net_weight_g},
                "because_of": p.id,
                "reason": f"often bought with {p.name}",
            })

    offered = offered[:MAX_SUGGESTIONS]
    log(db, req.trace_id, "merchant", "upsell_offered",
        "ok" if offered else "none",
        (", ".join(f"{o['name']} Rs {o['price_paise']/100:.0f}"
                   for o in offered) if offered
         else "nothing pairs with this basket"))
    return {"suggestions": offered, "max_accepted": MAX_SUGGESTIONS}


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
    # "hmac"   — a row with a signature over its terms. The original.
    # "ledger" — an append-only chain of signed entries. The balance is
    #            then computed by folding it, not read from a column.
    mode: str = "hmac"
    step_up: dict | None = None


@app.post("/mandates")
def issue_mandate(req: MandateRequest, db: Session = Depends(get_db)):
    if req.mode not in ("hmac", "ledger"):
        raise HTTPException(400, "mode must be 'hmac' or 'ledger'")

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

    out = {"mandate_id": mid, "expires_at": expires.isoformat(),
           "max_amount_paise": req.max_amount_paise, "mode": req.mode}
    if req.mode == "hmac":
        return out

    # ── ledger mode: open the account with a signed GRANT ──
    #
    # Software keys, generated here. That is a demo shortcut and it is
    # labelled as one: a real user's private key lives in a phone's secure
    # element and is never transmitted. Because the entry names its own
    # algorithm, swapping in a passkey later changes one field, not the
    # design -- see PHONE_SIGNING_FLOW.md.
    merchant_spec, merchant_priv = chain.merchant_key(db)
    user_spec, user_priv = chain.mint_key(db, "user", commit=False)
    agent_spec, agent_priv = chain.mint_key(db, "agent", commit=False)

    body = ledger.grant_body(
        holder=req.agent_id,
        cap_paise=req.max_amount_paise,
        categories=req.allowed_categories,
        # Whole seconds, matching the shape entries use. The walk parses
        # timestamps so a mismatch is harmless, but writing one format
        # everywhere keeps the chain readable by eye.
        expires_at=expires.replace(microsecond=0).isoformat() + "Z",
        user_key=user_spec, holder_key=agent_spec,
        merchant_keys=[merchant_spec],
        step_up=req.step_up)

    entry = ledger.build(mid, 0, None, ledger.GRANT, body)
    ledger.sign(entry, "user", user_spec["kid"], user_priv)

    try:
        chain.append(db, mid, entry)
    except chain.ChainError as exc:
        raise HTTPException(400, f"grant_rejected: {exc.reason}")

    log(db, mid, "merchant", "chain_opened", "ok",
        f"GRANT entry 0, cap Rs {req.max_amount_paise/100:.0f}",
        req.max_amount_paise)

    out.update({
        "chain_url": f"/mandates/{mid}/chain",
        "append_url": f"/mandates/{mid}/entries",
        "keys": {"user": user_spec, "agent": agent_spec,
                 "merchant": merchant_spec},
        "demo_only_private_keys": {"user": user_priv, "agent": agent_priv},
        "warning": ("Private keys are returned here because this merchant "
                    "generated them. A phone-held key is never transmitted; "
                    "only its public half is registered."),
    })
    return out


class EntryRequest(BaseModel):
    entry: dict


# Which entry types a caller may append here, and why the others cannot.
#
# A SPEND is the one that matters: letting anyone append one directly would
# be a way around /orders, where stock, holds, idempotency and payment are
# decided. Money leaves through the gate or it does not leave.
POSTABLE = {ledger.DELEGATE, ledger.RETURN, ledger.REVOKE}
NOT_POSTABLE = {
    ledger.GRANT:  "a chain has one GRANT, written when the mandate is issued",
    ledger.SPEND:  "a SPEND is created by POST /orders, which is where stock, "
                   "holds and payment are decided",
    ledger.REFUND: "a REFUND is issued by the merchant against an order",
}


@app.post("/mandates/{mandate_id}/entries")
def append_entry(mandate_id: str, req: EntryRequest,
                 db: Session = Depends(get_db)):
    """Append one pre-signed DELEGATE, RETURN or REVOKE.

    The merchant does not sign on the caller's behalf and does not fix up
    anything it sends. The entry is stored only if the whole chain still
    verifies with it on the end -- so every refusal here is the same
    refusal an offline verifier would give later, in the same words.
    """
    if db.get(Mandate, mandate_id) is None:
        raise HTTPException(404, "unknown_mandate")
    if not chain.exists(db, mandate_id):
        raise HTTPException(409, "mandate_is_not_ledger_backed")

    kind = req.entry.get("type")
    if kind in NOT_POSTABLE:
        raise HTTPException(422, {"error": "entry_type_not_postable",
                                  "reason": NOT_POSTABLE[kind]})
    if kind not in POSTABLE:
        raise HTTPException(422, {"error": "entry_rejected",
                                  "reason": f"unknown entry type {kind!r}"})

    try:
        row = chain.append(db, mandate_id, req.entry)
    except chain.ChainError as exc:
        log(db, mandate_id, "merchant", "entry_refused", "refused", exc.reason)
        raise HTTPException(422, {"error": "entry_rejected",
                                  "reason": exc.reason,
                                  "at_entry": exc.at_entry})

    result = chain.state(db, mandate_id)
    log(db, mandate_id, "merchant", "entry_appended", "ok",
        f"{row.entry_type} at seq {row.seq}")
    return {"mandate_id": mandate_id, "seq": row.seq,
            "type": row.entry_type, "entry_hash": row.entry_hash,
            "available_paise": result.available_paise,
            "state": result.state}


@app.get("/mandates/{mandate_id}/chain")
def read_chain(mandate_id: str, db: Session = Depends(get_db)):
    """The raw signed entries.

    Not a balance this server computed and asks you to believe -- the
    evidence, so you can compute it yourself. `python verify_chain.py`
    against this output needs nothing from here but the bytes.
    """
    if db.get(Mandate, mandate_id) is None:
        raise HTTPException(404, "unknown_mandate")
    entries = chain.load(db, mandate_id)
    if not entries:
        raise HTTPException(409, "mandate_is_not_ledger_backed")
    result = ledger.verify(entries)
    return {"mandate_id": mandate_id, "entries": entries,
            "verification": result.as_dict()}


class OrderRequest(BaseModel):
    trace_id: str
    mandate_id: str
    items: list[Line] | None = None   # either send items...
    hold_id: str | None = None        # ...or a hold you already took
    # Ledger mandates only: the SPEND entry, already signed by the agent
    # (and by the user, when the grant's step-up policy asks for it). The
    # merchant adds its own signature and appends -- it never signs on the
    # buyer's behalf.
    spend_entry: dict | None = None


def _refusal_code(reason):
    """Turn the walk's sentence into the refusal code callers already know.

    The wording stays as the reason; only the code is translated, so the
    existing agent and the existing tests keep seeing category_blocked and
    envelope_exhausted rather than a new vocabulary.
    """
    r = (reason or "").lower()
    if "allowlist" in r:                              return "category_blocked"
    if "overdraw" in r or "above the cap" in r:       return "envelope_exhausted"
    if "expired" in r:                                return "mandate_expired"
    if "revoke" in r:                                 return "mandate_revoked"
    if "step-up" in r:                                return "approval_required"
    if "signature" in r:                              return "bad_signature"
    return "chain_invalid"


def authorise_on_chain(db, m, req, lines, total, categories, refuse):
    """Check a purchase against the mandate's chain and sign the entry.

    Where the HMAC path asks the database three questions, this asks the
    chain one: would the chain still verify with this SPEND on the end?
    Cap, envelope, categories, expiry, revocation and the step-up policy
    are all answered by that single question, in the same code an outsider
    runs offline.

    Returns the entry with the merchant's signature added, ready to append.
    """
    entry = req.spend_entry
    if not entry:
        refuse("spend_entry_required",
               "this mandate is ledger-backed; POST /orders needs a SPEND "
               "entry signed by the agent", total)

    if entry.get("type") != ledger.SPEND:
        refuse("not_a_spend", f"entry type is {entry.get('type')!r}", total)
    if entry.get("mandate_id") != m.id:
        refuse("wrong_mandate", "entry names a different mandate", total)

    prev, seq = chain.head(db, m.id)
    if entry.get("seq") != seq or entry.get("prev") != prev:
        refuse("stale_entry",
               f"entry is built on position {entry.get('seq')}, but the chain "
               f"is at {seq} -- fetch the chain again and re-sign", total)

    body = entry.get("body") or {}

    # The agent signed an amount. The merchant computed one. If they differ,
    # nobody gets to decide which is right -- the order simply does not
    # happen. This is the merchant-deviation check, made cryptographic:
    # a shop cannot reprice between the quote and the charge, because the
    # signature it needs covers the old number.
    if body.get("amount_paise") != total:
        refuse("amount_mismatch",
               f"entry says Rs {(body.get('amount_paise') or 0)/100:.0f}, "
               f"this basket costs Rs {total/100:.0f}", total)

    # The categories in the entry are what the walk checks against the
    # allowlist, so they have to be the basket's real ones. Otherwise an
    # agent could buy electronics while writing "sweets" on the receipt.
    if set(body.get("categories") or []) != set(categories):
        refuse("category_mismatch",
               f"entry declares {sorted(body.get('categories') or [])}, "
               f"basket is {sorted(categories)}", total)

    if (body.get("hold_id") or None) != (req.hold_id or None):
        refuse("hold_mismatch", "entry names a different hold", total)

    order_id = body.get("order_id")
    if not isinstance(order_id, str) or not 1 <= len(order_id) <= 64:
        refuse("bad_order_id", "the entry must name an order id", total)
    if db.get(Order, order_id) is not None:
        refuse("duplicate_order", f"order {order_id} already exists", total)

    # Countersign FIRST, then dry-run. A SPEND requires the merchant's
    # signature, so verifying before adding it always fails with "missing
    # signature from merchant" -- which would hide every real reason
    # behind a misleading one.
    merchant_spec, merchant_priv = chain.merchant_key(db)
    signed = dict(entry)
    signed["sigs"] = list(entry.get("sigs") or [])
    ledger.sign(signed, "merchant", merchant_spec["kid"], merchant_priv)

    # Would the chain still verify with this on the end?
    #
    # This is an early exit, not the guard: chain.append re-runs the same
    # walk inside the transaction, and that is what actually makes a bad
    # entry unstorable. Deleting these lines changes no outcome -- a
    # mutation test confirmed every case still refuses, with the same code.
    # It earns its place by refusing before stock moves, so a doomed order
    # never reserves and releases a box another buyer wanted in between.
    trial = ledger.verify(chain.load(db, m.id) + [signed])
    if not trial.valid:
        refuse(_refusal_code(trial.reason), trial.reason, total)

    return signed


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
    #
    # Two paths, one decision. An HMAC mandate is checked against the
    # database; a ledger-backed one is checked by folding its chain. The
    # ledger path is authoritative for mandates that have one -- there is
    # no fallback to the SQL sum, because a second opinion is exactly what
    # a tamper-evident record must not have.
    on_chain = chain.exists(db, m.id)
    signed_entry = None

    if on_chain:
        signed_entry = authorise_on_chain(db, m, req, lines, total,
                                          categories, refuse)
        spent = chain.state(db, m.id).spent_paise
    else:
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

    # On a ledger mandate the buyer named the order when it signed the
    # entry, so the id comes from there. The signature covers that id,
    # which is what ties the money in the chain to this exact order.
    oid = (signed_entry["body"]["order_id"] if signed_entry
           else f"ord_{uuid.uuid4().hex[:10]}")

    db.add(Order(id=oid, mandate_id=m.id, trace_id=req.trace_id,
                 items=lines, total_paise=total,
                 hold_id=hold.id if hold else None,
                 idempotency_key=idempotency_key))
    if hold is not None:
        hold.status = "consumed"

    # One transaction: the order exists, the hold is spent, and the ledger
    # records it -- or none of those things happened. An order without its
    # entry would be money spent with no record, which is the single
    # failure this whole design exists to prevent.
    if signed_entry is not None:
        try:
            chain.append(db, m.id, signed_entry, commit=False)
        except chain.ChainError as exc:
            db.rollback()
            if hold is None:
                inventory.release(
                    db, [{"id": l["id"], "qty": l["qty"]} for l in lines])
            refuse(_refusal_code(exc.reason), exc.reason, total)

    db.commit()
    log(db, req.trace_id, "merchant", "order_created", "ok", oid, total)

    out = {"order_id": oid, "total_paise": total, "lines": lines,
           "reused": False}
    if signed_entry is not None:
        after = chain.state(db, m.id)
        out.update({"entry_seq": signed_entry["seq"],
                    "entry_hash": ledger.entry_hash(signed_entry),
                    "remaining_paise": after.available_paise})
    return out



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
    real mandate, so a line can only be added to a purchase that someone
    was actually authorised to attempt, and label the actor accordingly so
    a reader can tell merchant fact from agent claim.
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

    out = {"mandate_id": m.id, "max_amount_paise": m.max_amount_paise,
           "allowed_categories": m.allowed_categories,
           "expires_at": m.expires_at.isoformat(), "status": m.status}

    if not chain.exists(db, m.id):
        spent = mandate_spent(db, m.id)
        out.update({"mode": "hmac", "spent_paise": spent,
                    "remaining_paise": max(0, m.max_amount_paise - spent)})
        return out

    # Ledger-backed: these numbers are folded from signed entries, and the
    # caller can check them by fetching the chain and folding it too.
    r = chain.state(db, m.id)
    out.update({
        "mode": "ledger",
        "chain_url": f"/mandates/{m.id}/chain",
        "chain_valid": r.valid,
        "chain_reason": r.reason,
        "entries": r.entries,
        "spent_paise": r.spent_paise,
        "delegated_paise": r.delegated_paise,
        "remaining_paise": r.available_paise,
        "status": r.state if r.valid else m.status,
    })
    return out