"""The shop may offer an extra. It may not add one.

Everything here is about the line between suggesting and selling: the
merchant declares pairings, the buyer checks them against its mandate, and
a human decides. Nothing in this file lets the shop past that.
"""
import os, requests

BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8001")


def suggest(items, trace="t_upsell"):
    return requests.post(f"{BASE}/suggest",
                         json={"trace_id": trace, "items": items}).json()


def test_the_shop_offers_what_it_declared():
    """Kaju katli pairs with gift wrap in the merchant's own catalog."""
    out = suggest([{"id": "sw_001", "qty": 2}])
    ids = [s["id"] for s in out["suggestions"]]
    assert "sw_007" in ids                      # gift wrap
    wrap = next(s for s in out["suggestions"] if s["id"] == "sw_007")
    assert wrap["price_paise"] == 4000
    assert wrap["because_of"] == "sw_001"
    assert "kaju" in wrap["reason"].lower()


def test_it_never_offers_what_is_already_in_the_basket():
    out = suggest([{"id": "sw_001", "qty": 1}, {"id": "sw_007", "qty": 1}])
    assert "sw_007" not in [s["id"] for s in out["suggestions"]]


def test_the_same_extra_is_only_offered_once():
    """Two sweets that both pair with gift wrap is still one gift wrap."""
    out = suggest([{"id": "sw_001", "qty": 1}, {"id": "sw_002", "qty": 1}])
    ids = [s["id"] for s in out["suggestions"]]
    assert ids.count("sw_007") == 1


def test_offers_are_capped():
    out = suggest([{"id": pid, "qty": 1} for pid in
                   ("sw_001", "sw_002", "sw_003", "sw_004", "sw_005")])
    assert len(out["suggestions"]) <= out["max_accepted"]


def test_a_product_with_no_pairings_gets_no_offer():
    out = suggest([{"id": "sw_009", "qty": 1}])     # saffron bar declares none
    assert out["suggestions"] == []


def test_the_offer_is_written_to_the_trail():
    """A shop that wants credit for its upselling needs it recorded."""
    suggest([{"id": "sw_001", "qty": 1}], trace="t_upsell_log")
    rows = requests.get(f"{BASE}/audit/t_upsell_log").json()
    assert any(r["step"] == "upsell_offered" for r in rows)


def test_suggesting_does_not_reserve_or_sell_anything():
    """The whole point: an offer is not a purchase."""
    def stock():
        cat = requests.get(f"{BASE}/catalog",
                           params={"trace_id": "t_upsell"}).json()
        return next(p for p in cat if p["id"] == "sw_007")["availability"]["quantity"]

    before = stock()
    suggest([{"id": "sw_001", "qty": 1}])
    assert stock() == before


def test_an_accepted_extra_is_authorised_like_anything_else():
    """Taking the offer must not slip past the mandate."""
    mid = requests.post(f"{BASE}/mandates", json={
        "agent_id": "agt_upsell", "max_amount_paise": 200000,
        "allowed_categories": ["sweets"],        # NOT addons
        "valid_days": 7}).json()["mandate_id"]

    r = requests.post(f"{BASE}/orders", json={
        "trace_id": "t_upsell_gate", "mandate_id": mid,
        "items": [{"id": "sw_001", "qty": 1}, {"id": "sw_007", "qty": 1}]})
    assert r.status_code == 403
    assert r.json()["detail"] == "category_blocked"