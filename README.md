# Merchant Onboarding Agent

An agent that helps a payment platform's forward deployed engineer (FDE) get a new merchant
safely to go-live. An LLM (Claude or DeepSeek) reads the payment API's spec, writes integration
tests for the merchant's configuration, runs them, investigates failures, files each defect with
a regression test, and writes a readiness report.

**中文简介：** 一个帮支付平台 FDE 完成新商户接入的 Agent。大模型（Claude 或 DeepSeek）读取支付 API 规范，针对商户配置生成并运行集成测试，排查失败原因，为每个缺陷提交回归测试，最后输出《上线就绪报告》。Agent 只能通过 MCP server 访问沙箱。

## Results so far

**One pilot case has been run on a real model. The full 15-case eval has not**, so there is no
overall accuracy number yet.

| Pilot case | Result |
|---|---|
| Setup | Merchant `nova_telecom` (refunds disabled), seeded bug: refunds still go through. DeepSeek `deepseek-v4-pro` |
| Seeded bug | **Found**, on its own, at tool call 38 of 62 |
| Findings filed | 3: 2 valid (two symptoms of the bug), 1 false positive, which turned out to be **a real gap in the spec** |
| Readiness report | Not written: the agent ran out of turns (fixed since; see below) |
| Cost and time | 4.1 minutes; 638k input tokens (98% cache hits), 21k output; about ¥0.4 |

What the agent filed (reformatted to fit; the original, with its `api()` helper, is in the
[transcript](evals/runs/pilot-1-before-fixes/traces/single-B08_rep0.json)). The grader re-ran this
test on a clean build (it passes) and on the buggy build (it fails), so the finding counts:

```python
# Finding: nova_telecom has refund_allowed=false but refund on a captured payment succeeds  (rule R8)
def test_nova_refund_not_allowed_on_captured_payment():
    body = {"amount": 10000, "currency": "USD", "payment_method": "card",
            "payer_ref": "payer-" + uuid.uuid4().hex[:10]}
    r = api("POST", "/v1/payments", "nova_telecom", body, str(uuid.uuid4()))
    assert r.status_code == 201, r.text
    payment_id = r.json()["id"]

    r = api("POST", f"/v1/payments/{payment_id}/capture", "nova_telecom")
    assert r.status_code == 200, r.text

    r = api("POST", f"/v1/payments/{payment_id}/refunds", "nova_telecom", {"amount": 100})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "refund_not_allowed", r.text
```

The run exposed three problems, all fixed: the agent spent its turns on single requests
instead of batch tests, the spec did not say what a zero amount should return, and the final
turn was not reserved for the report. Full walkthrough: [docs/example-run.md](docs/example-run.md).

> Why this exists: see the [customer scenario](docs/scenario.md).
> Why it is built this way: see the [design decisions](docs/design-decisions.md).

## Status

| Part | State |
|---|---|
| Mock payment API with per-merchant config | Done, tested |
| 10 seeded bugs, each switchable | Done, tested |
| MCP server (6 tools) | Done, tested end to end over stdio |
| Test agent (Claude or DeepSeek) | Working. One real run on DeepSeek: found the seeded bug; [see the example run](docs/example-run.md) |
| Docker Compose | Written and reviewed, **not yet run** (no Docker on the machine it was built on) |
| Eval harness (recall / false positives) | [Built and self-tested](evals/README.md). One pilot case run; **full 15-case baseline not run yet** |

## Architecture

```mermaid
flowchart LR
    A[Agent<br/>Claude + tool runner] -- MCP over stdio --> M[MCP server]
    M -- get_api_spec / api_request --> P[Sandbox payment API<br/>FastAPI]
    M -- get_merchant_config --> C[(merchants.yaml)]
    M -- run_pytest --> T[Generated tests<br/>in a temp dir]
    T -- HTTP --> P
    C --> P
    A --> R[Readiness report<br/>Markdown]
```

The MCP server is the only door. The agent cannot read the API's source, the bug
switches, or the answer key.

## The sandbox payment API

Merchants are tenants: one API, different rules per merchant ([config/merchants.yaml](config/merchants.yaml)).

| Merchant | Methods | Currencies | Max amount | Refunds |
|---|---|---|---|---|
| `acme_utilities` | card, bank_transfer | USD | 500000 | allowed |
| `nova_telecom` | card, wallet | USD, EUR | 100000 | **not allowed** |
| `old_gym` | card | USD | 20000 | (suspended merchant) |

Endpoints: create (authorize) → capture / void → refund, plus signed webhooks.
Ten numbered business rules (R1–R10) are published in the OpenAPI description:
merchant auth, payment-method and currency allowlists, amount limits, idempotency,
the payment state machine, capture and refund limits, isolation between merchants,
and webhook signatures.

Terms: **merchant** = the business being onboarded (the tenant). **Payer** = the person paying.

## Quick start

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest                    # 33 tests
```

Start the sandbox (interactive docs at http://127.0.0.1:8000/docs):

```bash
uv run uvicorn onboarding.api.app:app_factory --factory --port 8000
```

In a second terminal, check the MCP wiring (no model call, no API key needed):

```bash
uv run python -m onboarding.agent.run --dry-run
```

Pick a model provider by setting its key. If `DEEPSEEK_API_KEY` is set the agent uses DeepSeek;
otherwise it uses Claude via `ANTHROPIC_API_KEY`.

| Provider | Key | Default model | Change it with |
|---|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-v4-pro` | `--model deepseek-flash` |
| Claude | `ANTHROPIC_API_KEY` | `claude-opus-5-5` | `--model ...` |

`--provider anthropic` (or `deepseek`) overrides the automatic choice.

Run the agent (this calls the model and costs tokens):

```bash
uv run python -m onboarding.agent.run --merchant nova_telecom
```

The report is printed and saved under `reports/`. Each defect is also filed with a regression test.

To give the agent something to find, start the sandbox with seeded bugs:

```bash
SEEDED_BUGS=B02,B08 uv run uvicorn onboarding.api.app:app_factory --factory --port 8000
```

### Eval

How many seeded bugs does the agent find, and how often is it wrong? Findings are graded by
replaying their regression tests on clean and buggy builds. Details: [evals/README.md](evals/README.md).

```bash
uv run python evals/run_eval.py --selftest
```

### With Docker

```bash
docker compose up api
docker compose run --rm agent --merchant nova_telecom
```

## MCP tools

| Tool | What it does |
|---|---|
| `get_api_spec` | OpenAPI spec, including business rules R1–R10 |
| `list_merchants` | Merchant ids and status |
| `get_merchant_config` | One merchant's sandbox configuration |
| `api_request` | One HTTP call to the sandbox, as a given merchant |
| `run_pytest` | Run a generated pytest file against the sandbox |
| `submit_finding` | File a confirmed defect with a regression test; rejected unless the test really fails |

## Safety choices

- **Sandbox only.** `api_request` refuses any path that leaves the sandbox host.
- **No real payment data.** Payers are opaque tokens. All secrets in the config are fakes.
- **Generated code is contained.** Tests run in a temp directory, with a timeout, and
  without the model API key. Use Docker so they run in a container, not on your machine.
- **A human decides.** The agent writes a report; it does not change configuration or approve go-live.

## Layout

```
config/merchants.yaml          per-merchant configuration
src/onboarding/api/            mock payment API + seeded bugs
src/onboarding/mcp_server/     MCP server
src/onboarding/agent/          the agent, with one backend per model provider
tests/                         rule checks, seeded-bug tests, MCP end-to-end test
evals/                         eval cases, grader, runner, answer key
docs/scenario.md               the customer scenario
docs/example-run.md            a real run, step by step, with its grading
```

## Roadmap

1. **First eval run** — get the baseline numbers, then tune the prompt against them
2. **Requirements → config** — turn a merchant's messy requirements doc into a config draft, and flag gaps
3. **Log diagnosis** — when a test fails, read service logs to find the root cause
