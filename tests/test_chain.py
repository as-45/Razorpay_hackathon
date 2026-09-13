"""The chain, over HTTP.

test_ledger.py proves the arithmetic. This file proves the merchant stores
it faithfully, refuses to store anything that would stop it verifying, and
-- at the end -- that what comes back over the wire can be checked by
somebody who does not trust the server that sent it.

SPEND entries are not written here. They belong to POST /orders, where
stock, holds and payment are decided, and letting a caller append one
directly would be a way around that gate. This file tests that refusal
too.
"""
import os
import uuid

import requests

from merchant import ledger

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001")


# ────────────────────────────────────────────────────────── helpers ──

def issue(mode="ledger", cap=200000, categories=("sweets",), step_up=None):
    r = requests.post(f"{BASE}/mandates", json={
        "agent_id": f"agt_{uuid.uuid4().hex[:6]}",
        "max_amount_paise": cap,
        "allowed_categories": list(categories),
        "valid_days": 7, "mode": mode,
        **({"step_up": step_up} if step_up else {})})
    assert r.status_code == 200, r.text
    return r.json()


def fetch(mid):
    return requests.get(f"{BASE}/mandates/{mid}/chain").json()


def read(mid):
    return requests.get(f"{BASE}/mandates/{mid}").json()


def append(mid, entry):
    return requests.post(f"{BASE}/mandates/{mid}/entries",
                         json={"entry": entry})


def reason_of(response):
    return response.json()["detail"]["reason"]


def build_next(m, type_, body):
    entries = fetch(m["mandate_id"])["entries"]
    prev = ledger.entry_hash(entries[-1]) if entries else None
    return ledger.build(m["mandate_id"], len(entries), prev, type_, body)


def child_of(m, cap=30000, categories=("sweets",),
             expires="2026-09-15T00:00:00Z", child_id=None, user_sig=False):
    """A DELEGATE entry signed by the agent, and the child's private key."""
    child_priv, child_pub = ledger.new_ed25519_keypair()
    kid = f"child_{uuid.uuid4().hex[:6]}"
    e = build_next(m, ledger.DELEGATE, {
        "child_mandate_id": child_id or f"mnd_{uuid.uuid4().hex[:8]}",
        "child_holder": "agt_gift",
        "child_holder_key": {"alg": "Ed25519", "kid": kid, "pub": child_pub},
        "cap_paise": cap, "categories": list(categories),
        "expires_at": expires})
    ledger.sign(e, "agent", m["keys"]["agent"]["kid"],
                m["demo_only_private_keys"]["agent"])
    if user_sig:
        ledger.sign(e, "user", m["keys"]["user"]["kid"],
                    m["demo_only_private_keys"]["user"])
    return e, kid, child_priv


def revoke(m):
    e = build_next(m, ledger.REVOKE, {"reason": "done", "scope": "self"})
    ledger.sign(e, "user", m["keys"]["user"]["kid"],
                m["demo_only_private_keys"]["user"])
    return e


# ───────────────────────────────────────────── opening the account ──

def test_a_ledger_mandate_opens_with_a_grant():
    m = issue()
    assert m["mode"] == "ledger"
    out = fetch(m["mandate_id"])
    grant = out["entries"][0]
    assert len(out["entries"]) == 1
    assert grant["type"] == "GRANT"
    assert grant["seq"] == 0
    assert grant["prev"] is None
    assert [s["role"] for s in grant["sigs"]] == ["user"]
    assert out["verification"]["valid"]
    assert out["verification"]["available_paise"] == 200000


def test_the_folded_state_is_reported_on_the_mandate():
    m = issue(cap=150000)
    out = read(m["mandate_id"])
    assert out["mode"] == "ledger"
    assert out["chain_valid"] is True
    assert out["remaining_paise"] == 150000
    assert out["spent_paise"] == 0
    assert out["entries"] == 1


def test_every_mandate_gets_its_own_keys():
    a, b = issue(), issue()
    assert a["keys"]["user"]["kid"] != b["keys"]["user"]["kid"]
    assert a["keys"]["agent"]["pub"] != b["keys"]["agent"]["pub"]
    # ...but one shop has one signing key.
    assert a["keys"]["merchant"] == b["keys"]["merchant"]


def test_the_merchant_never_hands_out_its_own_private_key():
    m = issue()
    assert set(m["demo_only_private_keys"]) == {"user", "agent"}


# ──────────────────────────────────── the old path still works ──

def test_the_hmac_mandate_is_untouched():
    """Both kinds coexist. Nothing that worked before stopped working."""
    m = issue(mode="hmac")
    assert m["mode"] == "hmac"
    assert "keys" not in m
    out = read(m["mandate_id"])
    assert out["mode"] == "hmac"
    assert out["remaining_paise"] == 200000
    assert requests.get(f"{BASE}/mandates/{m['mandate_id']}/chain"
                        ).status_code == 409


def test_hmac_is_still_the_default():
    r = requests.post(f"{BASE}/mandates", json={
        "agent_id": "agt_default", "max_amount_paise": 1000,
        "allowed_categories": ["sweets"], "valid_days": 7})
    assert r.json()["mode"] == "hmac"


def test_an_unknown_mandate_is_404():
    assert requests.get(f"{BASE}/mandates/mnd_nope/chain").status_code == 404
    assert append("mnd_nope", {"type": "REVOKE"}).status_code == 404


def test_appending_to_an_hmac_mandate_is_refused():
    m = issue(mode="hmac")
    e = ledger.build(m["mandate_id"], 0, None, ledger.REVOKE,
                     {"reason": "x", "scope": "self"})
    assert append(m["mandate_id"], e).status_code == 409


# ──────────────────────────────── what may not be posted here ──

def test_a_spend_cannot_be_appended_directly():
    """The gate is POST /orders. This endpoint is not a way around it."""
    m = issue()
    e = build_next(m, ledger.SPEND,
                   ledger.spend_body("ord_x", 100, "sweets.example",
                                     ["sweets"]))
    ledger.sign(e, "agent", m["keys"]["agent"]["kid"],
                m["demo_only_private_keys"]["agent"])
    r = append(m["mandate_id"], e)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "entry_type_not_postable"
    assert "POST /orders" in reason_of(r)


def test_a_second_grant_cannot_be_appended():
    m = issue()
    e = build_next(m, ledger.GRANT, {"cap_paise": 999999})
    assert append(m["mandate_id"], e).json()["detail"]["error"] == \
        "entry_type_not_postable"


def test_a_refund_cannot_be_appended_by_a_caller():
    m = issue()
    e = build_next(m, ledger.REFUND,
                   {"order_id": "ord_1", "amount_paise": 500000,
                    "currency": "INR", "merchant": "sweets.example",
                    "reason": "creative accounting"})
    assert append(m["mandate_id"], e).json()["detail"]["error"] == \
        "entry_type_not_postable"


def test_an_invented_entry_type_is_refused():
    m = issue()
    e = build_next(m, ledger.REVOKE, {"reason": "x", "scope": "self"})
    e["type"] = "MINT_MONEY"
    assert append(m["mandate_id"], e).status_code == 422


# ──────────────────────────────────────────────── delegation ──

def test_a_signed_delegation_moves_the_balance():
    m = issue()
    e, _, _ = child_of(m, cap=30000)
    r = append(m["mandate_id"], e)
    assert r.status_code == 200, r.text
    assert r.json()["seq"] == 1
    assert r.json()["available_paise"] == 170000

    out = read(m["mandate_id"])
    assert out["delegated_paise"] == 30000
    assert out["remaining_paise"] == 170000


def test_an_unsigned_delegation_is_refused():
    m = issue()
    e, _, _ = child_of(m)
    e["sigs"] = []
    r = append(m["mandate_id"], e)
    assert r.status_code == 422
    assert "missing signature from agent" in reason_of(r)


def test_a_tampered_delegation_is_refused():
    m = issue()
    e, _, _ = child_of(m, cap=1000)
    e["body"]["cap_paise"] = 190000                  # after signing
    r = append(m["mandate_id"], e)
    assert r.status_code == 422
    assert "does not verify" in reason_of(r)


def test_delegating_more_than_is_left_is_refused():
    m = issue(cap=50000)
    e, _, _ = child_of(m, cap=60000)
    assert "overdraw" in reason_of(append(m["mandate_id"], e))


def test_a_delegation_cannot_widen_the_allowlist():
    m = issue()
    e, _, _ = child_of(m, cap=1000, categories=("sweets", "electronics"))
    assert "widen" in reason_of(append(m["mandate_id"], e))


def test_a_delegation_cannot_outlive_its_parent():
    m = issue()
    e, _, _ = child_of(m, cap=1000, expires="2030-01-01T00:00:00Z")
    assert "outlive" in reason_of(append(m["mandate_id"], e))


def test_a_child_returns_what_it_did_not_spend():
    m = issue()
    e, child_kid, child_priv = child_of(m, cap=30000, child_id="mnd_kid")
    assert append(m["mandate_id"], e).status_code == 200

    r = build_next(m, ledger.RETURN,
                   {"child_mandate_id": "mnd_kid", "unspent_paise": 12000,
                    "child_final_hash": "sha256:abcd"})
    ledger.sign(r, "child", child_kid, child_priv)
    assert append(m["mandate_id"], r).status_code == 200

    out = read(m["mandate_id"])
    assert out["remaining_paise"] == 182000        # 170000 + 12000
    assert out["delegated_paise"] == 18000


def test_a_child_cannot_return_more_than_it_was_given():
    m = issue()
    e, child_kid, child_priv = child_of(m, cap=30000, child_id="mnd_kid2")
    append(m["mandate_id"], e)
    r = build_next(m, ledger.RETURN,
                   {"child_mandate_id": "mnd_kid2", "unspent_paise": 90000,
                    "child_final_hash": "sha256:abcd"})
    ledger.sign(r, "child", child_kid, child_priv)
    assert "cannot return more" in reason_of(append(m["mandate_id"], r))


# ──────────────────────────────────────────────────── step-up ──

def test_delegation_needs_approval_when_the_policy_says_so():
    m = issue(step_up={"always_for_delegation": True})
    unsigned, _, _ = child_of(m, cap=1000)
    assert "required approval" in reason_of(append(m["mandate_id"], unsigned))

    approved, _, _ = child_of(m, cap=1000, user_sig=True)
    assert append(m["mandate_id"], approved).status_code == 200


def test_the_policy_is_inside_the_signed_grant():
    """Not a server setting. It is part of what the user signed, which is
    why every later reader reaches the same conclusion about it."""
    m = issue(step_up={"always_for_delegation": True})
    grant = fetch(m["mandate_id"])["entries"][0]
    assert grant["body"]["step_up"] == {"always_for_delegation": True}


# ───────────────────────────────────────────────────── revoke ──

def test_a_user_can_close_the_account():
    m = issue()
    assert append(m["mandate_id"], revoke(m)).status_code == 200
    out = read(m["mandate_id"])
    assert out["status"] == "revoked"
    assert out["remaining_paise"] == 0


def test_nothing_may_follow_a_revoke():
    m = issue()
    append(m["mandate_id"], revoke(m))
    later, _, _ = child_of(m, cap=100)
    assert "REVOKE" in reason_of(append(m["mandate_id"], later))


def test_the_agent_cannot_revoke():
    """Only the user closes the account. The agent holds it, not owns it."""
    m = issue()
    e = build_next(m, ledger.REVOKE, {"reason": "no", "scope": "self"})
    ledger.sign(e, "agent", m["keys"]["agent"]["kid"],
                m["demo_only_private_keys"]["agent"])
    r = append(m["mandate_id"], e)
    assert r.status_code == 422
    assert "missing signature from user" in reason_of(r)


# ─────────────────────────────────────── storage does not lie ──

def test_the_same_position_cannot_be_filled_twice():
    """Two appends built against the same head. One lands."""
    m = issue()
    first, _, _ = child_of(m, cap=1000)
    second, _, _ = child_of(m, cap=1000)         # also seq 1
    assert append(m["mandate_id"], first).status_code == 200
    r = append(m["mandate_id"], second)
    assert r.status_code == 422
    assert ("already taken" in reason_of(r)
            or "removed or reordered" in reason_of(r)
            or "history has been edited" in reason_of(r))


def test_the_stored_bytes_are_the_bytes_that_were_signed():
    """The merchant round-trips entries through JSON. If that were lossy,
    every signature would break on the way back out."""
    m = issue()
    sent, _, _ = child_of(m, cap=1000)
    append(m["mandate_id"], sent)
    got = fetch(m["mandate_id"])["entries"][1]
    assert ledger.payload_bytes(got) == ledger.payload_bytes(sent)
    assert ledger.entry_hash(got) == ledger.entry_hash(sent)


def test_entries_come_back_in_order_with_prev_linked():
    m = issue()
    for _ in range(3):
        e, _, _ = child_of(m, cap=1000)
        append(m["mandate_id"], e)
    entries = fetch(m["mandate_id"])["entries"]
    assert [e["seq"] for e in entries] == [0, 1, 2, 3]
    for a, b in zip(entries, entries[1:]):
        assert b["prev"] == ledger.entry_hash(a)


# ────────────────────────────────────── the point of all this ──

def test_what_the_server_returns_verifies_without_the_server():
    """Fetch the chain and check it here, against the user's public key.

    Nothing in this test trusts the merchant's verdict. It re-folds the
    entries with the same code an outsider would run and gets the same
    answer -- which is the entire argument for a ledger over a column.
    """
    m = issue(cap=100000, step_up={"always_for_delegation": True})
    e1, kid, priv = child_of(m, cap=30000, child_id="mnd_v1", user_sig=True)
    assert append(m["mandate_id"], e1).status_code == 200

    ret = build_next(m, ledger.RETURN,
                     {"child_mandate_id": "mnd_v1", "unspent_paise": 5000,
                      "child_final_hash": "sha256:beef"})
    ledger.sign(ret, "child", kid, priv)
    assert append(m["mandate_id"], ret).status_code == 200

    entries = fetch(m["mandate_id"])["entries"]
    mine = ledger.verify(entries, user_pub=m["keys"]["user"]["pub"])

    assert mine.valid, mine.reason
    assert mine.entries == 3
    assert mine.delegated_paise == 25000
    assert mine.available_paise == 75000

    theirs = read(m["mandate_id"])
    assert theirs["remaining_paise"] == mine.available_paise
    assert theirs["delegated_paise"] == mine.delegated_paise


def test_a_chain_edited_after_download_stops_verifying():
    m = issue()
    e, _, _ = child_of(m, cap=30000)
    append(m["mandate_id"], e)
    entries = fetch(m["mandate_id"])["entries"]
    assert ledger.verify(entries).valid

    entries[1]["body"]["cap_paise"] = 100
    out = ledger.verify(entries)
    assert not out.valid
    assert out.at_entry == 1


def test_the_anchor_catches_a_chain_from_somewhere_else():
    a, b = issue(), issue()
    entries = fetch(a["mandate_id"])["entries"]
    out = ledger.verify(entries, user_pub=b["keys"]["user"]["pub"])
    assert not out.valid
    assert "expected user key" in out.reason