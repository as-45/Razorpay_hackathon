from typing import TypedDict, Optional

class ShopState(TypedDict, total=False):
    instruction:    str            # what the user said
    trace_id:       str
    mandate_id:     str
    cap_paise:      int            # agent's own copy, for fast-fail

    catalog:        list           # raw from merchant
    screened:       list           # after guards.py cleans it
    findings:       list           # suspicious content found

    selection:      list           # [{"id": "...", "qty": n}]
    quote:          dict           # merchant's authoritative total

    approved:       bool
    order_id:       Optional[str]
    payment_url:    Optional[str]
    status:         str            # running | refused | paid
    refusal_reason: Optional[str]
    notes:          list           # agent-side audit lines