"""The gate has no side doors, and the trail is not open to anyone."""
import os, time, requests

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001")


def mandate(cap=200000, cats=("sweets",), days=7):
    return requests.post(f"{BASE}/mandates", json={
        "agent_id": "agt_trust", "max_amount_paise": cap,
        "allowed_categories": list(cats), "valid_days": days}).json()["mandate_id"]


# ── the audit trail ──────────────────────────────────────────────────────
def test_anonymous_writes_to_the_trail_are_refused():
    """The trail is evidence. A stranger must not be able to forge it."""
    r = requests.post(f"{BASE}/audit", json={
        "trace_id": "t_forged", "step": "payment_captured",
        "decision": "ok", "reason": "money definitely moved, honest"})
    assert r.status_code == 403
    assert r.json()["detail"] == "audit_requires_mandate"
    assert requests.get(f"{BASE}/audit/t_forged").json() == []


def test_an_unknown_mandate_cannot_write_either():
    r = requests.post(f"{BASE}/audit", json={
        "trace_id": "t_forged2", "step": "anything", "decision": "ok",
        "mandate_id": "mnd_doesnotexist"})
    assert r.status_code == 403


def test_a_real_mandate_can_narrate_its_own_run():
    mid = mandate()
    r = requests.post(f"{BASE}/audit", json={
        "trace_id": "t_real", "step": "intent_parsed", "decision": "ok",
        "reason": "kaju katli x2", "mandate_id": mid})
    assert r.status_code == 200
    rows = requests.get(f"{BASE}/audit/t_real").json()
    assert any(x["step"] == "intent_parsed" and x["actor"] == "agent"
               for x in rows)


# ── the mandate signature ────────────────────────────────────────────────
def test_editing_a_mandate_in_the_database_invalidates_it():
    """The signature is what makes the stored terms trustworthy. Raising the
    cap by hand must break it — including for a passkey-approved mandate,
    which used to skip this check entirely."""
    from merchant.db import SessionLocal
    from merchant.models import Mandate

    mid = mandate(cap=100000)
    db = SessionLocal()
    m = db.get(Mandate, mid)
    m.max_amount_paise = 10_000_00        # help yourself to a bigger budget
    m.customer_id = "cust_pretend_passkey"  # and claim a passkey approved it
    db.commit()
    db.close()

    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_tamper", "mandate_id": mid,
        "items": [{"id": "sw_001", "qty": 2}]})
    assert r.status_code == 403
    assert r.json()["detail"] == "bad_signature"


# ── holds expire ─────────────────────────────────────────────────────────
def test_an_expired_hold_cannot_be_spent_and_returns_the_stock():
    """What happens if the human takes too long to say yes."""
    ttl = requests.get(f"{BASE}/.well-known/agent-catalog").json()[
        "reservations"]["ttl_seconds"]
    if ttl > 20:
        import pytest
        pytest.skip(f"hold TTL is {ttl}s — set HOLD_TTL_SECONDS low to test this")

    def stock():
        cat = requests.get(f"{BASE}/catalog", params={"trace_id": "t_exp"}).json()
        return next(p for p in cat if p["id"] == "sw_005")["availability"]["quantity"]

    # Let anything else that is holding stock time out first, so the only
    # movement measured below is this test's own.
    time.sleep(ttl + 2)
    requests.get(f"{BASE}/holds/flush-sweep")     # any request runs the sweep
    before = stock()
    h = requests.post(f"{BASE}/holds", json={
        "trace_id": "t_exp", "items": [{"id": "sw_005", "qty": 1}]}).json()
    assert stock() == before - 1

    time.sleep(ttl + 2)

    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_exp", "mandate_id": mandate(),
        "hold_id": h["hold_id"]})
    assert r.status_code == 403
    assert r.json()["detail"] == "hold_not_active"
    assert stock() == before          # given back, nothing charged