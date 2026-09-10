from typing import TypedDict, Optional

class ShopState(TypedDict, total=False):
    instruction:    str            # what the user said
    trace_id:       str
    mandate_id:     str
    cap_paise:      int            # agent's own copy, for fast-fail
    parsed:        dict           # parsed instruction, e.g. budget
    catalog:        list           # raw from merchant
    screened:       list           # after guards.py cleans it
    findings:       list           # suspicious content found
    allowed_categories: list
    delivery_paise: int            # discovered from the merchant manifest
    spent_paise:     int           # already committed against this mandate
    remaining_paise: int           # what the mandate has left to spend
    suggestion:          dict        # extra the shop offered, if any survived
    accepted_suggestion: bool        # whether the human took it
    hold_id:          Optional[str]  # basket reserved at a frozen price
    hold_expires_at:  Optional[str]
    hold_ttl_seconds: Optional[int]
    hold_expires_at:  Optional[str]
    hold_ttl_seconds: Optional[int]  
    selection:      list           # [{"id": "...", "qty": n}]
    quote:          dict           # merchant's authoritative total

    approved:       bool
    order_id:       Optional[str]
    payment_url:    Optional[str]
    status:         str            # running | refused | paid
    refusal_reason: Optional[str]
    notes:          list           # agent-side audit lines
    budget_scope: str
    fallback: dict