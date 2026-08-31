import requests

BASE = "http://127.0.0.1:8000"
TIMEOUT = 15

def get_catalog(trace_id):
    r = requests.get(f"{BASE}/catalog", params={"trace_id": trace_id},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def get_quote(trace_id, items):
    r = requests.post(f"{BASE}/quote", timeout=TIMEOUT,
                      json={"trace_id": trace_id, "items": items})
    r.raise_for_status()
    return r.json()

def create_order(trace_id, mandate_id, items):
    """Returns (ok, payload). 403 is a refusal, not a crash."""
    r = requests.post(f"{BASE}/orders", timeout=TIMEOUT,
                      json={"trace_id": trace_id,
                            "mandate_id": mandate_id, "items": items})
    if r.status_code == 200:
        return True, r.json()
    return False, r.json()

def pay_order(order_id):
    r = requests.post(f"{BASE}/orders/{order_id}/pay", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def fetch_order(order_id):
    r = requests.get(f"{BASE}/orders/{order_id}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def push_audit(trace_id, step, decision, reason="", amount_paise=None):
    try:
        requests.post(f"{BASE}/audit", timeout=TIMEOUT,
                      json={"trace_id": trace_id, "step": step,
                            "decision": decision, "reason": reason,
                            "amount_paise": amount_paise})
    except Exception:
        pass   # audit must never break the purchase