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