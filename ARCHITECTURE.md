# Architecture

A merchant that an AI buyer can purchase from unattended, with spending
authority enforced on the merchant's side.

## System

Two independent processes. The agent belongs to the buyer; the merchant
trusts nothing it sends. They communicate only over HTTP — the agent has
no access to the merchant's database or code.

```mermaid
flowchart TB
    U["User: '2 boxes kaju katli, under Rs 2000'"]

    subgraph AGENT["Shopper agent — LangGraph (buyer's side)"]
        A["parse → fetch → screen → select<br/>→ quote → precheck → approval → pay"]
    end

    subgraph MERCH["Merchant API — FastAPI (shop's side)"]
        M0["GET /.well-known/agent-catalog"]
        M1["GET /catalog"]
        M2["POST /quote — merchant prices it"]
        M3{"POST /orders — MANDATE GATE"}
        M4["POST /orders/id/pay — idempotent"]
        M5["GET /orders/id — poll"]
    end

    RZP[("Razorpay test mode")]
    DB[("sweets.db")]
    AUD[["Audit trail — both actors"]]

    U --> A
    A -.->|http| M1
    A -.->|http| M2
    A -.->|http| M3
    M3 -->|6 checks pass| M4
    M3 -->|any check fails| MX["403, reason logged"]
    M4 --> RZP
    RZP --> M5
    M5 --> A

    M1 --- DB
    M3 --- DB
    AGENT -.-> AUD
    MERCH -.-> AUD
```

## Agent graph

Generated from the code with `graph.get_graph().draw_mermaid()`.

```mermaid
graph TD;
        __start__([__start__]):::first
        parse(parse)
        fetch(fetch)
        screen(screen)
        select(select)
        quote(quote)
        precheck(precheck)
        approval(approval)
        pay(pay)
        confirm(confirm)
        refuse(refuse)
        __end__([__end__]):::last
        __start__ --> parse;
        parse --> fetch;
        fetch --> screen;
        screen --> select;
        select -.-> quote;
        select -.-> refuse;
        quote --> precheck;
        precheck -.-> approval;
        precheck -.-> refuse;
        approval -.-> pay;
        approval -.-> refuse;
        pay -.-> confirm;
        pay -.-> refuse;
        confirm --> __end__;
        refuse --> __end__;
        classDef default fill:#f2f0ff,line-height:1.2
        classDef first fill-opacity:0
        classDef last fill:#bfb6fc
```

Four of the nine nodes can route to `refuse`. Every one of them writes
its reason to the audit trail before doing so.

## Design decisions

**The mandate gate is on the merchant, not the agent.** The agent belongs
to the buyer and may be buggy, hijacked, or hostile. The merchant is the
party releasing goods, so it performs the authoritative check. The
agent's own cap check is a fast-fail only — deleting it would not make
the system unsafe. Verified by calling `POST /orders` with curl and no
agent at all: still refused.

**The merchant computes every total.** Callers send product ids and
quantities. A caller-supplied price is ignored. `price_items()` re-prices
from the database inside `/orders`, so a broken agent cannot understate a
cart.

**The LLM never touches money.** It parses intent and selects products.
Totals, limits and authorisation are deterministic code. A model cannot
be argued out of an `if` statement.

**Product text is data, not instruction.** Reviews are screened for
imperative patterns before reaching the model, and whatever survives is
passed as explicitly untrusted content.

**Money is integer paise.** No floats anywhere in the money path.

**Payment is idempotent.** One order yields one payment link, ever. A
retried request returns the existing link.