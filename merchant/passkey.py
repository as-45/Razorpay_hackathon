import base64, hashlib, json, uuid
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from webauthn import (generate_registration_options,
                      verify_registration_response,
                      generate_authentication_options,
                      verify_authentication_response,
                      options_to_json)
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from .db import get_db
from .models import Customer, Mandate
from .audit import log

router = APIRouter()

RP_ID  = "localhost"
ORIGIN = "http://localhost:8000"
CHALLENGES = {}          # customer_id -> challenge bytes

def b64e(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
def b64d(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def terms_payload(agent_id, cap, cats, expires_at):
    return f"{agent_id}|{cap}|{','.join(sorted(cats))}|{expires_at}"


# ---------- registration ----------

class RegisterBegin(BaseModel):
    customer_id: str

@router.post("/webauthn/register/begin")
def register_begin(req: RegisterBegin, db: Session = Depends(get_db)):
    opts = generate_registration_options(
        rp_id=RP_ID, rp_name="Sharma Sweets",
        user_id=req.customer_id.encode(), user_name=req.customer_id)
    CHALLENGES[req.customer_id] = opts.challenge
    if db.get(Customer, req.customer_id) is None:
        db.add(Customer(id=req.customer_id))
        db.commit()
    return json.loads(options_to_json(opts))


class RegisterComplete(BaseModel):
    customer_id: str
    credential: dict

@router.post("/webauthn/register/complete")
def register_complete(req: RegisterComplete, db: Session = Depends(get_db)):
    challenge = CHALLENGES.pop(req.customer_id, None)
    if challenge is None:
        raise HTTPException(400, "no_pending_challenge")

    v = verify_registration_response(
        credential=req.credential,
        expected_challenge=challenge,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN)

    c = db.get(Customer, req.customer_id)
    c.credential_id = b64e(v.credential_id)
    c.public_key    = b64e(v.credential_public_key)
    c.sign_count    = v.sign_count
    db.commit()
    return {"registered": True, "customer_id": req.customer_id}



# ---------- mandate signing ----------

class MandateBegin(BaseModel):
    customer_id: str
    agent_id: str
    max_amount_paise: int
    allowed_categories: list[str]
    valid_days: int = 7

@router.post("/webauthn/mandate/begin")
def mandate_begin(req: MandateBegin, db: Session = Depends(get_db)):
    c = db.get(Customer, req.customer_id)
    if c is None or not c.credential_id:
        raise HTTPException(400, "no_passkey_registered")

    expires = (datetime.utcnow() + timedelta(days=req.valid_days)).isoformat()
    payload = terms_payload(req.agent_id, req.max_amount_paise,
                            req.allowed_categories, expires)
    challenge = hashlib.sha256(payload.encode()).digest()

    opts = generate_authentication_options(
        rp_id=RP_ID,
        challenge=challenge,
        allow_credentials=[PublicKeyCredentialDescriptor(
            id=b64d(c.credential_id))])

    CHALLENGES[req.customer_id] = challenge
    return {"options": json.loads(options_to_json(opts)),
            "expires_at": expires}


class MandateComplete(BaseModel):
    customer_id: str
    agent_id: str
    max_amount_paise: int
    allowed_categories: list[str]
    expires_at: str
    credential: dict

@router.post("/webauthn/mandate/complete")
def mandate_complete(req: MandateComplete, db: Session = Depends(get_db)):
    c = db.get(Customer, req.customer_id)
    if c is None or not c.credential_id:
        raise HTTPException(400, "no_passkey_registered")

    # rebuild the challenge from the terms we were sent
    payload   = terms_payload(req.agent_id, req.max_amount_paise,
                              req.allowed_categories, req.expires_at)
    challenge = hashlib.sha256(payload.encode()).digest()

    if CHALLENGES.get(req.customer_id) != challenge:
        raise HTTPException(400, "terms_do_not_match_challenge")

    v = verify_authentication_response(
        credential=req.credential,
        expected_challenge=challenge,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
        credential_public_key=b64d(c.public_key),
        credential_current_sign_count=c.sign_count)

    c.sign_count = v.new_sign_count
    CHALLENGES.pop(req.customer_id, None)

    mid = f"mnd_{uuid.uuid4().hex[:10]}"
    db.add(Mandate(
        id=mid, agent_id=req.agent_id,
        max_amount_paise=req.max_amount_paise,
        allowed_categories=req.allowed_categories,
        expires_at=datetime.fromisoformat(req.expires_at),
        signature=b64e(req.credential["response"]["signature"].encode()),
        customer_id=req.customer_id))
    db.commit()

    log(db, mid, "merchant", "mandate_issued_by_passkey", "ok",
        f"{req.customer_id} approved {req.agent_id} up to "
        f"Rs {req.max_amount_paise/100:.0f}", req.max_amount_paise)

    return {"mandate_id": mid, "customer_id": req.customer_id,
            "expires_at": req.expires_at,
            "max_amount_paise": req.max_amount_paise}