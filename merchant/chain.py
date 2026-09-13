"""Storage for mandate chains.

`ledger.py` is the arithmetic and the cryptography and knows nothing about
databases. This module is the other half: it puts entries in rows, reads
them back, and refuses to store anything that would make the chain stop
verifying.

The rule that governs the whole file: **an entry is accepted only if the
chain still verifies with it appended.** Not "the signature looked fine" --
the whole walk, every time. Appends are rare and chains are short, so the
cost is nothing and the guarantee is total: a stored chain is a valid one.
"""

import json
import uuid

from sqlalchemy.exc import IntegrityError

from . import ledger
from .models import KeyPair, LedgerEntry


class ChainError(Exception):
    """An entry was refused. `.reason` is safe to show a caller."""

    def __init__(self, reason, at_entry=None):
        super().__init__(reason)
        self.reason = reason
        self.at_entry = at_entry


# ───────────────────────────────────────────────────────────── keys ──

def get_key(db, kid):
    row = db.get(KeyPair, kid)
    return None if row is None else _spec(row)


def _spec(row):
    spec = {"alg": row.alg, "kid": row.kid, "pub": row.public_key}
    if row.rp_id:
        spec["rp_id"] = row.rp_id
    if row.origin:
        spec["origin"] = row.origin
    return spec


def mint_key(db, role, kid=None, commit=True):
    """Generate an Ed25519 keypair, store it, return (spec, private).

    Used for the merchant's own key and, in software-key mode, for the
    agent and the demo user.
    """
    kid = kid or f"{role}_{uuid.uuid4().hex[:8]}"
    priv, pub = ledger.new_ed25519_keypair()
    db.add(KeyPair(kid=kid, role=role, alg="Ed25519",
                   public_key=pub, private_key=priv))
    if commit:
        db.commit()
    return {"alg": "Ed25519", "kid": kid, "pub": pub}, priv


def register_public_key(db, kid, role, pub, alg="Ed25519",
                        rp_id=None, origin=None, commit=True):
    """Record a key we will only ever verify against -- a phone's passkey,
    or an agent that generated its own."""
    existing = db.get(KeyPair, kid)
    if existing is not None:
        return _spec(existing)
    db.add(KeyPair(kid=kid, role=role, alg=alg, public_key=pub,
                   private_key=None, rp_id=rp_id, origin=origin))
    if commit:
        db.commit()
    return {"alg": alg, "kid": kid, "pub": pub,
            **({"rp_id": rp_id} if rp_id else {}),
            **({"origin": origin} if origin else {})}


MERCHANT_KID = "merchant#k1"


def merchant_key(db):
    """The shop's own signing key. Generated once, then reused forever.

    It lives in the database rather than an env var so a fresh clone works
    with no setup, which matters more here than key ceremony does.
    """
    row = db.get(KeyPair, MERCHANT_KID)
    if row is None:
        spec, priv = mint_key(db, "merchant", kid=MERCHANT_KID)
        return spec, priv
    return _spec(row), row.private_key


def private_key(db, kid):
    row = db.get(KeyPair, kid)
    if row is None or row.private_key is None:
        raise ChainError(f"no private key held for {kid!r}")
    return row.private_key


# ──────────────────────────────────────────────────────────── rows ──

def to_entry(row):
    """Rebuild the entry dict from the bytes we stored."""
    entry = json.loads(row.payload)
    entry["sigs"] = row.sigs
    return entry


def load(db, mandate_id):
    rows = (db.query(LedgerEntry)
            .filter(LedgerEntry.mandate_id == mandate_id)
            .order_by(LedgerEntry.seq)
            .all())
    return [to_entry(r) for r in rows]


def exists(db, mandate_id):
    """Is this mandate ledger-backed, or one of the older HMAC ones?

    Deliberately answered by looking for entries rather than by a column on
    `mandates`, so nothing existing needs a migration and both kinds of
    mandate keep working side by side.
    """
    return (db.query(LedgerEntry.id)
            .filter(LedgerEntry.mandate_id == mandate_id)
            .first() is not None)


def head(db, mandate_id):
    row = (db.query(LedgerEntry)
           .filter(LedgerEntry.mandate_id == mandate_id)
           .order_by(LedgerEntry.seq.desc())
           .first())
    return (row.entry_hash, row.seq + 1) if row else (None, 0)


def state(db, mandate_id, user_pub=None):
    return ledger.verify(load(db, mandate_id), user_pub=user_pub)


# ─────────────────────────────────────────────────────────── append ──

def append(db, mandate_id, entry, commit=True):
    """Store one entry, but only if the resulting chain verifies.

    Every reason the walk could reject it -- a bad signature, a wrong
    signer, an overdraw, a missing step-up approval -- becomes a refusal
    here, with the walk's own wording. There is no way to write an entry
    that a later reader would reject.
    """
    if entry.get("mandate_id") != mandate_id:
        raise ChainError("entry belongs to a different mandate")

    try:
        payload = ledger.payload_bytes(entry)
    except ledger.NotCanonical as exc:
        raise ChainError(str(exc)) from exc

    # We store the bytes and rebuild the entry from them later, so prove
    # here -- once, at write time -- that the round trip is lossless.
    if ledger.canon(json.loads(payload)) != payload:
        raise ChainError("entry does not round-trip through canonical form")

    chain = load(db, mandate_id)
    candidate = chain + [entry]

    result = ledger.verify(candidate)
    if not result.valid:
        raise ChainError(result.reason, at_entry=result.at_entry)

    row = LedgerEntry(
        mandate_id=mandate_id,
        seq=entry["seq"],
        entry_type=entry["type"],
        payload=payload.decode("utf-8"),
        sigs=entry.get("sigs") or [],
        entry_hash=ledger.entry_hash(entry),
        prev=entry.get("prev"),
    )
    db.add(row)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ChainError(
            f"position {entry['seq']} is already taken -- something else "
            "appended to this chain first", at_entry=entry["seq"]) from exc
    return row


def build_next(db, mandate_id, type_, body, at=None):
    """An unsigned entry at the current head, ready to be signed."""
    prev, seq = head(db, mandate_id)
    return ledger.build(mandate_id, seq, prev, type_, body, at=at)


def challenge_for(entry):
    """What a phone signs: base64url of SHA-256 over the entry's payload.

    A passkey normally proves "this person is here". Using this as the
    WebAuthn challenge turns that same prompt into a signature over this
    entry and no other.
    """
    import hashlib
    return ledger.b64u(hashlib.sha256(ledger.payload_bytes(entry)).digest())