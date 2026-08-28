from .db import SessionLocal
from .models import AuditLog

def log(db, trace_id, actor, step, decision, reason="", amount_paise=None):
    s = SessionLocal()
    try:
        s.add(AuditLog(trace_id=trace_id, actor=actor, step=step,
                       decision=decision, reason=reason,
                       amount_paise=amount_paise))
        s.commit()
    finally:
        s.close()