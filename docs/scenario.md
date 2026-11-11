# Customer scenario

*A made-up but realistic engagement. It is the "why" behind this project.*

## Situation

**PayBridge** is a payment platform. Each merchant gets its own configuration:
payment methods, currencies, amount limit, refund policy, webhook endpoint.

Onboarding a merchant takes PayBridge's forward deployed engineers **about two weeks**.
Most of that time goes to one loop: read the merchant's requirements, set up the
configuration, hand-write integration tests, find out which failures are platform
defects and which are config mistakes, and write up a go-live decision.

In the last quarter, two merchants went live with problems that testing should have caught:

- A telecom merchant with refunds disabled discovered that refunds still went through.
- A utilities merchant was double-charged after a network retry.

## Goal

Cut the test-and-verify part of onboarding from days to under an hour, without
lowering the bar for go-live.

## Success metrics

| Metric | Target |
|---|---|
| Seeded defects found by the agent (recall) | >= 80% |
| Reported defects that are not real (false positives) | <= 10% |
| Time from "config ready" to readiness report | < 30 minutes |
| Report usable by a non-engineer for the go/no-go call | Yes |

## Constraints

- **The agent never touches production.** It works only in the sandbox, through an MCP
  server that exposes six tools.
- **No real payment data.** Payers are opaque tokens; all secrets are sandbox fakes.
- **Generated code is contained.** Tests run in a throwaway directory with a time limit and
  without the model API key; with Docker, they also run inside a container.
- **A human makes the go-live decision.** The agent writes the report; the FDE signs off.

## Stakeholders

| Who | Cares about |
|---|---|
| Merchant's integration lead | Going live on the agreed date |
| PayBridge FDE | Not shipping a broken integration; less manual testing |
| PayBridge platform team | Clear, reproducible bug reports |
| Merchant's finance lead | No double charges, no wrong refunds |

## Out of scope (for now)

- Turning a merchant's requirements document into a configuration (planned, v2)
- Diagnosing failures from service logs (planned, v3)
