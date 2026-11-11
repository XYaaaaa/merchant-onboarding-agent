# Design decisions

Why the project is built the way it is. Each entry says what was decided, what else was
considered, and what it costs. Where a choice was a judgment call rather than something
measured, it says so.

---

## 1. An agent for the testing, a fixed workflow for the eval

**Decision.** Testing a merchant is done by an agent: the model decides what to test next.
Grading is a fixed script: same steps every time, no model involved.

**Why.** What to test depends on the merchant's configuration and on what earlier tests
returned. A failing test needs follow-up whose direction can't be written down in advance.
That is the kind of task an agent is for. Grading is the opposite: it must give the same
answer every time, so it gets no freedom at all.

**Alternative.** A fixed list of checks per rule. The repo has one (`tests/checks.py`, 12
checks). It is cheaper and fully reliable, but it only finds what its author thought of.
In the pilot run the agent tested a zero amount, which those checks did not, and found a gap
in the spec.

**Cost.** Agent runs are slower, cost tokens, and vary between runs. That is why the eval exists.

---

## 2. Grading by replay, not by an LLM judge

**Decision.** The agent must file every defect with a pytest test. The grader runs that test
on a fresh clean build (it must pass) and on the case's buggy build (it must fail). Then it
runs it on each seeded bug alone to see which bug it catches.

**Why.**
- **Evidence over claims.** A finding counts only if its test proves it. The agent's report
  is not taken at its word.
- **Deterministic.** The same findings always get the same score, so two prompt versions can
  be compared without judge noise.
- **Free and checkable.** No model call to grade, and the grader itself is tested: a perfect
  stand-in agent must score 100%, an empty one 0%, a noisy one must have every wrong finding
  counted (`run_eval.py --selftest`).
- **Matches the job.** A bug report a platform team can run is what an FDE would actually hand over.

**Alternative.** An LLM judge reading the report against the answer key. Easier to build, but
it can be fooled by a confident report, costs tokens per grade, and drifts with the judge model.

**Cost.**
- The agent has to write a correct regression test, which is harder than describing a bug.
  A real defect with a badly written test is scored as a false positive.
- A real defect that was not seeded is also scored as a false positive. This happened in the
  pilot. The guard is that the clean build passes all 12 reference checks.
- Report quality (is it clear to a non-engineer?) is not graded. Only the verdict line is read.

---

## 3. The MCP server is the agent's only access

**Decision.** The agent reaches the sandbox only through six MCP tools. It cannot read the
API's source, the bug switches, or the answer key.

**Why.**
- **A fair eval.** If the agent could read `bugs.py`, finding bugs would be meaningless.
- **One place for safety rules.** `api_request` refuses paths that leave the sandbox host;
  `run_pytest` runs in a temp directory with a timeout and without model API keys. Every
  agent gets these rules because there is no other way in.
- **Swappable clients.** The same server works with the Claude backend, the DeepSeek
  backend, or any MCP client such as Claude Code, without changes.
- **Mirrors the real setting.** An FDE's agent at a customer would get scoped, audited tools
  into a sandbox, not a shell on the platform.

**Alternative.** Give the agent a shell or direct HTTP. Fewer moving parts, but no boundary,
and the eval could be gamed.

**Cost.** Every new capability needs a new tool, and tool descriptions become part of the
prompt the model reads.

---

## 4. `submit_finding` checks the test when it is filed

**Decision.** `submit_finding` runs the attached test immediately and rejects it unless it
fails with an assertion.

**Why.** It catches the cheapest mistakes (a test that passes, or does not run) while the agent
can still fix them, instead of only at grading time.

**Limit.** It only checks against the current sandbox, so it cannot tell a real defect from a
wrong test. That is what the clean-build replay in grading is for.

---

## 5. The spec is the contract, published in the OpenAPI description

**Decision.** Ten numbered business rules (R1–R10) live in the API's OpenAPI description. The
agent reads them through `get_api_spec`. Findings must name the rule they break.

**Why.** A defect needs a definition of correct. Numbered rules make findings and coverage
comparable, and keeping them in the spec the API serves means there is one source of truth.

**What the pilot showed.** The spec had gaps (what a zero amount returns; whether the refund
flag is checked before payment state). The agent hit both. Writing an unambiguous spec turned
out to be as important as the agent.

---

## 6. Seeded bugs: each breaks exactly one rule, some only for some merchants

**Decision.** Ten bugs, switched on per run. A test proves that each one makes exactly one
reference check fail and leaves the other eleven passing. Some only show for certain
configurations: B08 (refunds disabled but allowed) needs a merchant with refunds off; B03
needs a payment method the merchant has not enabled.

**Why.** "Exactly one" means a finding can be attributed to a single bug. Merchant-specific
bugs test whether the agent actually reads the merchant's configuration, which is the point
of onboarding.

**Choice of bugs.** Defects payment APIs really have: idempotency (double charges), refund
totals, state machine, tenant isolation, webhook signatures, limits.

---

## 7. Fifteen cases, including two with no bugs

**Decision.** 10 single-bug cases, 3 with three bugs at once, 2 with none.

**Why.** Single-bug cases give per-bug recall. Multi-bug cases are closer to reality and test
whether the agent stops after the first find. Clean cases are the only way to measure false
alarms: an agent that reports everything would otherwise look perfect.

**Limit.** 19 bug instances is small. Even a perfect score has a 95% interval of roughly
83–100%. The runner supports `--reps 3` to measure run-to-run variation before comparing versions.

---

## 8. A turn limit, with the last turn reserved for the report

**Decision.** At most 50 model turns per run (one turn can hold several tool calls). The
user prompt states the limit, and on the DeepSeek backend the final turn forbids tool calls,
so the agent must write its report.

**Why.** Cost and time control: a stuck agent should not run forever.

**History, honestly.** The first limit, 40, was a guess. In the pilot the agent used all 40
turns on single requests and never wrote its report. Three changes followed: the prompt now
says to get coverage from batch pytest runs, the limit is stated in the task, and the last
turn is reserved. The limit went to 50 for headroom; that number is also a judgment call, not
measured. The full eval run will show whether it is right.

---

## 9. Two model backends behind one interface

**Decision.** The agent loop has a Claude backend (Anthropic SDK tool runner) and a DeepSeek
backend (OpenAI-compatible API, hand-written loop). DeepSeek is used when `DEEPSEEK_API_KEY` is
set. Everything else (prompt, tools, eval) is shared.

**Why.** Model choice is a variable worth measuring, and customers ask about it. A shared eval
makes "is the cheaper model good enough?" a measurable question.

**Detail that mattered.** DeepSeek rejects a tool conversation unless the model's
`reasoning_content` is sent back unchanged on every later request. The backend does this, and
a test against a scripted stand-in server enforces it.

---

## 10. A trustworthy eval runner

**Decision.**
- **One private sandbox per case**, on a free port, so cases can run in parallel without
  sharing state.
- **Results written as each case finishes**, and a stopped run resumes where it left off.
- **A hard time limit per case.**
- **Failures to run are kept apart from scores** (`errors.jsonl`), so a network error is never
  counted as the agent missing a bug.
- **Missing credentials stop the run at once**, instead of logging fifteen identical errors.
- **The served model is checked** against the requested one (Claude backend), so a silent
  substitution cannot skew a comparison.
- **A consent gate:** runs that call a model need `--yes`.

**Why.** An eval is only useful if its numbers can be trusted and rerun after every change.

---

## 11. Safety choices

- Sandbox only; no production access anywhere.
- No real payment data: payers are opaque tokens, and every secret in the config is a labelled fake.
- Generated tests run without model API keys in their environment, with a timeout. With Docker,
  they also run inside a container.
- A human makes the go-live decision. The agent writes a report and files findings; it cannot
  change configuration.

---

## Open questions

- **Is the turn limit right?** Needs the full eval run.
- **Should report quality be graded?** A rubric-based judge could score clarity for a
  non-engineer reader. Left out for now to keep grading deterministic.
- **Real defects that were not seeded.** Today they count as false positives. Reviewing them by
  hand and adding confirmed ones to the answer key would fix that.
