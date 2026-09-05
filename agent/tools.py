import os
import requests

# The only thing the agent is allowed to know up front: where the merchant is.
# Everything else — endpoint paths, delivery fee, categories — is discovered.
BASE    = os.getenv("MERCHANT_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = 15

_manifest = None


# ── discovery ────────────────────────────────────────────────────────────
def discover(base_url=None):
    """Read the merchant's manifest. Everything else derives from it."""
    global _manifest, BASE
    if base_url:
        BASE = base_url.rstrip("/")
        _manifest = None
    if _manifest is None:
        r = requests.get(f"{BASE}/.well-known/agent-catalog", timeout=TIMEOUT)
        r.raise_for_status()
        _manifest = r.json()
    return _manifest


def _url(name, **fmt):
    """Resolve an endpoint by capability name, from the manifest."""
    path = discover()["endpoints"][name]
    return f"{BASE}{path.format(**fmt)}"


def delivery_paise():
    return discover()["commerce"]["delivery_fee_paise"]


def merchant_name():
    return discover()["merchant"]["name"]


# ── HTTP helpers: a non-200 is an outcome, never an exception ────────────
def _result(r):
    if r.status_code == 200:
        return True, r.json()
    try:
        return False, r.json()
    except Exception:
        return False, {"detail": f"http_{r.status_code}"}


def _safe(fn):
    """Network failure is an outcome too."""
    try:
        return fn()
    except requests.RequestException as e:
        return False, {"detail": f"merchant_unreachable: {str(e)[:120]}"}


# ── merchant calls ───────────────────────────────────────────────────────
def get_catalog(trace_id):
    return _safe(lambda: _result(requests.get(
        _url("catalog"), params={"trace_id": trace_id}, timeout=TIMEOUT)))


def get_quote(trace_id, items):
    return _safe(lambda: _result(requests.post(
        _url("quote"), timeout=TIMEOUT,
        json={"trace_id": trace_id, "items": items})))


def create_order(trace_id, mandate_id, items):
    return _safe(lambda: _result(requests.post(
        _url("orders"), timeout=TIMEOUT,
        json={"trace_id": trace_id, "mandate_id": mandate_id,
              "items": items})))


def pay_order(order_id):
    return _safe(lambda: _result(requests.post(
        _url("order_pay", order_id=order_id), timeout=TIMEOUT)))


def fetch_order(order_id):
    return _safe(lambda: _result(requests.get(
        _url("order_status", order_id=order_id), timeout=TIMEOUT)))


def get_mandate(mandate_id):
    return _safe(lambda: _result(requests.get(
        _url("mandate_read", mandate_id=mandate_id), timeout=TIMEOUT)))


def push_audit(trace_id, step, decision, reason="", amount_paise=None):
    try:
        requests.post(_url("audit_write"), timeout=TIMEOUT,
                      json={"trace_id": trace_id, "step": step,
                            "decision": decision, "reason": reason,
                            "amount_paise": amount_paise})
    except Exception:
        pass   # audit must never break the purchase