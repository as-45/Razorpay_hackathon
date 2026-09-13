"""The gate, running on the chain.

POST /orders used to answer "can this mandate afford it?" with a SQL sum.
For a ledger-backed mandate it now answers by folding a signed chain, and
appends the dual-signed SPEND in the same transaction as the order.

The tests that matter most here are the ones where the agent and the
merchant disagree -- about the price, about what is in the basket -- and
the purchase simply does not happen, because a signature cannot be made
to cover a number nobody signed.
"""
import os
import uuid

import requests

from merchant import ledger

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001")


# ────────────────────────────────────────────────────────── helpers ──

def issue(cap=200000, categories=("sweets",), step_up=None, mode="ledger"):
    r = requests.post(f"{BASE}/mandates", json={
        "agent_id": f"agt_{uuid.uuid4().hex[:6]}",
        "max_amount_paise": cap, "allowed_categories": list(categories),
        "valid_days": 7, "mode": mode,
        **({"step_up": step_up} if step_up else {})})
    assert r.status_code == 200, r.text
    return r.json()


def chain_of(mid):
    return requests.get(f"{BASE}/mandates/{mid}/chain").json()["entries"]


def read(mid):
    return requests.get(f"{BASE}/mandates/{mid}").json()


def quote(items):
    r = requests.post(f"{BASE}/quote",
                      json={"trace_id": "t_led", "items": items})
    assert r.status_code == 200, r.text
    return r.json()


def spend_for(m, items, amount=None, categories=("sweets",), order_id=None,
              hold_id=None, sign_agent=True, sign_user=False):
    """Build the SPEND entry an agent would sign for this basket."""
    entries = chain_of(m["mandate_id"])
    prev = ledger.entry_hash(entries[-1]) if entries else None
    body = ledger.spend_body(
        order_id or f"ord_{uuid.uuid4().hex[:10]}",
        amount if amount is not None else quote(items)["total_paise"],
        "sweets.example", list(categories),
        basket_hash=ledger.basket_hash(items), hold_id=hold_id)
    e = ledger.build(m["mandate_id"], len(entries), prev, ledger.SPEND, body)
    if sign_agent:
        ledger.sign(e, "agent", m["keys"]["agent"]["kid"],
                    m["demo_only_private_keys"]["agent"])
    if sign_user:
        ledger.sign(e, "user", m["keys"]["user"]["kid"],
                    m["demo_only_private_keys"]["user"])
    return e


def order(m, items, entry, trace="t_led", key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return requests.post(f"{BASE}/orders", headers=headers, json={
        "trace_id": trace, "mandate_id": m["mandate_id"],
        "items": items, "spend_entry": entry})


# Chikki: Rs 120 a bar, sixty in stock. Cheap and plentiful, so this
# file can run twice without emptying a shelf.
ONE_BAR = [{"id": "sw_011", "qty": 1}]


# ──────────────────────────────────────────────── the happy path ──

def test_a_ledger_order_is_created_and_recorded():
    m = issue()
    total = quote(ONE_BAR)["total_paise"]
    e = spend_for(m, ONE_BAR)
    r = order(m, ONE_BAR, e)
    assert r.status_code == 200, r.text
    out = r.json()

    assert out["total_paise"] == total
    assert out["entry_seq"] == 1
    assert out["remaining_paise"] == 200000 - total
    # The buyer named the order when it signed; that name is the order id.
    assert out["order_id"] == e["body"]["order_id"]


def test_the_spend_lands_on_the_chain_dual_signed():
    m = issue()
    order(m, ONE_BAR, spend_for(m, ONE_BAR))
    entries = chain_of(m["mandate_id"])
    assert len(entries) == 2
    spend = entries[1]
    assert spend["type"] == "SPEND"
    assert sorted(s["role"] for s in spend["sigs"]) == ["agent", "merchant"]


def test_the_folded_balance_matches_the_order():
    m = issue()
    total = quote(ONE_BAR)["total_paise"]
    order(m, ONE_BAR, spend_for(m, ONE_BAR))
    out = read(m["mandate_id"])
    assert out["spent_paise"] == total
    assert out["remaining_paise"] == 200000 - total


def test_two_orders_accumulate_against_one_budget():
    m = issue()
    total = quote(ONE_BAR)["total_paise"]
    assert order(m, ONE_BAR, spend_for(m, ONE_BAR)).status_code == 200
    assert order(m, ONE_BAR, spend_for(m, ONE_BAR)).status_code == 200
    assert read(m["mandate_id"])["remaining_paise"] == 200000 - 2 * total


# ──────────────────────────── the agent and the merchant disagree ──

def test_an_entry_signed_for_the_wrong_price_is_refused():
    """The heart of it. A shop cannot reprice between quote and charge,
    because the signature it needs covers the old number."""
    m = issue()
    e = spend_for(m, ONE_BAR, amount=1)
    r = order(m, ONE_BAR, e)
    assert r.status_code == 403
    assert r.json()["detail"] == "amount_mismatch"


def test_an_entry_lying_about_the_categories_is_refused():
    """Otherwise an agent buys electronics and writes 'sweets' on the
    receipt, and the allowlist check passes on a fiction."""
    m = issue(categories=("sweets", "addons"))
    items = [{"id": "sw_007", "qty": 1}]            # gift wrap: addons
    e = spend_for(m, items, categories=("sweets",))
    r = order(m, items, e)
    assert r.status_code == 403
    assert r.json()["detail"] == "category_mismatch"


def test_a_real_blocked_category_is_still_blocked():
    m = issue(categories=("sweets",))
    items = [{"id": "sw_007", "qty": 1}]            # addons, not allowed
    e = spend_for(m, items, categories=("addons",))
    r = order(m, items, e)
    assert r.status_code == 403
    assert r.json()["detail"] == "category_blocked"


def test_an_unsigned_entry_is_refused():
    m = issue()
    e = spend_for(m, ONE_BAR, sign_agent=False)
    r = order(m, ONE_BAR, e)
    assert r.status_code == 403
    assert r.json()["detail"] == "bad_signature"


def test_an_entry_tampered_after_signing_is_refused():
    m = issue()
    e = spend_for(m, ONE_BAR)
    e["body"]["basket_hash"] = "sha256:0000"        # after the signature
    r = order(m, ONE_BAR, e)
    assert r.status_code == 403
    assert r.json()["detail"] == "bad_signature"


def test_an_order_without_an_entry_is_refused():
    m = issue()
    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_led", "mandate_id": m["mandate_id"],
        "items": ONE_BAR})
    assert r.status_code == 403
    assert r.json()["detail"] == "spend_entry_required"


def test_an_entry_built_on_a_stale_head_is_refused():
    m = issue()
    stale = spend_for(m, ONE_BAR)                  # built at seq 1
    assert order(m, ONE_BAR, spend_for(m, ONE_BAR)).status_code == 200
    r = order(m, ONE_BAR, stale)                   # chain is at seq 2 now
    assert r.status_code == 403
    assert r.json()["detail"] == "stale_entry"


def test_an_entry_for_another_mandate_is_refused():
    a, b = issue(), issue()
    e = spend_for(b, ONE_BAR)
    r = order(a, ONE_BAR, e)
    assert r.status_code == 403
    assert r.json()["detail"] in ("wrong_mandate", "stale_entry")


# ─────────────────────────────────────────────────── the envelope ──

def test_the_envelope_is_enforced_by_the_fold():
    m = issue(cap=10000)                      # less than one bar costs
    e = spend_for(m, ONE_BAR)
    r = order(m, ONE_BAR, e)
    assert r.status_code == 403
    assert r.json()["detail"] == "envelope_exhausted"


def test_a_refused_order_moves_no_stock():
    m = issue(cap=10000)

    def stock():
        cat = requests.get(f"{BASE}/catalog",
                           params={"trace_id": "t_led"}).json()
        return next(p for p in cat if p["id"] == "sw_011"
                    )["availability"]["quantity"]

    before = stock()
    order(m, ONE_BAR, spend_for(m, ONE_BAR))
    assert stock() == before


def test_a_revoked_mandate_cannot_buy():
    m = issue()
    entries = chain_of(m["mandate_id"])
    rev = ledger.build(m["mandate_id"], 1, ledger.entry_hash(entries[-1]),
                       ledger.REVOKE, {"reason": "done", "scope": "self"})
    ledger.sign(rev, "user", m["keys"]["user"]["kid"],
                m["demo_only_private_keys"]["user"])
    assert requests.post(f"{BASE}/mandates/{m['mandate_id']}/entries",
                         json={"entry": rev}).status_code == 200

    r = order(m, ONE_BAR, spend_for(m, ONE_BAR))
    assert r.status_code == 403
    assert r.json()["detail"] == "mandate_revoked"


# ─────────────────────────────────────────────────────── step-up ──

# One bar is Rs 160 with delivery, three is Rs 400. The threshold sits
# between them, so one runs alone and three ask for a signature.
STEP_UP = {"per_order_above_paise": 20000}


def test_a_small_order_runs_without_the_user():
    m = issue(step_up=STEP_UP)
    assert order(m, ONE_BAR, spend_for(m, ONE_BAR)).status_code == 200


def test_a_large_order_without_approval_is_refused():
    m = issue(step_up=STEP_UP)
    items = [{"id": "sw_011", "qty": 3}]
    r = order(m, items, spend_for(m, items))
    assert r.status_code == 403
    assert r.json()["detail"] == "approval_required"


def test_the_same_large_order_goes_through_once_the_user_signs():
    m = issue(step_up=STEP_UP)
    items = [{"id": "sw_011", "qty": 3}]
    r = order(m, items, spend_for(m, items, sign_user=True))
    assert r.status_code == 200, r.text
    spend = chain_of(m["mandate_id"])[1]
    assert sorted(s["role"] for s in spend["sigs"]) == \
        ["agent", "merchant", "user"]


# ─────────────────────────────────────────────────── idempotency ──

def test_a_retry_returns_the_same_order_and_appends_once():
    m = issue()
    key = f"idem-{uuid.uuid4().hex[:8]}"
    e = spend_for(m, ONE_BAR)
    first = order(m, ONE_BAR, e, key=key)
    assert first.status_code == 200

    second = order(m, ONE_BAR, e, key=key)
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["order_id"] == first.json()["order_id"]
    assert len(chain_of(m["mandate_id"])) == 2         # not 3


def test_the_same_order_id_cannot_be_charged_twice():
    m = issue()
    oid = f"ord_{uuid.uuid4().hex[:8]}"
    assert order(m, ONE_BAR, spend_for(m, ONE_BAR,
                                        order_id=oid)).status_code == 200
    r = order(m, ONE_BAR, spend_for(m, ONE_BAR, order_id=oid))
    assert r.status_code == 403
    assert r.json()["detail"] == "duplicate_order"


# ────────────────────────────────────────────── holds still work ──

def test_a_held_basket_can_be_ordered_on_a_ledger_mandate():
    m = issue()
    h = requests.post(f"{BASE}/holds",
                      json={"trace_id": "t_led", "items": ONE_BAR}).json()
    e = spend_for(m, ONE_BAR, amount=h["total_paise"], hold_id=h["hold_id"])
    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_led", "mandate_id": m["mandate_id"],
        "hold_id": h["hold_id"], "spend_entry": e})
    assert r.status_code == 200, r.text
    assert r.json()["total_paise"] == h["total_paise"]


def test_an_entry_naming_the_wrong_hold_is_refused():
    m = issue()
    h = requests.post(f"{BASE}/holds",
                      json={"trace_id": "t_led", "items": ONE_BAR}).json()
    e = spend_for(m, ONE_BAR, amount=h["total_paise"], hold_id="hld_other")
    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_led", "mandate_id": m["mandate_id"],
        "hold_id": h["hold_id"], "spend_entry": e})
    assert r.status_code == 403
    assert r.json()["detail"] == "hold_mismatch"
    requests.delete(f"{BASE}/holds/{h['hold_id']}")


# ────────────────────────────────── the old path is untouched ──

def test_an_hmac_mandate_orders_exactly_as_before():
    """No spend_entry, no chain, no change. The submitted demo still runs."""
    m = issue(mode="hmac")
    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_led_hmac", "mandate_id": m["mandate_id"],
        "items": ONE_BAR})
    assert r.status_code == 200, r.text
    assert "entry_seq" not in r.json()
    assert r.json()["order_id"].startswith("ord_")


def test_an_hmac_mandate_ignores_a_spend_entry_it_was_sent():
    m = issue(mode="hmac")
    junk = {"type": "SPEND", "body": {"amount_paise": 1}}
    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_led_hmac", "mandate_id": m["mandate_id"],
        "items": ONE_BAR, "spend_entry": junk})
    assert r.status_code == 200, r.text


# ───────────────────────────────────── the point of all of it ──

def test_the_receipt_verifies_without_the_merchant():
    """After a real purchase, download the chain and check it here.

    Not "the server says Rs 1,600 is left" -- the entries say so, signed,
    and anyone can recompute it.
    """
    m = issue(cap=100000, step_up=STEP_UP)
    total = quote(ONE_BAR)["total_paise"]
    r = order(m, ONE_BAR, spend_for(m, ONE_BAR))
    assert r.status_code == 200, r.text

    entries = chain_of(m["mandate_id"])
    mine = ledger.verify(entries, user_pub=m["keys"]["user"]["pub"])

    assert mine.valid, mine.reason
    assert mine.spent_paise == total
    assert mine.available_paise == 100000 - total
    assert entries[1]["body"]["order_id"] == r.json()["order_id"]
    assert read(m["mandate_id"])["remaining_paise"] == mine.available_paise


def test_an_edited_receipt_stops_verifying():
    m = issue()
    order(m, ONE_BAR, spend_for(m, ONE_BAR))
    entries = chain_of(m["mandate_id"])
    entries[1]["body"]["amount_paise"] = 1
    out = ledger.verify(entries)
    assert not out.valid
    assert out.at_entry == 1