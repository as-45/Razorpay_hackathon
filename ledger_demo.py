"""Build a mandate chain, verify it, then try to cheat it.

No server, no database, no network, no phone. Just the ledger.

    python ledger_demo.py
"""
import copy
import json

from merchant import ledger as L

RUPEE = "₹"


def rs(paise):
    return f"{RUPEE}{paise / 100:,.0f}"


def line(ch="─", n=64):
    print(ch * n)


# ── the parties ────────────────────────────────────────────────────────
user_priv, user_pub = L.new_ed25519_keypair()
agent_priv, agent_pub = L.new_ed25519_keypair()
merch_priv, merch_pub = L.new_ed25519_keypair()

USER = {"alg": "Ed25519", "kid": "usr_athreya#1", "pub": user_pub}
AGENT = {"alg": "Ed25519", "kid": "agt_local#1", "pub": agent_pub}
MERCH = {"alg": "Ed25519", "kid": "sweets#k1", "pub": merch_pub}

chain = []


def append(type_, body, signers, at):
    e = L.build("mdt_7f3a91c0", len(chain), L.head(chain), type_, body, at=at)
    for role, kid, priv in signers:
        L.sign(e, role, kid, priv)
    chain.append(e)
    return e


# ── 0 · the user opens the account ─────────────────────────────────────
append(L.GRANT,
       L.grant_body(holder="agt_shopper_01", cap_paise=200000,
                    categories=["sweets"], expires_at="2026-09-20T00:00:00Z",
                    user_key=USER, holder_key=AGENT, merchant_keys=[MERCH],
                    step_up={"per_order_above_paise": 50000,
                             "always_for_delegation": True}),
       [("user", USER["kid"], user_priv)],
       "2026-09-13T09:00:00Z")

# ── 1 · a small purchase, no human involved ────────────────────────────
append(L.SPEND,
       L.spend_body("ord_5c2b", 40000, "sweets.example", ["sweets"],
                    basket_hash=L.basket_hash([{"id": "sw_002", "qty": 1}]),
                    quote_hash="sha256:77ab", hold_id="hld_9e31"),
       [("agent", AGENT["kid"], agent_priv),
        ("merchant", MERCH["kid"], merch_priv)],
       "2026-09-13T10:14:22Z")

# ── 2 · a large one, so the signed policy pulls the user back in ───────
append(L.SPEND,
       L.spend_body("ord_88f1", 120000, "sweets.example", ["sweets"],
                    basket_hash=L.basket_hash([{"id": "sw_001", "qty": 2}]),
                    quote_hash="sha256:1f0e", hold_id="hld_a201"),
       [("agent", AGENT["kid"], agent_priv),
        ("merchant", MERCH["kid"], merch_priv),
        ("user", USER["kid"], user_priv)],          # the fingerprint
       "2026-09-13T11:02:07Z")

# ── the record ─────────────────────────────────────────────────────────
line("=")
print("  THE CHAIN")
line("=")
for e in chain:
    who = " + ".join(s["role"] for s in e["sigs"])
    amount = L.delta(e)
    money = f"{'-' if amount < 0 else '+'}{rs(abs(amount))}" if amount else ""
    print(f"  {e['seq']}  {e['type']:<9} {money:>9}   signed: {who}")
    print(f"     prev  {e['prev'] or 'null'}")
    print(f"     hash  {L.entry_hash(e)}")
print()

r = L.verify(chain, user_pub=user_pub)
line()
print(f"  cap        {rs(r.cap_paise)}")
print(f"  spent      {rs(r.spent_paise)}")
print(f"  available  {rs(r.available_paise)}")
print(f"  state      {r.state}")
line()
print(f"  {r}")
print()

# ── now try to cheat ───────────────────────────────────────────────────
line("=")
print("  ATTACKS")
line("=")


def attack(name, mutate):
    forged = copy.deepcopy(chain)
    mutate(forged)
    out = L.verify(forged, user_pub=user_pub)
    verdict = "VALID  <-- PROBLEM" if out.valid else "rejected"
    print(f"  {name:<44} {verdict}")
    if not out.valid:
        print(f"     entry {out.at_entry}: {out.reason}")


def edit_amount(c):
    c[2]["body"]["amount_paise"] = 20000


def edit_and_resign(c):
    c[1]["body"]["amount_paise"] = 100
    c[1]["sigs"] = []
    L.sign(c[1], "agent", AGENT["kid"], agent_priv)
    L.sign(c[1], "merchant", MERCH["kid"], merch_priv)


def delete_entry(c):
    del c[1]


def drop_the_approval(c):
    c[2]["sigs"] = [s for s in c[2]["sigs"] if s["role"] != "user"]


def spend_beyond_the_cap(c):
    e = L.build("mdt_7f3a91c0", len(c), L.head(c), L.SPEND,
                L.spend_body("ord_x", 90000, "sweets.example", ["sweets"]),
                at="2026-09-13T12:00:00Z")
    L.sign(e, "agent", AGENT["kid"], agent_priv)
    L.sign(e, "merchant", MERCH["kid"], merch_priv)
    L.sign(e, "user", USER["kid"], user_priv)
    c.append(e)


def merchant_raises_the_cap(c):
    e = L.build("mdt_7f3a91c0", len(c), L.head(c), L.REFUND,
                {"order_id": "ord_none", "amount_paise": 500000,
                 "currency": "INR", "merchant": "sweets.example",
                 "reason": "creative accounting"},
                at="2026-09-13T12:00:00Z")
    L.sign(e, "merchant", MERCH["kid"], merch_priv)
    c.append(e)
    e2 = L.build("mdt_7f3a91c0", len(c), L.head(c), L.SPEND,
                 L.spend_body("ord_y", 400000, "sweets.example", ["sweets"]),
                 at="2026-09-13T12:30:00Z")
    L.sign(e2, "agent", AGENT["kid"], agent_priv)
    L.sign(e2, "merchant", MERCH["kid"], merch_priv)
    L.sign(e2, "user", USER["kid"], user_priv)
    c.append(e2)


def buy_something_not_allowed(c):
    e = L.build("mdt_7f3a91c0", len(c), L.head(c), L.SPEND,
                L.spend_body("ord_z", 1000, "sweets.example", ["electronics"]),
                at="2026-09-13T12:00:00Z")
    L.sign(e, "agent", AGENT["kid"], agent_priv)
    L.sign(e, "merchant", MERCH["kid"], merch_priv)
    c.append(e)


attack("change an amount", edit_amount)
attack("change it and re-sign what you can", edit_and_resign)
attack("delete an inconvenient entry", delete_entry)
attack("strip the user's approval off the big one", drop_the_approval)
attack("spend past the cap", spend_beyond_the_cap)
attack("merchant refunds itself into more budget", merchant_raises_the_cap)
attack("buy outside the allowed categories", buy_something_not_allowed)

print()
line("=")
print("  A REFUND IS NOT AN EDIT")
line("=")
append(L.REFUND,
       {"order_id": "ord_5c2b", "amount_paise": 40000, "currency": "INR",
        "merchant": "sweets.example", "reason": "payment_expired"},
       [("merchant", MERCH["kid"], merch_priv)],
       "2026-09-13T12:00:00Z")
r = L.verify(chain, user_pub=user_pub)
print(f"  the abandoned {rs(40000)} order is corrected, not erased")
print(f"  entries {r.entries}  ·  spent {rs(r.spent_paise)}  "
      f"·  available {rs(r.available_paise)}")
print()

print(f"  entry 1 is still in the chain, exactly as it was signed:")
print("  " + json.dumps({k: chain[1][k] for k in ("seq", "type", "at")},
                        separators=(", ", ": ")))
line()