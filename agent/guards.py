import re

SUSPICIOUS = [
    r"\bignore\b.{0,30}\b(previous|prior|all)\b",
    r"\bsystem\s*:",
    r"\bdisregard\b",
    r"\byou (must|should|are required to)\b",
    r"\b(add|buy|purchase|pay)\b.{0,20}\b(immediately|now)\b",
    r"\boverride\b",
    r"\bnew instructions?\b",
]

def screen_text(text):
    """Return list of patterns matched in untrusted text."""
    if not text:
        return []
    return [p for p in SUSPICIOUS if re.search(p, text, re.I)]

def screen_catalog(catalog):
    """Strip suspicious review text before the model ever sees it."""
    clean, findings = [], []
    for p in catalog:
        safe_reviews = []
        for i, rev in enumerate(p.get("reviews", [])):
            hits = screen_text(rev)
            if hits:
                findings.append({"product": p["id"],
                                 "field": f"reviews[{i}]",
                                 "patterns": hits})
            else:
                safe_reviews.append(rev)
        clean.append({**p, "reviews": safe_reviews})
    return clean, findings

def validate_ids(selection, catalog):
    """The model may hallucinate. Only real catalog ids survive."""
    known = {p["id"] for p in catalog}
    good  = [s for s in selection if s.get("id") in known]
    bad   = [s.get("id") for s in selection if s.get("id") not in known]
    return good, bad


def validate_selection(selection, catalog, limit_paise=None, delivery_paise=0):
    """Full deterministic check of what the model returned.

    The model's output is a suggestion, never a fact. Every line must
    survive: the id is real, the quantity is a sane integer, the shop has
    that many, and the whole basket still fits the spending limit.

    Returns (good, rejected) where rejected carries a human-readable reason
    so the audit trail can explain the refusal.
    """
    by_id    = {p["id"]: p for p in catalog}
    good, rejected, running = [], [], 0

    for s in selection or []:
        pid = s.get("id")
        qty = s.get("qty")

        p = by_id.get(pid)
        if p is None:
            rejected.append({"id": pid, "reason": "not in the catalog"})
            continue

        if not isinstance(qty, int) or isinstance(qty, bool):
            rejected.append({"id": pid, "reason": f"quantity {qty!r} is not a whole number"})
            continue
        if qty < 1:
            rejected.append({"id": pid, "reason": f"quantity {qty} is below 1"})
            continue
        if qty > p["stock"]:
            rejected.append({"id": pid,
                             "reason": f"asked {qty}, only {p['stock']} in stock"})
            continue

        subtotal = p["price_paise"] * qty
        if limit_paise is not None and running + subtotal + delivery_paise > limit_paise:
            rejected.append({
                "id": pid,
                "reason": (f"{qty} x Rs {p['price_paise']/100:.0f} would take the "
                           f"basket past the Rs {limit_paise/100:.0f} limit")})
            continue

        running += subtotal
        good.append({"id": pid, "qty": qty})

    return good, rejected