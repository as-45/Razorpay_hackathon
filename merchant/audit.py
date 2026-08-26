from .models import AuditLog

def log(db, trace_id, actor, step, decision, reason="", amount_paise=None):
    db.add(AuditLog(trace_id=trace_id, actor=actor, step=step,
                    decision=decision, reason=reason,
                    amount_paise=amount_paise))
    db.commit()