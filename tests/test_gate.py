import os, requests

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001")

def mandate(cap=200000, cats=("sweets",), days=7):
    r = requests.post(f"{BASE}/mandates", json={
        "agent_id": "agt_test", "max_amount_paise": cap,
        "allowed_categories": list(cats), "valid_days": days})
    return r.json()["mandate_id"]

def order(mid, items, trace="t_test"):
    return requests.post(f"{BASE}/orders", json={
        "trace_id": trace, "mandate_id": mid, "items": items})


def test_within_cap_succeeds():
    r = order(mandate(), [{"id": "sw_001", "qty": 2}])
    assert r.status_code == 200
    assert r.json()["total_paise"] == 164000

def test_over_cap_refused():
    r = order(mandate(cap=100000), [{"id": "sw_001", "qty": 2}])
    assert r.status_code == 403
    assert r.json()["detail"] == "exceeds_cap"

def test_blocked_category_refused():
    r = order(mandate(cap=500000, cats=("sweets",)),
              [{"id": "sw_006", "qty": 1}])
    assert r.status_code == 403
    assert r.json()["detail"] == "category_blocked"

def test_expired_mandate_refused():
    r = order(mandate(days=-1), [{"id": "sw_001", "qty": 1}])
    assert r.status_code == 403
    assert r.json()["detail"] == "mandate_expired"

def test_unknown_mandate_refused():
    r = order("mnd_doesnotexist", [{"id": "sw_001", "qty": 1}])
    assert r.status_code == 403
    assert r.json()["detail"] == "unknown_mandate"

def test_insufficient_stock_refused():
    r = order(mandate(cap=999999), [{"id": "sw_004", "qty": 99}])
    assert r.status_code == 409

def test_caller_supplied_price_is_ignored():
    """A hostile client claiming a low price still gets the real one."""
    mid = mandate(cap=100000)
    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_price", "mandate_id": mid,
        "items": [{"id": "sw_001", "qty": 2, "price_paise": 1}]})
    assert r.status_code == 403          # merchant re-prices to 164000

def test_payment_link_is_idempotent():
    oid = order(mandate(), [{"id": "sw_002", "qty": 1}]).json()["order_id"]
    a = requests.post(f"{BASE}/orders/{oid}/pay").json()
    b = requests.post(f"{BASE}/orders/{oid}/pay").json()
    assert a["payment_url"] == b["payment_url"]
    assert b["reused"] is True

def test_refusal_is_logged():
    order(mandate(cap=100000), [{"id": "sw_001", "qty": 2}], trace="t_logged")
    rows = requests.get(f"{BASE}/audit/t_logged").json()
    assert any(r["decision"] == "refused" for r in rows)