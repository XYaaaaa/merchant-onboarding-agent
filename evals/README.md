# Eval: does the agent find the seeded bugs?

**Status: design done, harness not built yet.** The bug switches and the reference
checks exist and are tested; the script that runs the agent per bug and scores the
reports is the next step.

## How it works

The API is correct by default. `SEEDED_BUGS=B02,B08` switches on deliberate defects
(`src/onboarding/api/bugs.py`). The agent only reaches the API through MCP, so it
cannot see which bugs are on.

## Answer key

| Bug | Rule | Defect |
|---|---|---|
| B01 | R5 | Replaying an Idempotency-Key creates a second payment |
| B02 | R8 | Several partial refunds can add up to more than the captured amount |
| B03 | R2 | Payment method allowlist is not enforced |
| B04 | R9 | A merchant can read another merchant's payment |
| B05 | R6 | A captured payment can be captured again |
| B06 | R10 | Webhook signature is computed with the wrong key |
| B07 | R4 | `amount == max_amount` is rejected (off by one) |
| B08 | R8 | `refund_allowed: false` is ignored |
| B09 | R5 | Idempotency keys are shared across merchants |
| B10 | R7 | Capture amount may exceed the authorized amount |

`tests/test_seeded_bugs.py` proves each bug is detectable: it breaks exactly one
reference check and leaves the other eleven passing.

Some bugs only show for certain merchants (B08 needs a merchant with refunds
disabled; B03 needs a method the merchant has not enabled). That is deliberate:
it tests whether the agent reads the merchant's configuration.

## Planned metrics

- **Recall** - seeded bugs reported / seeded bugs switched on
- **False positives** - reported defects that match no seeded bug (run with no bugs on: should report zero)
- **Stability** - same setup, 3 runs: how much do the results vary?
- **Cost** - tokens and wall-clock time per run
