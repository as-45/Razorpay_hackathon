"""The mandate ledger.

A mandate is not a permission slip, it is an account: an append-only chain
of signed entries. The grant opens it, every spend is another entry, and
the remaining budget is not stored anywhere -- it is computed by walking
the chain.

Nothing in this module touches the database, the network, FastAPI, or a
phone. It is pure functions over dicts, which is why it can be trusted:
`verify()` is the whole security argument and it fits on two screens.

Two rules govern the bytes, and both matter:

  * `sigs` sits OUTSIDE the signed payload, so the agent and the merchant
    can sign byte-identical bytes for one SPEND, one after the other.
  * `prev` covers the previous entry INCLUDING its signatures, so history
    cannot be quietly re-signed.
"""

import base64
import hashlib
import json
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

VERSION = 1

# Domain separation: these bytes prefix everything we sign, so a signature
# made here can never be replayed as a signature over something else.
DOMAIN = b"ledger.v1."

GRANT, SPEND, REFUND, DELEGATE, RETURN, REVOKE = (
    "GRANT", "SPEND", "REFUND", "DELEGATE", "RETURN", "REVOKE")

TYPES = {GRANT, SPEND, REFUND, DELEGATE, RETURN, REVOKE}

# Who is allowed to sign what. This table is the security design; the
# cryptography is just the enforcement.
REQUIRED_ROLES = {
    GRANT:    {"user"},
    SPEND:    {"agent", "merchant"},   # plus "user" when step-up fires
    REFUND:   {"merchant"},
    DELEGATE: {"agent"},
    RETURN:   {"child"},
    REVOKE:   {"user"},
}

PAYLOAD_FIELDS = ("v", "mandate_id", "seq", "prev", "type", "at", "body")


# ─────────────────────────────────────────────────────────── encoding ──

def b64u(raw: bytes) -> str:
    """base64url, no padding -- the encoding WebAuthn speaks."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class NotCanonical(ValueError):
    """Raised for values that cannot be serialised reproducibly."""


def _check_serialisable(obj, path="body"):
    """Money is integer paise. Floats are refused outright.

    Every canonicalisation bug worth having starts with a float: is it
    1640.0, 1640, or 1.64e3? Refusing them removes the whole class.
    """
    if isinstance(obj, float):
        raise NotCanonical(f"{path}: floats are not allowed, use integer paise")
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise NotCanonical(f"{path}: object keys must be strings")
            _check_serialisable(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_serialisable(v, f"{path}[{i}]")
        return
    raise NotCanonical(f"{path}: {type(obj).__name__} is not serialisable")


def canon(obj) -> bytes:
    """Canonical JSON: sorted keys, no whitespace, UTF-8.

    RFC 8785 in the shape we need. Its hard part is number formatting,
    which `_check_serialisable` sidesteps by refusing floats -- integers
    have exactly one spelling in every language.
    """
    _check_serialisable(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


# ─────────────────────────────────────────────────────── entry shapes ──

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build(mandate_id, seq, prev, type_, body, at=None):
    """An entry with no signatures yet. Sign it, then append it."""
    if type_ not in TYPES:
        raise ValueError(f"unknown entry type {type_!r}")
    return {
        "v": VERSION,
        "mandate_id": mandate_id,
        "seq": seq,
        "prev": prev,
        "type": type_,
        "at": at or now_iso(),
        "body": body,
        "sigs": [],
    }


def payload_bytes(entry) -> bytes:
    """Exactly what gets signed. Note the absence of `sigs`."""
    return canon({k: entry[k] for k in PAYLOAD_FIELDS})


def sig_input(entry) -> bytes:
    return DOMAIN + payload_bytes(entry)


def entry_hash(entry) -> str:
    """Fingerprint of the COMPLETE entry, signatures included.

    This is what the next entry's `prev` holds, which is why an old
    signature cannot be swapped out without breaking the chain.
    """
    sigs = sorted(entry.get("sigs", []),
                  key=lambda s: (s.get("role", ""), s.get("key_id", "")))
    digest = hashlib.sha256(payload_bytes(entry) + b"\n" + canon(sigs)).hexdigest()
    return "sha256:" + digest


def head(entries):
    """`prev` for the next entry: None for an empty chain."""
    return entry_hash(entries[-1]) if entries else None


# ───────────────────────────────────────────────────────────── crypto ──

def new_ed25519_keypair():
    """Returns (private_b64, public_b64). Public form is DER SPKI, which is
    the same container ES256 keys use, so the verifier loads both the same
    way."""
    priv = ed25519.Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())
    pub_der = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    return b64u(priv_raw), b64u(pub_der)


def sign(entry, role, key_id, private_b64, alg="Ed25519"):
    """Add one signature over `sig_input(entry)`. Mutates and returns entry.

    Several parties call this on the same entry. Because `sigs` is not part
    of the payload, each of them signs identical bytes.
    """
    if alg != "Ed25519":
        raise ValueError("sign() issues Ed25519 only; WebAuthn assertions "
                         "are produced by the authenticator, not here")
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(b64u_decode(private_b64))
    entry.setdefault("sigs", []).append({
        "role": role,
        "key_id": key_id,
        "alg": alg,
        "sig": b64u(priv.sign(sig_input(entry))),
    })
    return entry


def _load_pub(pub_b64):
    return serialization.load_der_public_key(b64u_decode(pub_b64))


def _verify_ed25519(sig, entry, key):
    pub = _load_pub(key["pub"])
    if not isinstance(pub, ed25519.Ed25519PublicKey):
        return "key is not Ed25519"
    try:
        pub.verify(b64u_decode(sig["sig"]), sig_input(entry))
    except (InvalidSignature, ValueError):
        return "signature does not verify"
    return None


def _verify_webauthn_es256(sig, entry, key):
    """A passkey assertion, verified as a signature over this entry.

    A passkey normally proves "this person is here" -- a login. Setting the
    challenge to SHA-256 of the payload turns the same hardware into a
    signature over that specific document, which is the whole bridge
    between WebAuthn and this ledger.
    """
    for field in ("authenticator_data", "client_data_json"):
        if field not in sig:
            return f"assertion is missing {field}"

    try:
        auth_data = b64u_decode(sig["authenticator_data"])
        cdj_raw = b64u_decode(sig["client_data_json"])
        cdj = json.loads(cdj_raw)
    except Exception:
        return "assertion is malformed"

    if cdj.get("type") != "webauthn.get":
        return f"clientData.type is {cdj.get('type')!r}, expected webauthn.get"

    expected_origin = key.get("origin")
    if expected_origin and cdj.get("origin") != expected_origin:
        return f"origin {cdj.get('origin')!r} is not {expected_origin!r}"

    # THE binding: the challenge is the hash of this entry's payload.
    want = b64u(hashlib.sha256(payload_bytes(entry)).digest())
    if (cdj.get("challenge") or "").rstrip("=") != want:
        return "challenge is not the hash of this entry"

    if len(auth_data) < 37:
        return "authenticatorData is too short"

    rp_id = key.get("rp_id")
    if rp_id and auth_data[:32] != hashlib.sha256(rp_id.encode()).digest():
        return "assertion was made for a different site"

    if not auth_data[32] & 0x04:                       # UV flag
        return "user verification flag is not set"

    pub = _load_pub(key["pub"])
    if not isinstance(pub, ec.EllipticCurvePublicKey):
        return "key is not an EC key"
    signed = auth_data + hashlib.sha256(cdj_raw).digest()
    try:
        pub.verify(b64u_decode(sig["sig"]), signed, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return "signature does not verify"
    return None


VERIFIERS = {
    "Ed25519": _verify_ed25519,
    "WebAuthn-ES256": _verify_webauthn_es256,
}


# ──────────────────────────────────────────────────────── the arithmetic ──

def money(body, field) -> int:
    """A money field, or a complaint. Integer paise, never negative.

    Without this a SPEND of -50,000 is a deposit, and the balance check
    below waves it through.
    """
    v = body.get(field)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    if v < 0:
        return None
    return v


def delta(entry) -> int:
    """What this entry does to the available balance.

    DELEGATE debits the parent's full child cap immediately, and RETURN
    gives back the unspent part. That is what makes one chain enough to
    compute one budget -- a verifier never has to go and find the child's.
    """
    t, b = entry["type"], entry["body"]
    if t == SPEND:
        return -int(b["amount_paise"])
    if t == REFUND:
        return int(b["amount_paise"])
    if t == DELEGATE:
        return -int(b["cap_paise"])
    if t == RETURN:
        return int(b["unspent_paise"])
    return 0


def step_up_required(grant_body, entry, since_approval_paise) -> bool:
    """Does the USER-SIGNED policy demand a fresh approval for this entry?

    Recomputed identically by the merchant at order time and by every
    offline verifier afterwards. That symmetry is the point: a purchase
    that should have carried a phone signature and does not makes the
    chain invalid, permanently, to anyone who checks.
    """
    rules = grant_body.get("step_up") or {}

    if entry["type"] == DELEGATE:
        return bool(rules.get("always_for_delegation"))
    if entry["type"] != SPEND:
        return False

    body = entry["body"]
    amount = int(body["amount_paise"])

    threshold = rules.get("per_order_above_paise")
    if threshold is not None and amount > int(threshold):
        return True

    cumulative = rules.get("cumulative_since_approval_paise")
    if cumulative is not None and since_approval_paise + amount > int(cumulative):
        return True

    always = set(rules.get("categories_always") or [])
    if always & set(body.get("categories") or []):
        return True

    return False


# ───────────────────────────────────────────────────────────── result ──

class Result:
    def __init__(self, valid, reason=None, at_entry=None, cap_paise=0,
                 spent_paise=0, delegated_paise=0, available_paise=0,
                 state="open", entries=0):
        self.valid = valid
        self.reason = reason
        self.at_entry = at_entry
        self.cap_paise = cap_paise
        self.spent_paise = spent_paise
        self.delegated_paise = delegated_paise
        self.available_paise = available_paise
        self.state = state
        self.entries = entries

    def __bool__(self):
        return self.valid

    def __repr__(self):
        if self.valid:
            return (f"<VALID {self.entries} entries · available "
                    f"Rs {self.available_paise / 100:,.2f} · {self.state}>")
        return f"<INVALID at entry {self.at_entry}: {self.reason}>"

    def as_dict(self):
        return {"valid": self.valid, "reason": self.reason,
                "at_entry": self.at_entry, "cap_paise": self.cap_paise,
                "spent_paise": self.spent_paise,
                "delegated_paise": self.delegated_paise,
                "available_paise": self.available_paise,
                "state": self.state, "entries": self.entries}


def _bad(reason, i, **kw):
    return Result(False, reason=reason, at_entry=i, **kw)


# ─────────────────────────────────────────────────────────── the walk ──

def verify(entries, user_pub=None, extra_keys=None):
    """Walk the chain and report what it says.

    `user_pub` is the trust anchor. The chain can prove it is internally
    consistent and that entry 0 was signed by key K; it cannot prove K is
    your key. Pass the expected public key and that gap closes. Pass None
    and everything still verifies except the anchor itself -- useful for
    the merchant, which is not the party that needs convincing.

    Keys are collected from the chain as it is walked: the GRANT declares
    the user, agent and merchant keys, and a DELEGATE declares its child's.
    A key is therefore always declared, in a signed entry, before anything
    it signed is checked.
    """
    if not entries:
        return _bad("the chain is empty", None)

    first = entries[0]
    if first.get("type") != GRANT or first.get("seq") != 0 or first.get("prev") is not None:
        return _bad("entry 0 is not a GRANT with seq 0 and prev null", 0)

    grant = first["body"]

    keys = dict(extra_keys or {})
    for spec in (grant.get("user_key"), grant.get("holder_key")):
        if spec:
            keys[spec["kid"]] = spec
    for spec in grant.get("merchant_keys") or []:
        keys[spec["kid"]] = spec

    if user_pub is not None:
        declared = (grant.get("user_key") or {}).get("pub")
        if declared != user_pub:
            return _bad("entry 0 was not signed by the expected user key", 0)

    mandate_id = first.get("mandate_id")
    cap = int(grant.get("cap_paise", 0))
    allowed_categories = set(grant.get("categories") or [])
    expires_at = grant.get("expires_at")

    available = cap
    spent = delegated = since_approval = 0
    state = "open"
    prev = None
    last_at = None

    # Money may only come back to a place it actually went. Without these,
    # a merchant can sign a REFUND for any sum it likes and a child can
    # RETURN more than it was ever handed -- both raise the balance above
    # the cap, which the overdraw check below cannot see.
    spend_by_order = {}
    refunded_by_order = {}
    cap_by_child = {}
    returned_by_child = {}

    for i, e in enumerate(entries):
        # ── shape ──
        if e.get("v") != VERSION:
            return _bad(f"unsupported entry version {e.get('v')!r}", i)
        if e.get("mandate_id") != mandate_id:
            return _bad("entry belongs to a different mandate", i)
        if e.get("type") not in TYPES:
            return _bad(f"unknown entry type {e.get('type')!r}", i)
        if e.get("seq") != i:
            return _bad(f"seq is {e.get('seq')!r}, expected {i} "
                        "-- an entry was removed or reordered", i)
        if e.get("prev") != prev:
            return _bad("prev does not match the previous entry -- "
                        "history has been edited", i)
        if i > 0 and e["type"] == GRANT:
            return _bad("a chain has exactly one GRANT, at entry 0", i)

        # ── state and time ──
        if state != "open":
            return _bad(f"entry follows a REVOKE, which closes the chain", i)
        at = e.get("at")
        if not isinstance(at, str):
            return _bad("timestamp is missing", i)
        if last_at is not None and at < last_at:
            return _bad("timestamp moves backwards", i)
        if expires_at and at > expires_at and e["type"] not in (REVOKE, REFUND, RETURN):
            return _bad("entry is dated after the grant expired", i)

        # ── signatures ──
        required = set(REQUIRED_ROLES[e["type"]])
        if step_up_required(grant, e, since_approval):
            required.add("user")

        present = {}
        for sig in e.get("sigs") or []:
            role, kid, alg = sig.get("role"), sig.get("key_id"), sig.get("alg")
            key = keys.get(kid)
            if key is None:
                return _bad(f"signature names unknown key {kid!r}", i)
            if key.get("alg") != alg:
                return _bad(f"key {kid!r} is {key.get('alg')}, signature claims {alg}", i)
            verifier = VERIFIERS.get(alg)
            if verifier is None:
                return _bad(f"unsupported algorithm {alg!r}", i)
            why = verifier(sig, e, key)
            if why:
                return _bad(f"{role} signature: {why}", i)
            present.setdefault(role, set()).add(kid)

        missing = required - set(present)
        if missing:
            if "user" in missing and e["type"] == SPEND:
                return _bad("this purchase required approval under the signed "
                            "step-up policy and carries no user signature", i)
            return _bad(f"missing signature from {', '.join(sorted(missing))}", i)

        allowed_roles = required | {"user"}          # a bonus user sig is fine
        stray = set(present) - allowed_roles
        if stray:
            return _bad(f"{', '.join(sorted(stray))} may not sign a "
                        f"{e['type']}", i)

        # ── policy and bookkeeping, per type ──
        body = e["body"]

        if e["type"] == SPEND:
            amount = money(body, "amount_paise")
            if amount is None:
                return _bad("amount_paise must be a non-negative integer", i)
            order_id = body.get("order_id")
            if not order_id:
                return _bad("a SPEND must name an order", i)
            if order_id in spend_by_order:
                return _bad(f"order {order_id!r} is already on the chain", i)
            outside = set(body.get("categories") or []) - allowed_categories
            if allowed_categories and outside:
                return _bad(f"category {', '.join(sorted(outside))} is outside "
                            "the grant's allowlist", i)
            spend_by_order[order_id] = amount

        elif e["type"] == REFUND:
            amount = money(body, "amount_paise")
            if amount is None:
                return _bad("amount_paise must be a non-negative integer", i)
            order_id = body.get("order_id")
            if order_id not in spend_by_order:
                return _bad(f"refund names order {order_id!r}, which was never "
                            "spent on this chain", i)
            already = refunded_by_order.get(order_id, 0)
            if already + amount > spend_by_order[order_id]:
                return _bad(f"refunds for order {order_id!r} would exceed what "
                            "was spent on it", i)
            refunded_by_order[order_id] = already + amount

        elif e["type"] == DELEGATE:
            child_cap = money(body, "cap_paise")
            if child_cap is None:
                return _bad("cap_paise must be a non-negative integer", i)
            child_id = body.get("child_mandate_id")
            if not child_id:
                return _bad("a DELEGATE must name a child mandate", i)
            if child_id in cap_by_child:
                return _bad(f"child {child_id!r} was already delegated to", i)
            child_cats = set(body.get("categories") or [])
            if allowed_categories and not child_cats <= allowed_categories:
                return _bad("a delegation cannot widen the category allowlist", i)
            if expires_at and (body.get("expires_at") or "") > expires_at:
                return _bad("a delegation cannot outlive its parent", i)
            child_key = body.get("child_holder_key")
            if child_key:
                keys[child_key["kid"]] = child_key
            cap_by_child[child_id] = child_cap

        elif e["type"] == RETURN:
            unspent = money(body, "unspent_paise")
            if unspent is None:
                return _bad("unspent_paise must be a non-negative integer", i)
            child_id = body.get("child_mandate_id")
            if child_id not in cap_by_child:
                return _bad(f"return names child {child_id!r}, which was never "
                            "delegated to on this chain", i)
            already = returned_by_child.get(child_id, 0)
            if already + unspent > cap_by_child[child_id]:
                return _bad(f"child {child_id!r} cannot return more than it "
                            "was given", i)
            returned_by_child[child_id] = already + unspent

        # ── arithmetic ──
        # The balance is DERIVED, never accumulated: available is always
        # exactly cap minus what was spent minus what is delegated out.
        # Both bounds then fall out of one subtraction.
        if e["type"] == SPEND:
            spent += amount
            since_approval = 0 if "user" in present else since_approval + amount
        elif e["type"] == REFUND:
            spent -= amount
        elif e["type"] == DELEGATE:
            delegated += child_cap
            since_approval = 0 if "user" in present else since_approval
        elif e["type"] == RETURN:
            delegated -= unspent

        available = cap - spent - delegated
        if available < 0:
            return _bad("this entry would overdraw the mandate", i)
        if available > cap:
            return _bad("this entry would raise the balance above the cap", i)

        if e["type"] == REVOKE:
            state, available = "revoked", 0

        prev, last_at = entry_hash(e), at

    return Result(True, cap_paise=cap, spent_paise=spent,
                  delegated_paise=delegated, available_paise=available,
                  state=state, entries=len(entries))


# ───────────────────────────────────────────────── convenience bodies ──

def grant_body(holder, cap_paise, categories, expires_at,
               user_key, holder_key, merchant_keys=None, step_up=None,
               currency="INR"):
    body = {
        "holder": holder,
        "cap_paise": int(cap_paise),
        "currency": currency,
        "categories": sorted(categories),
        "expires_at": expires_at,
        "user_key": user_key,
        "holder_key": holder_key,
    }
    if merchant_keys:
        body["merchant_keys"] = merchant_keys
    if step_up:
        body["step_up"] = step_up
    return body


def spend_body(order_id, amount_paise, merchant, categories,
               basket_hash=None, quote_hash=None, hold_id=None, currency="INR"):
    body = {
        "order_id": order_id,
        "amount_paise": int(amount_paise),
        "currency": currency,
        "merchant": merchant,
        "categories": sorted(categories),
    }
    for k, v in (("basket_hash", basket_hash), ("quote_hash", quote_hash),
                 ("hold_id", hold_id)):
        if v is not None:
            body[k] = v
    return body


def basket_hash(lines):
    """A fingerprint of exactly which lines this money bought.

    Order-independent, so the agent and the merchant compute the same
    value from the same basket without agreeing on a sort first.
    """
    normalised = sorted(({"id": l["id"], "qty": int(l["qty"])} for l in lines),
                        key=lambda x: x["id"])
    return "sha256:" + hashlib.sha256(canon(normalised)).hexdigest()