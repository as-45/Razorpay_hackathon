import hmac, hashlib, os

SECRET = os.getenv("MANDATE_SECRET", "dev-secret-change-me").encode()

def sign(agent_id, max_amount_paise, allowed_categories, expires_at):
    payload = f"{agent_id}|{max_amount_paise}|{','.join(sorted(allowed_categories))}|{expires_at}"
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()

def verify(m):
    expected = sign(m.agent_id, m.max_amount_paise,
                    m.allowed_categories, m.expires_at.isoformat())
    return hmac.compare_digest(expected, m.signature)