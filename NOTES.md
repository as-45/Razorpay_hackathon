# Build notes

## Day 1 — 25 Aug

Goal: prove the Razorpay test-mode payment leg works before building anything else.

**Order creation** — worked first try. Amounts are integer paise.

**Payment Links** — worked. Server-side link creation, returns a short URL.
This settles the architecture: the agent creates a link and polls for status,
so no browser automation is needed on the agent's side.

**Problem: test card rejected.**
Default Visa test card 4111 1111 1111 1111 failed with
"International cards are not supported".
Fix: used the domestic Mastercard test card 5267 3181 8797 5449.
OTP 1111 on the simulated page.

**Problem: duplicate reference_id.**
Re-running the link script failed with
"payment link with given reference_id: trace_001 already exists".
Razorpay enforces uniqueness on reference_id.
Fix: generate a fresh id per run with uuid4.
Worth reusing later as a natural idempotency key so a retried
order request cannot create two payments.

**Result:** payment completed, ID TTuJAYpA21pGay, INR 1,640.