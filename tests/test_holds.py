"""A hold is the shopkeeper putting your boxes on the counter."""
import os, time, requests

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001")


def stock_of(pid):
    cat = requests.get(f"{BASE}/catalog", params={"trace_id": "t_hold"}).json()
    return next(p for p in cat if p["id"] == pid)["availability"]["quantity"]


def hold(items, trace="t_hold"):
    return requests.post(f"{BASE}/holds",
                         json={"trace_id": trace, "items": items})


def mandate(cap=500000, cats=("sweets",)):
    return requests.post(f"{BASE}/mandates", json={
        "agent_id": "agt_hold", "max_amount_paise": cap,
        "allowed_categories": list(cats), "valid_days": 7}).json()["mandate_id"]


def test_hold_takes_stock_off_the_shelf():
    before = stock_of("sw_005")
    h = hold([{"id": "sw_005", "qty": 2}]).json()
    try:
        assert stock_of("sw_005") == before - 2
        assert h["total_paise"] == 35000 * 2 + 4000
    finally:
        # Don't leave stock parked. A hold left behind expires later and
        # quietly returns its units in the middle of another test.
        requests.delete(f"{BASE}/holds/{h['hold_id']}")


def test_releasing_a_hold_puts_stock_back():
    before = stock_of("sw_005")
    h = hold([{"id": "sw_005", "qty": 2}]).json()
    requests.delete(f"{BASE}/holds/{h['hold_id']}")
    assert stock_of("sw_005") == before


def test_ordering_against_a_hold_does_not_take_stock_twice():
    before = stock_of("sw_003")
    h = hold([{"id": "sw_003", "qty": 1}]).json()
    assert stock_of("sw_003") == before - 1

    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_hold", "mandate_id": mandate(),
        "hold_id": h["hold_id"]})
    assert r.status_code == 200
    assert r.json()["total_paise"] == h["total_paise"]
    assert stock_of("sw_003") == before - 1        # not - 2


def test_a_hold_can_only_be_spent_once():
    h = hold([{"id": "sw_003", "qty": 1}]).json()
    mid = mandate()
    assert requests.post(f"{BASE}/orders", json={
        "trace_id": "t_hold", "mandate_id": mid,
        "hold_id": h["hold_id"]}).status_code == 200
    again = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_hold", "mandate_id": mid, "hold_id": h["hold_id"]})
    assert again.status_code == 403
    assert again.json()["detail"] == "hold_not_active"


def test_price_is_frozen_for_the_life_of_the_hold():
    """What the human approves is what the human pays."""
    h = hold([{"id": "sw_001", "qty": 2}]).json()
    quoted = h["total_paise"]
    read = requests.get(f"{BASE}/holds/{h['hold_id']}").json()
    assert read["total_paise"] == quoted
    assert read["seconds_left"] > 0
    requests.delete(f"{BASE}/holds/{h['hold_id']}")


def test_holding_more_than_the_shelf_has_is_refused():
    r = hold([{"id": "sw_013", "qty": 999}])
    assert r.status_code == 409