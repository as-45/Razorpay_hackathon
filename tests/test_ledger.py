"""The chain either adds up or it does not.

Every test here is an attack. The chain is a claim about what a user
authorised and what was spent against it; these are the ways someone
could try to make that claim say something else.

Nothing in this file needs a server, a database, or a network.
"""
import copy
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from merchant import ledger as L

CATS = ["sweets"]
EXPIRES = "2026-09-20T00:00:00Z"


# ────────────────────────────────────────────────────────── fixtures ──

class Party:
    """Somebody who can sign: a key, an id, and a role."""

    def __init__(self, kid, role):
        self.kid, self.role = kid, role
        self.priv, self.pub = L.new_ed25519_keypair()

    @property
    def spec(self):
        return {"alg": "Ed25519", "kid": self.kid, "pub": self.pub}

    def sign(self, entry, role=None):
        return L.sign(entry, role or self.role, self.kid, self.priv)


@pytest.fixture
def user():
    return Party("usr_athreya#1", "user")


@pytest.fixture
def agent():
    return Party("agt_local#1", "agent")


@pytest.fixture
def merchant():
    return Party("sweets#k1", "merchant")


def make_grant(user, agent, merchant, cap=200000, step_up=None,
               categories=CATS, at="2026-09-13T09:00:00Z"):
    body = L.grant_body(
        holder="agt_shopper_01", cap_paise=cap, categories=categories,
        expires_at=EXPIRES, user_key=user.spec, holder_key=agent.spec,
        merchant_keys=[merchant.spec], step_up=step_up)
    e = L.build("mdt_test", 0, None, L.GRANT, body, at=at)
    return user.sign(e)


def add_spend(chain, agent, merchant, amount, order_id="ord_1",
              categories=CATS, at="2026-09-13T10:00:00Z", user=None):
    body = L.spend_body(order_id, amount, "sweets.example", categories,
                        basket_hash=L.basket_hash([{"id": "sw_001", "qty": 2}]),
                        quote_hash="sha256:77ab", hold_id="hld_1")
    e = L.build("mdt_test", len(chain), L.head(chain), L.SPEND, body, at=at)
    # Order matters not at all -- everyone signs the same bytes.
    agent.sign(e)
    merchant.sign(e)
    if user:
        user.sign(e)
    chain.append(e)
    return e


@pytest.fixture
def chain(user, agent, merchant):
    """A grant and one ordinary purchase. Rs 2,000 cap, Rs 1,640 spent."""
    c = [make_grant(user, agent, merchant)]
    add_spend(c, agent, merchant, 164000)
    return c


# ───────────────────────────────────────────────── the happy path ──

def test_a_good_chain_verifies_and_reports_the_balance(chain, user):
    r = L.verify(chain, user_pub=chain[0]["body"]["user_key"]["pub"])
    assert r.valid, r.reason
    assert r.cap_paise == 200000
    assert r.spent_paise == 164000
    assert r.available_paise == 36000
    assert r.state == "open"
    assert r.entries == 2


def test_the_balance_is_computed_not_stored(chain, agent, merchant):
    """Nothing anywhere holds a balance. Add a spend, the number moves."""
    before = L.verify(chain).available_paise
    add_spend(chain, agent, merchant, 20000, order_id="ord_2")
    assert L.verify(chain).available_paise == before - 20000


def test_two_parties_sign_identical_bytes(chain):
    """`sigs` sits outside the payload, which is what makes co-signing work."""
    spend = chain[1]
    payload = L.payload_bytes(spend)
    stripped = {k: spend[k] for k in L.PAYLOAD_FIELDS}
    assert L.canon(stripped) == payload
    assert "sigs" not in json.loads(payload)
    assert len(spend["sigs"]) == 2


def test_canonical_form_ignores_key_order():
    a = {"b": 2, "a": 1, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 1, "b": 2}
    assert L.canon(a) == L.canon(b)


def test_floats_are_refused():
    with pytest.raises(L.NotCanonical):
        L.canon({"amount": 1640.0})


# ─────────────────────────────────────────────────── tampering ──

def test_editing_an_amount_breaks_the_signature(chain):
    chain[1]["body"]["amount_paise"] = 104000
    r = L.verify(chain)
    assert not r.valid
    assert r.at_entry == 1
    assert "does not verify" in r.reason


def test_editing_an_amount_and_resigning_breaks_the_next_prev(
        chain, agent, merchant, user):
    """You can re-sign an entry you hold keys for. You cannot re-sign the
    one after it, because its `prev` names the entry you just changed."""
    add_spend(chain, agent, merchant, 10000, order_id="ord_2")
    chain[1]["body"]["amount_paise"] = 100
    chain[1]["sigs"] = []
    agent.sign(chain[1])
    merchant.sign(chain[1])
    r = L.verify(chain)
    assert not r.valid
    assert r.at_entry == 2
    assert "history has been edited" in r.reason


def test_deleting_an_entry_is_caught(chain, agent, merchant):
    add_spend(chain, agent, merchant, 10000, order_id="ord_2")
    del chain[1]
    r = L.verify(chain)
    assert not r.valid
    assert "removed or reordered" in r.reason


def test_reordering_is_caught(chain, agent, merchant):
    add_spend(chain, agent, merchant, 10000, order_id="ord_2")
    chain[1], chain[2] = chain[2], chain[1]
    assert not L.verify(chain).valid


def test_an_entry_appended_by_a_stranger_is_caught(chain, merchant):
    stranger = Party("attacker#1", "agent")
    body = L.spend_body("ord_x", 5000, "sweets.example", CATS)
    e = L.build("mdt_test", 2, L.head(chain), L.SPEND, body,
                at="2026-09-13T11:00:00Z")
    stranger.sign(e)
    merchant.sign(e)
    chain.append(e)
    r = L.verify(chain)
    assert not r.valid
    assert "unknown key" in r.reason


# ──────────────────────────────────────────────────── authority ──

def test_a_merchant_cannot_open_a_mandate(user, agent, merchant):
    """Only a user signature creates authority out of nothing."""
    body = L.grant_body("agt_1", 999999, CATS, EXPIRES,
                        user_key=user.spec, holder_key=agent.spec,
                        merchant_keys=[merchant.spec])
    e = L.build("mdt_test", 0, None, L.GRANT, body, at="2026-09-13T09:00:00Z")
    merchant.sign(e)
    r = L.verify([e])
    assert not r.valid
    assert "missing signature from user" in r.reason


def test_a_spend_needs_the_merchant_too(chain, agent, merchant, user):
    """A stolen grant is a licence to ask, not to take."""
    body = L.spend_body("ord_2", 1000, "sweets.example", CATS)
    e = L.build("mdt_test", 2, L.head(chain), L.SPEND, body,
                at="2026-09-13T11:00:00Z")
    agent.sign(e)
    chain.append(e)
    r = L.verify(chain)
    assert not r.valid
    assert "missing signature from merchant" in r.reason


def test_a_merchant_cannot_revoke(chain, merchant):
    e = L.build("mdt_test", 2, L.head(chain), L.REVOKE,
                {"reason": "because I said so", "scope": "self"},
                at="2026-09-13T11:00:00Z")
    merchant.sign(e)
    chain.append(e)
    r = L.verify(chain)
    assert not r.valid
    assert "missing signature from user" in r.reason


def test_the_anchor_must_be_the_expected_user(chain):
    other = Party("someone_else#1", "user")
    r = L.verify(chain, user_pub=other.pub)
    assert not r.valid
    assert r.at_entry == 0
    assert "expected user key" in r.reason


# ───────────────────────────────────────────────── the arithmetic ──

def test_overspending_makes_the_chain_invalid(chain, agent, merchant):
    """Not a policy violation to investigate later -- invalid arithmetic."""
    add_spend(chain, agent, merchant, 50000, order_id="ord_2")
    r = L.verify(chain)
    assert not r.valid
    assert "overdraw" in r.reason


def test_a_refund_gives_the_budget_back(chain, merchant):
    e = L.build("mdt_test", 2, L.head(chain), L.REFUND,
                {"order_id": "ord_1", "amount_paise": 64000,
                 "currency": "INR", "merchant": "sweets.example",
                 "reason": "payment_expired"},
                at="2026-09-13T12:00:00Z")
    merchant.sign(e)
    chain.append(e)
    r = L.verify(chain)
    assert r.valid, r.reason
    assert r.available_paise == 100000
    assert r.spent_paise == 100000


def test_nothing_may_follow_a_revoke(chain, user, agent, merchant):
    e = L.build("mdt_test", 2, L.head(chain), L.REVOKE,
                {"reason": "done", "scope": "self"}, at="2026-09-13T12:00:00Z")
    user.sign(e)
    chain.append(e)
    assert L.verify(chain).state == "revoked"
    assert L.verify(chain).available_paise == 0

    add_spend(chain, agent, merchant, 100, order_id="ord_3",
              at="2026-09-13T13:00:00Z")
    r = L.verify(chain)
    assert not r.valid
    assert "follows a REVOKE" in r.reason


def test_timestamps_may_not_run_backwards(chain, agent, merchant):
    add_spend(chain, agent, merchant, 100, order_id="ord_2",
              at="2026-09-13T08:00:00Z")
    r = L.verify(chain)
    assert not r.valid
    assert "backwards" in r.reason


def test_spending_after_expiry_is_caught(chain, agent, merchant):
    add_spend(chain, agent, merchant, 100, order_id="ord_2",
              at="2026-10-01T00:00:00Z")
    r = L.verify(chain)
    assert not r.valid
    assert "expired" in r.reason


def test_microseconds_do_not_decide_whether_a_purchase_is_expired(
        user, agent, merchant):
    """Found in a live response, not by a test.

    The merchant wrote expires_at with microseconds and entries without
    them. Compared as text, "…13:14:55Z" sorts AFTER "…13:14:55.748130Z"
    because 'Z' is 90 and '.' is 46 -- so a purchase made 0.74 seconds
    BEFORE expiry was judged expired. Timestamps are parsed now.
    """
    c = [make_grant(user, agent, merchant)]
    c[0]["sigs"] = []
    c[0]["body"]["expires_at"] = "2026-09-20T13:14:55.748130Z"
    user.sign(c[0])
    add_spend(c, agent, merchant, 1000, at="2026-09-20T13:14:55Z")
    r = L.verify(c)
    assert r.valid, r.reason


def test_an_entry_after_expiry_is_still_caught_with_mixed_formats(
        user, agent, merchant):
    c = [make_grant(user, agent, merchant)]
    c[0]["sigs"] = []
    c[0]["body"]["expires_at"] = "2026-09-20T13:14:55.748130Z"
    user.sign(c[0])
    add_spend(c, agent, merchant, 1000, at="2026-09-20T13:14:56Z")
    assert "expired" in L.verify(c).reason


def test_timestamps_compare_by_time_not_by_text(user, agent, merchant):
    """Same instant, two spellings, and one offset form. All equivalent."""
    assert (L.parse_ts("2026-09-20T13:14:55Z")
            == L.parse_ts("2026-09-20T13:14:55+00:00"))
    assert (L.parse_ts("2026-09-20T13:14:55.000000Z")
            == L.parse_ts("2026-09-20T13:14:55Z"))
    assert (L.parse_ts("2026-09-20T13:14:55Z")
            < L.parse_ts("2026-09-20T13:14:55.748130Z"))
    assert L.parse_ts("not a time") is None
    assert L.parse_ts(None) is None


def test_a_nonsense_timestamp_is_refused(chain, agent, merchant):
    add_spend(chain, agent, merchant, 100, order_id="ord_2",
              at="whenever, honestly")
    assert "not a valid ISO-8601" in L.verify(chain).reason


def test_a_category_outside_the_grant_is_caught(chain, agent, merchant):
    add_spend(chain, agent, merchant, 100, order_id="ord_2",
              categories=["electronics"])
    r = L.verify(chain)
    assert not r.valid
    assert "outside the grant's allowlist" in r.reason


def test_a_merchant_cannot_refund_money_that_was_never_spent(chain, merchant):
    """Found by ledger_demo.py, not by a test.

    Guarding only the floor lets a merchant sign a REFUND for any sum it
    likes and lift the balance above the cap the user signed. Money may
    only come back to a place it actually went.
    """
    e = L.build("mdt_test", 2, L.head(chain), L.REFUND,
                {"order_id": "ord_imaginary", "amount_paise": 500000,
                 "currency": "INR", "merchant": "sweets.example",
                 "reason": "creative accounting"},
                at="2026-09-13T12:00:00Z")
    merchant.sign(e)
    chain.append(e)
    r = L.verify(chain)
    assert not r.valid
    assert "never spent on this chain" in r.reason


def test_refunds_cannot_exceed_what_the_order_cost(chain, merchant):
    e = L.build("mdt_test", 2, L.head(chain), L.REFUND,
                {"order_id": "ord_1", "amount_paise": 164001,
                 "currency": "INR", "merchant": "sweets.example",
                 "reason": "rounding, honest"},
                at="2026-09-13T12:00:00Z")
    merchant.sign(e)
    chain.append(e)
    assert "would exceed what was spent" in L.verify(chain).reason


def test_refunds_cannot_be_stacked_to_exceed_the_order(chain, merchant):
    for n, amount in enumerate([100000, 100000], start=2):
        e = L.build("mdt_test", n, L.head(chain), L.REFUND,
                    {"order_id": "ord_1", "amount_paise": amount,
                     "currency": "INR", "merchant": "sweets.example",
                     "reason": "twice is nice"},
                    at="2026-09-13T12:00:00Z")
        merchant.sign(e)
        chain.append(e)
    assert "would exceed what was spent" in L.verify(chain).reason


def test_the_same_order_cannot_be_charged_twice(chain, agent, merchant):
    add_spend(chain, agent, merchant, 1000, order_id="ord_1",
              at="2026-09-13T11:00:00Z")
    assert "already on the chain" in L.verify(chain).reason


def test_a_negative_spend_is_not_a_deposit(chain, agent, merchant):
    add_spend(chain, agent, merchant, -50000, order_id="ord_2")
    r = L.verify(chain)
    assert not r.valid
    assert "non-negative integer" in r.reason


def test_the_balance_can_never_rise_above_the_cap(chain):
    r = L.verify(chain)
    assert r.available_paise <= r.cap_paise


# ─────────────────────────────────────────────────── delegation ──

def test_delegation_debits_the_parent_immediately(chain, agent):
    child = Party("agt_gift#1", "child")
    e = L.build("mdt_test", 2, L.head(chain), L.DELEGATE,
                {"child_mandate_id": "mdt_child", "child_holder": "agt_gift",
                 "child_holder_key": child.spec, "cap_paise": 30000,
                 "categories": CATS, "expires_at": "2026-09-15T00:00:00Z"},
                at="2026-09-13T11:00:00Z")
    agent.sign(e)
    chain.append(e)
    r = L.verify(chain)
    assert r.valid, r.reason
    assert r.delegated_paise == 30000
    assert r.available_paise == 6000          # 200000 - 164000 - 30000


def test_an_agent_cannot_delegate_more_than_it_holds(chain, agent):
    child = Party("agt_gift#1", "child")
    e = L.build("mdt_test", 2, L.head(chain), L.DELEGATE,
                {"child_mandate_id": "mdt_child", "child_holder": "agt_gift",
                 "child_holder_key": child.spec, "cap_paise": 90000,
                 "categories": CATS, "expires_at": "2026-09-15T00:00:00Z"},
                at="2026-09-13T11:00:00Z")
    agent.sign(e)
    chain.append(e)
    r = L.verify(chain)
    assert not r.valid
    assert "overdraw" in r.reason


def test_a_delegation_cannot_widen_the_allowlist(chain, agent):
    child = Party("agt_gift#1", "child")
    e = L.build("mdt_test", 2, L.head(chain), L.DELEGATE,
                {"child_mandate_id": "mdt_child", "child_holder": "agt_gift",
                 "child_holder_key": child.spec, "cap_paise": 1000,
                 "categories": ["sweets", "electronics"],
                 "expires_at": "2026-09-15T00:00:00Z"},
                at="2026-09-13T11:00:00Z")
    agent.sign(e)
    chain.append(e)
    r = L.verify(chain)
    assert not r.valid
    assert "widen" in r.reason


def test_a_delegation_cannot_outlive_its_parent(chain, agent):
    child = Party("agt_gift#1", "child")
    e = L.build("mdt_test", 2, L.head(chain), L.DELEGATE,
                {"child_mandate_id": "mdt_child", "child_holder": "agt_gift",
                 "child_holder_key": child.spec, "cap_paise": 1000,
                 "categories": CATS, "expires_at": "2027-01-01T00:00:00Z"},
                at="2026-09-13T11:00:00Z")
    agent.sign(e)
    chain.append(e)
    assert "outlive" in L.verify(chain).reason


def test_a_return_gives_back_the_unspent_part(chain, agent):
    child = Party("agt_gift#1", "child")
    d = L.build("mdt_test", 2, L.head(chain), L.DELEGATE,
                {"child_mandate_id": "mdt_child", "child_holder": "agt_gift",
                 "child_holder_key": child.spec, "cap_paise": 30000,
                 "categories": CATS, "expires_at": "2026-09-15T00:00:00Z"},
                at="2026-09-13T11:00:00Z")
    agent.sign(d)
    chain.append(d)

    r = L.build("mdt_test", 3, L.head(chain), L.RETURN,
                {"child_mandate_id": "mdt_child", "unspent_paise": 14000,
                 "child_final_hash": "sha256:abcd"},
                at="2026-09-13T12:00:00Z")
    child.sign(r)
    chain.append(r)

    out = L.verify(chain)
    assert out.valid, out.reason
    assert out.available_paise == 20000       # 6000 + 14000
    assert out.delegated_paise == 16000


def test_a_child_cannot_return_more_than_it_was_given(chain, agent):
    """The mirror of the refund hole, on the delegation side."""
    child = Party("agt_gift#1", "child")
    d = L.build("mdt_test", 2, L.head(chain), L.DELEGATE,
                {"child_mandate_id": "mdt_child", "child_holder": "agt_gift",
                 "child_holder_key": child.spec, "cap_paise": 30000,
                 "categories": CATS, "expires_at": "2026-09-15T00:00:00Z"},
                at="2026-09-13T11:00:00Z")
    agent.sign(d)
    chain.append(d)

    r = L.build("mdt_test", 3, L.head(chain), L.RETURN,
                {"child_mandate_id": "mdt_child", "unspent_paise": 90000,
                 "child_final_hash": "sha256:abcd"},
                at="2026-09-13T12:00:00Z")
    child.sign(r)
    chain.append(r)
    assert "cannot return more than it was given" in L.verify(chain).reason


def test_a_return_for_a_child_that_never_existed_is_caught(chain, agent):
    ghost = Party("agt_ghost#1", "child")
    r = L.build("mdt_test", 2, L.head(chain), L.RETURN,
                {"child_mandate_id": "mdt_nobody", "unspent_paise": 10000,
                 "child_final_hash": "sha256:abcd"},
                at="2026-09-13T12:00:00Z")
    ghost.sign(r)
    chain.append(r)
    r2 = L.verify(chain)
    assert not r2.valid
    # the ghost's key is not on the chain at all, so it fails even earlier
    assert "unknown key" in r2.reason or "never delegated to" in r2.reason


# ───────────────────────────────────────────────────── step-up ──

STEP_UP = {"per_order_above_paise": 50000,
           "cumulative_since_approval_paise": 100000,
           "categories_always": ["gift"],
           "always_for_delegation": True}


def test_a_small_purchase_needs_no_approval(user, agent, merchant):
    c = [make_grant(user, agent, merchant, step_up=STEP_UP)]
    add_spend(c, agent, merchant, 40000)
    assert L.verify(c).valid


def test_a_large_purchase_without_approval_is_invalid(user, agent, merchant):
    c = [make_grant(user, agent, merchant, step_up=STEP_UP)]
    add_spend(c, agent, merchant, 60000)
    r = L.verify(c)
    assert not r.valid
    assert "required approval" in r.reason


def test_a_large_purchase_with_approval_is_valid(user, agent, merchant):
    c = [make_grant(user, agent, merchant, step_up=STEP_UP)]
    add_spend(c, agent, merchant, 60000, user=user)
    assert L.verify(c).valid


def test_small_purchases_that_add_up_trigger_the_rule(user, agent, merchant):
    """Four Rs 300 orders are fine. The fifth crosses Rs 1,000 cumulative."""
    c = [make_grant(user, agent, merchant, step_up=STEP_UP)]
    for i in range(3):
        add_spend(c, agent, merchant, 30000, order_id=f"ord_{i}")
        assert L.verify(c).valid, f"order {i} should have been fine"
    add_spend(c, agent, merchant, 30000, order_id="ord_4")
    r = L.verify(c)
    assert not r.valid
    assert "required approval" in r.reason


def test_an_approval_resets_the_cumulative_counter(user, agent, merchant):
    c = [make_grant(user, agent, merchant, step_up=STEP_UP)]
    for i in range(3):
        add_spend(c, agent, merchant, 30000, order_id=f"ord_{i}")
    add_spend(c, agent, merchant, 30000, order_id="ord_4", user=user)
    assert L.verify(c).valid
    add_spend(c, agent, merchant, 30000, order_id="ord_5")
    assert L.verify(c).valid


def test_a_flagged_category_always_needs_approval(user, agent, merchant):
    c = [make_grant(user, agent, merchant, step_up=STEP_UP,
                    categories=["sweets", "gift"])]
    add_spend(c, agent, merchant, 1000, categories=["gift"])
    assert "required approval" in L.verify(c).reason


def test_delegation_needs_approval_when_the_policy_says_so(user, agent, merchant):
    c = [make_grant(user, agent, merchant, step_up=STEP_UP)]
    child = Party("agt_gift#1", "child")
    e = L.build("mdt_test", 1, L.head(c), L.DELEGATE,
                {"child_mandate_id": "m2", "child_holder": "g",
                 "child_holder_key": child.spec, "cap_paise": 1000,
                 "categories": CATS, "expires_at": "2026-09-15T00:00:00Z"},
                at="2026-09-13T10:00:00Z")
    agent.sign(e)
    c.append(e)
    assert not L.verify(c).valid

    e["sigs"] = []
    agent.sign(e)
    user.sign(e)
    assert L.verify(c).valid


# ─────────────────────────────────────────── the passkey path ──

class Passkey:
    """A stand-in for the phone, so the WebAuthn path is proven before a
    phone is ever involved. It does exactly what the secure element does:
    sign authenticatorData ‖ SHA-256(clientDataJSON) with an ES256 key."""

    RP_ID = "sweets-demo.example"
    ORIGIN = "https://sweets-demo.example"

    def __init__(self, kid="cred_9f2a"):
        self.kid = kid
        self._priv = ec.generate_private_key(ec.SECP256R1())
        self.pub = L.b64u(self._priv.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo))

    @property
    def spec(self):
        return {"alg": "WebAuthn-ES256", "kid": self.kid, "pub": self.pub,
                "rp_id": self.RP_ID, "origin": self.ORIGIN}

    def sign(self, entry, uv=True, origin=None, challenge=None):
        challenge = challenge or L.b64u(
            hashlib.sha256(L.payload_bytes(entry)).digest())
        cdj = json.dumps({"type": "webauthn.get", "challenge": challenge,
                          "origin": origin or self.ORIGIN,
                          "crossOrigin": False},
                         separators=(",", ":")).encode()
        flags = 0x01 | (0x04 if uv else 0x00)
        auth_data = (hashlib.sha256(self.RP_ID.encode()).digest()
                     + bytes([flags]) + (7).to_bytes(4, "big"))
        signed = auth_data + hashlib.sha256(cdj).digest()
        der = self._priv.sign(signed, ec.ECDSA(hashes.SHA256()))
        entry.setdefault("sigs", []).append({
            "role": "user", "key_id": self.kid, "alg": "WebAuthn-ES256",
            "authenticator_data": L.b64u(auth_data),
            "client_data_json": L.b64u(cdj),
            "sig": L.b64u(der)})
        return entry


@pytest.fixture
def phone():
    return Passkey()


def phone_grant(phone, agent, merchant, step_up=None):
    body = L.grant_body("agt_shopper_01", 200000, CATS, EXPIRES,
                        user_key=phone.spec, holder_key=agent.spec,
                        merchant_keys=[merchant.spec], step_up=step_up)
    e = L.build("mdt_test", 0, None, L.GRANT, body, at="2026-09-13T09:00:00Z")
    return phone.sign(e)


def test_a_phone_signed_grant_verifies(phone, agent, merchant):
    c = [phone_grant(phone, agent, merchant)]
    r = L.verify(c, user_pub=phone.pub)
    assert r.valid, r.reason
    assert r.available_paise == 200000


def test_the_challenge_must_be_the_hash_of_this_entry(phone, agent, merchant):
    """The binding that turns a login into a signature over a document."""
    body = L.grant_body("agt_1", 200000, CATS, EXPIRES,
                        user_key=phone.spec, holder_key=agent.spec,
                        merchant_keys=[merchant.spec])
    e = L.build("mdt_test", 0, None, L.GRANT, body, at="2026-09-13T09:00:00Z")
    phone.sign(e, challenge=L.b64u(b"a challenge from somewhere else!"))
    r = L.verify([e])
    assert not r.valid
    assert "challenge is not the hash of this entry" in r.reason


def test_an_assertion_cannot_be_moved_to_another_entry(phone, agent, merchant):
    """Replay: take the signature off the grant, staple it to a spend."""
    c = [phone_grant(phone, agent, merchant)]
    stolen = copy.deepcopy(c[0]["sigs"][0])
    body = L.spend_body("ord_2", 1000, "sweets.example", CATS)
    e = L.build("mdt_test", 1, L.head(c), L.SPEND, body,
                at="2026-09-13T10:00:00Z")
    agent.sign(e)
    merchant.sign(e)
    e["sigs"].append(stolen)
    c.append(e)
    r = L.verify(c)
    assert not r.valid
    assert "challenge is not the hash of this entry" in r.reason


def test_a_signature_from_a_lookalike_site_is_rejected(phone, agent, merchant):
    body = L.grant_body("agt_1", 200000, CATS, EXPIRES,
                        user_key=phone.spec, holder_key=agent.spec,
                        merchant_keys=[merchant.spec])
    e = L.build("mdt_test", 0, None, L.GRANT, body, at="2026-09-13T09:00:00Z")
    phone.sign(e, origin="https://sweets-demo.example.evil.test")
    assert "is not" in L.verify([e]).reason


def test_an_unverified_user_is_rejected(phone, agent, merchant):
    """UV=0 means the phone was merely present, not that a human approved."""
    body = L.grant_body("agt_1", 200000, CATS, EXPIRES,
                        user_key=phone.spec, holder_key=agent.spec,
                        merchant_keys=[merchant.spec])
    e = L.build("mdt_test", 0, None, L.GRANT, body, at="2026-09-13T09:00:00Z")
    phone.sign(e, uv=False)
    assert "user verification" in L.verify([e]).reason


def test_the_phone_approves_a_step_up_purchase(phone, agent, merchant):
    """The full picture: phone grants, agent shops, phone approves the big one."""
    c = [phone_grant(phone, agent, merchant, step_up=STEP_UP)]

    add_spend(c, agent, merchant, 40000, order_id="ord_small")
    assert L.verify(c, user_pub=phone.pub).valid

    body = L.spend_body("ord_big", 60000, "sweets.example", CATS,
                        hold_id="hld_9e31")
    e = L.build("mdt_test", 2, L.head(c), L.SPEND, body,
                at="2026-09-13T11:00:00Z")
    agent.sign(e)
    merchant.sign(e)
    c.append(e)
    assert "required approval" in L.verify(c).reason      # no phone yet

    phone.sign(e)                                          # fingerprint
    r = L.verify(c, user_pub=phone.pub)
    assert r.valid, r.reason
    assert r.spent_paise == 100000
    assert r.available_paise == 100000