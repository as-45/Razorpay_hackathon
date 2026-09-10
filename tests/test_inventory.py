"""Stock has to actually move, and only once."""
import os, uuid, requests, pytest
from concurrent.futures import ThreadPoolExecutor

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001")


def stock_of(pid):
    cat = requests.get(f"{BASE}/catalog", params={"trace_id": "t_inv"}).json()
    return next(p for p in cat if p["id"] == pid)["availability"]["quantity"]


def mandate(cap=5_000_00, cats=("sweets", "premium", "milk", "new", "addons")):
    return requests.post(f"{BASE}/mandates", json={
        "agent_id": "agt_inv", "max_amount_paise": cap,
        "allowed_categories": list(cats), "valid_days": 7}).json()["mandate_id"]


def order(mid, items, trace="t_inv", key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return requests.post(f"{BASE}/orders", headers=headers, json={
        "trace_id": trace, "mandate_id": mid, "items": items})


def test_order_reduces_stock():
    before = stock_of("sw_011")            # chikki, Rs 120, plenty in stock
    r = order(mandate(), [{"id": "sw_011", "qty": 2}])
    assert r.status_code == 200
    assert stock_of("sw_011") == before - 2


def test_cannot_oversell_across_orders():
    """Buy the shelf empty one order at a time, then the next must fail."""
    left = stock_of("sw_013")              # gold leaf barfi, only 4 seeded
    mid = mandate(cap=50_000_00, cats=("premium",))
    for _ in range(left):
        assert order(mid, [{"id": "sw_013", "qty": 1}]).status_code == 200
    assert stock_of("sw_013") == 0
    assert order(mid, [{"id": "sw_013", "qty": 1}]).status_code == 409


def test_two_agents_cannot_both_take_the_last_one():
    """The race a read-then-write could not survive.

    A hold parks everything but one unit, so the shelf really does hold
    exactly one when the two requests land. The hold is released at the
    end, which is what keeps this test runnable more than once.
    """
    pid = "sw_011"
    have = stock_of(pid)
    if have < 1:
        pytest.skip(f"{pid} is out of stock — reseed to run this")

    parked = None
    if have > 1:
        parked = requests.post(f"{BASE}/holds", json={
            "trace_id": "t_race",
            "items": [{"id": pid, "qty": have - 1}]}).json()["hold_id"]
    assert stock_of(pid) == 1

    try:
        mid = mandate(cap=50_000_00, cats=("sweets",))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(order, mid, [{"id": pid, "qty": 1}],
                                   f"t_race{i}") for i in range(2)]
            codes = sorted(f.result().status_code for f in futures)

        assert codes == [200, 409], f"expected one winner, got {codes}"
        assert stock_of(pid) == 0
    finally:
        if parked:
            requests.delete(f"{BASE}/holds/{parked}")


def test_mandate_is_an_envelope_not_a_per_order_limit():
    """A Rs 2,000 mandate must not fund three Rs 900 orders."""
    mid = mandate(cap=200000, cats=("sweets",))
    assert order(mid, [{"id": "sw_002", "qty": 1}]).status_code == 200   # 490
    assert order(mid, [{"id": "sw_002", "qty": 1}]).status_code == 200   # 980
    assert order(mid, [{"id": "sw_002", "qty": 2}]).status_code == 200   # 1900
    r = order(mid, [{"id": "sw_002", "qty": 1}])                        # 2390
    assert r.status_code == 403
    assert r.json()["detail"] == "envelope_exhausted"


def test_mandate_reports_remaining_budget():
    mid = mandate(cap=200000, cats=("sweets",))
    order(mid, [{"id": "sw_002", "qty": 1}])          # Rs 450 + 40
    m = requests.get(f"{BASE}/mandates/{mid}").json()
    assert m["spent_paise"] == 49000
    assert m["remaining_paise"] == 151000


def test_retry_with_same_key_does_not_double_order():
    key = f"idem-{uuid.uuid4().hex[:8]}"   # fresh per run; keys live forever
    mid = mandate(cap=500000, cats=("sweets",))
    before = stock_of("sw_010")
    a = order(mid, [{"id": "sw_010", "qty": 1}], key=key)
    b = order(mid, [{"id": "sw_010", "qty": 1}], key=key)
    assert a.json()["order_id"] == b.json()["order_id"]
    assert b.json()["reused"] is True
    assert stock_of("sw_010") == before - 1
