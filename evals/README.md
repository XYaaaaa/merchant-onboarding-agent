# Eval: does the agent find the seeded bugs?

**Status: harness built and self-tested. No model run yet, so there is no score to report.**

## What is measured

One case = one merchant to onboard + a hidden set of seeded bugs. The agent runs a full
session against its own private sandbox and files each defect with `submit_finding`.

| Metric | Meaning |
|---|---|
| **Seeded bugs found (recall)** | bugs caught by at least one valid finding / bugs switched on |
| **False positives** | findings that do not hold up when replayed |
| **Clean runs with a false alarm** | runs with no bugs on where the agent still filed something |
| **Verdict right** | `NOT READY` when bugs are on, `READY` when none are |
| **Cases fully right** | all bugs found, no false positives, verdict right |
| Tokens, time, tool calls | recorded per case |

## How a finding is graded: replay, not trust

The agent's report is not taken at its word. Every finding must carry a pytest test, and
the grader runs that test on fresh sandboxes:

| Run on | Must | Otherwise |
|---|---|---|
| Clean build (no bugs) | pass | the test itself is wrong → false positive |
| The case's build | fail | it shows no defect → false positive |
| Each seeded bug alone | — | tells us which bug the finding catches |

No LLM judge, so grading is deterministic and free. Only the verdict line is read from the report.

## Cases ([cases.yaml](cases.yaml))

15 cases, 19 seeded-bug instances:

- **10 single-bug** — one per bug, on a merchant where the bug can be observed
- **3 multi-bug** — three bugs on at once
- **2 clean** — no bugs; measures false alarms

## The seeded bugs (answer key)

The API is correct by default; `SEEDED_BUGS=B02,B08` switches defects on
(`src/onboarding/api/bugs.py`). The agent only reaches the API through MCP, so it cannot see this.

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

Some bugs only show for certain merchants (B08 needs refunds disabled; B02 needs them
enabled). That is deliberate: it tests whether the agent reads the merchant's configuration.

## Running it

Check the harness itself (about a minute, no model calls, no API key):

```bash
uv run python evals/run_eval.py --selftest
```

It pushes three stand-in agents through the real grader and checks the scores:
a perfect one (must get 15/15 and 19/19), an empty one (0/19), and a noisy one
(every wrong finding must be counted).

Try one case first, then the full set (needs `ANTHROPIC_API_KEY`; costs tokens):

```bash
uv run python evals/run_eval.py --cases single-B08 --yes
```

```bash
uv run python evals/run_eval.py --yes
```

Useful flags: `--reps 3` (stability), `--model`, `--concurrency`, `--variant v1`
(keep a changed prompt's results next to `baseline`).

Results land in `evals/runs/<variant>/`: `results.jsonl` (one row per case run),
`traces/` (full transcripts), `errors.jsonl` (runs that failed to execute; these are
never scored as agent failures). A stopped run resumes where it left off.

## Limits worth knowing

- **Small set.** 19 bug instances: a perfect score still has a 95% interval of roughly
  83–100%. Treat differences of a bug or two as noise; use `--reps 3` before comparing prompts.
- **Synthetic bugs.** They are the kinds of defects payment APIs really have, but I wrote
  both the bugs and the rules, so the spec is unusually clear. Real specs are vaguer.
- **Recall is per seeded bug.** A real defect the agent finds that I did not seed would
  be counted as a false positive. The clean build passing all 12 reference checks is the
  guard against that.
- **Generated tests run on your machine** unless you use Docker.
