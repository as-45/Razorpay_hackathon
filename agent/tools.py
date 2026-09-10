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


def supports_holds():
    return bool(discover().get("capabilities", {}).get("holds"))


def supports_suggest():
    return bool(discover().get("capabilities", {}).get("suggest"))


def get_suggestions(trace_id, items):
    """Ask the shop what it would offer alongside this basket."""
    return _safe(lambda: _result(requests.post(
        _url("suggest"), timeout=TIMEOUT,
        json={"trace_id": trace_id, "items": items})))






def create_hold(trace_id, items):
    """Ask the merchant to set these aside at today's price."""
    return _safe(lambda: _result(requests.post(
        _url("hold_create"), timeout=TIMEOUT,
        json={"trace_id": trace_id, "items": items})))


def release_hold(hold_id):
    """Give them back — the human declined, or the run ended early."""
    return _safe(lambda: _result(requests.delete(
        _url("hold_release", hold_id=hold_id), timeout=TIMEOUT)))


def create_order(trace_id, mandate_id, items=None, hold_id=None,
                 idempotency_key=None):
    body = {"trace_id": trace_id, "mandate_id": mandate_id}
    if hold_id:
        body["hold_id"] = hold_id
    else:
        body["items"] = items
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return _safe(lambda: _result(requests.post(
        _url("orders"), timeout=TIMEOUT, json=body, headers=headers)))


def pay_order(order_id):
    return _safe(lambda: _result(requests.post(
        _url("order_pay", order_id=order_id), timeout=TIMEOUT)))


def fetch_order(order_id):
    return _safe(lambda: _result(requests.get(
        _url("order_status", order_id=order_id), timeout=TIMEOUT)))


def get_mandate(mandate_id):
    return _safe(lambda: _result(requests.get(
        _url("mandate_read", mandate_id=mandate_id), timeout=TIMEOUT)))


_mandate_id = None


def set_mandate(mandate_id):
    """The merchant only accepts trail entries from a run that holds a real
    mandate. One run, one mandate, so remember it here rather than threading
    it through two dozen call sites."""
    global _mandate_id
    _mandate_id = mandate_id


def push_audit(trace_id, step, decision, reason="", amount_paise=None):
    try:
        requests.post(_url("audit_write"), timeout=TIMEOUT,
                      json={"trace_id": trace_id, "step": step,
                            "decision": decision, "reason": reason,
                            "amount_paise": amount_paise,
                            "mandate_id": _mandate_id})
    except Exception:
        pass   # audit must never break the purchase