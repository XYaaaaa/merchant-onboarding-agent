# Example run: DeepSeek on one eval case

**中文简介：** 用 DeepSeek（`deepseek-v4-pro`）跑的第一个真实用例的完整记录：Agent 做了什么、提交了哪些缺陷、每条怎么被打分，以及这次运行暴露的问题和修复。

This is the first run against a real model. It is one case, so it shows how the agent
works; it says nothing about its overall accuracy. It ran **before** the fixes it led to.

| | |
|---|---|
| Case | `single-B08`: merchant `nova_telecom`, seeded bug **B08** (refunds go through although the merchant has `refund_allowed: false`) |
| Model | DeepSeek `deepseek-v4-pro` |
| Turn limit | 40 |
| Cost | 638k input tokens (623k cache hits), 21k output tokens, 4.1 minutes |
| Raw data | [results.jsonl](../evals/runs/pilot-1-before-fixes/results.jsonl) (the grade), [full transcript](../evals/runs/pilot-1-before-fixes/traces/single-B08_rep0.json) (all 62 tool calls and results) |

## What the agent did

The agent was told only the merchant id. Everything below it decided on its own.

| Calls | What it did |
|---|---|
| 1–5 | Read the API spec, then the config of `nova_telecom` **and** of the other two merchants |
| 6–20 | Tested payment creation: missing / unknown / suspended merchant, payment method not enabled, currency not enabled, amount over the limit, amount 0, idempotency replay and conflict |
| 21–32 | Tested capture and void: partial capture, capturing twice, capturing more than authorized, amount 0, voiding twice, capturing a voided payment |
| 33–37 | Isolation: created a payment as `acme_utilities` and tried to read, capture, void and refund it as `nova_telecom` |
| 38–41 | Refunds for `nova_telecom` on payments in different states, and webhook deliveries. **Call 38 hit the seeded bug:** a refund on a captured payment returned `201 Created` instead of `403` |
| 42–47 | Cross-checked refunds on `acme_utilities` (refunds enabled there): partial refunds and the refund total limit |
| 48–59 | Negative amounts, one batch run of pytest, more refund edge cases |
| 60–62 | Filed three findings with `submit_finding` |

It then ran out of turns before writing the report.

## The findings and how they were graded

Each finding carries a pytest test. The grader re-ran each test on a fresh **clean** build
(must pass) and on a build with **B08** on (must fail).

| # | Finding | Clean build | B08 build | Verdict |
|---|---|---|---|---|
| 1 | R8: `nova_telecom` has refunds disabled, but a refund on a captured payment succeeds | pass | fail | **Valid, catches B08** |
| 2 | R7: capture with amount 0 or −1 returns `invalid_request`, not `capture_exceeds_authorized` | fail | fail | **False positive** |
| 3 | R8: refund on an authorized (uncaptured) payment returns `invalid_state`, not `refund_not_allowed` | pass | fail | **Valid, catches B08** (second symptom of the same bug) |

## Score

| Metric | Result |
|---|---|
| Seeded bugs found | 1 / 1 |
| False positives | 1 of 3 findings |
| Verdict | none: the run ended before the report |
| Case passed | no |

## What this run taught us, and what changed

| Problem | Cause | Fix |
|---|---|---|
| No report | The agent probed one request at a time (53 `api_request` calls, only 1 `run_pytest`) and used up its 40 turns | The instructions now say to get coverage from batch pytest runs, state the turn budget, and the last turn is reserved for the report |
| Finding 2 counted as a false positive | **The spec was unclear, not the agent.** R7 never said what a non-positive amount should return, so the agent's reading was reasonable | R7 and R8 now say it; the reference checks cover it |
| Finding 3 depends on check order | The spec did not say whether the refund flag is checked before the payment state | R8 now says the flag comes first |

The agent found a real gap in the spec that was not one of the seeded bugs. The eval scored it
as a false positive, which is the known limit of grading against seeded bugs only
(see [evals/README.md](../evals/README.md)).

The same case has not been re-run since the fixes.
