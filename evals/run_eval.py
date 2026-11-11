"""Eval: how many seeded bugs does the agent find, and how often is it wrong?

For each case (a merchant + hidden seeded bugs) the agent runs once against
its own sandbox. Grading replays the evidence: every finding the agent files
carries a pytest test, and that test is run on fresh sandboxes:

    clean build      -> must pass   (otherwise the test is wrong)
    the case's build -> must fail   (otherwise it shows no defect)
    one-bug builds   -> which seeded bug it catches

A finding that fails either of the first two is a false positive.

    python evals/run_eval.py --selftest            # check the harness, no model calls
    python evals/run_eval.py --yes                 # full run (calls the model, costs tokens)
    python evals/run_eval.py --cases single-B08 --yes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import anthropic
import uvicorn
import yaml

sys.path.insert(0, str(Path(__file__).parent))

import oracle  # noqa: E402
from onboarding.agent.run import DEFAULT_MODEL, AgentResult, leaf_exception, run_agent  # noqa: E402
from onboarding.api.app import create_app  # noqa: E402

EVALS = Path(__file__).parent
FLOW = EVALS / "runs"
GRADE_TIMEOUT_S = 120


def load_cases(only: list[str] | None = None) -> list[dict]:
    cases = yaml.safe_load((EVALS / "cases.yaml").read_text())["cases"]
    if only:
        unknown = set(only) - {c["id"] for c in cases}
        if unknown:
            raise SystemExit(f"Unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c["id"] in only]
    return cases


def case_prompt(case: dict) -> str:
    return f"Merchant to onboard: {case['merchant']}. Test it and write the report."


# ---------------------------------------------------------------- sandbox

class Sandbox:
    """A private payment API on a free local port, with the given seeded bugs on."""

    def __init__(self, bugs):
        config = uvicorn.Config(create_app(bugs=frozenset(bugs)), host="127.0.0.1", port=0, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.url = ""

    async def __aenter__(self) -> "Sandbox":
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Sandbox API did not start")
            await asyncio.sleep(0.02)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.should_exit = True
        await asyncio.to_thread(self._thread.join, 5)


# ---------------------------------------------------------------- grading

async def run_test_on(test_code: str, bugs) -> str:
    """Run one finding's test on a fresh sandbox. Returns 'pass', 'fail' or 'error'."""
    async with Sandbox(bugs) as sandbox:
        with tempfile.TemporaryDirectory(prefix="eval-grade-") as tmp:
            (Path(tmp) / "test_finding.py").write_text(test_code)
            env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
            env["PAYMENT_API_URL"] = sandbox.url
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_finding.py",
                cwd=tmp, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                code = await asyncio.wait_for(proc.wait(), GRADE_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                return "error"
    return {0: "pass", 1: "fail"}.get(code, "error")


async def grade_finding(finding: dict, bugs: list[str]) -> dict:
    code = finding["test_code"]
    on_clean = await run_test_on(code, [])
    on_case = await run_test_on(code, bugs) if bugs else on_clean
    valid = on_clean == "pass" and on_case == "fail"
    catches: list[str] = []
    if valid:
        if len(bugs) == 1:
            catches = list(bugs)
        else:
            results = await asyncio.gather(*(run_test_on(code, [b]) for b in bugs))
            catches = [b for b, outcome in zip(bugs, results) if outcome == "fail"]
    return {"rule_id": finding.get("rule_id"), "title": finding.get("title"),
            "on_clean": on_clean, "on_case": on_case, "valid": valid, "catches": catches}


VERDICT = re.compile(r"^[\s#*>_-]*verdict[\s*_:]*\s*(NOT\s+READY|READY)", re.IGNORECASE | re.MULTILINE)


def read_verdict(report: str) -> str | None:
    match = VERDICT.search(report)
    return re.sub(r"\s+", " ", match.group(1).upper()) if match else None


async def grade_case(case: dict, report: str, findings: list[dict]) -> tuple[dict, dict, dict]:
    bugs = case["bugs"]
    graded = [await grade_finding(f, bugs) for f in findings]
    found = sorted({b for g in graded for b in g["catches"]})
    false_pos = sum(not g["valid"] for g in graded)
    verdict, expected = read_verdict(report), ("NOT READY" if bugs else "READY")
    recall = len(found) / len(bugs) if bugs else 1.0
    verdict_ok = verdict == expected
    grade = {
        "case_pass": float(recall == 1.0 and false_pos == 0 and verdict_ok),
        "recall": recall,
        "false_pos": float(false_pos),
        "verdict_ok": float(verdict_ok),
    }
    missed = sorted(set(bugs) - set(found))
    explanation = {
        "recall": f"found {found or 'none'}; missed {missed or 'none'}" if bugs else "no bugs seeded",
        "false_pos": "; ".join(f"{g['title']} (clean: {g['on_clean']}, case build: {g['on_case']})"
                               for g in graded if not g["valid"]) or "none",
        "verdict_ok": f"said {verdict or 'nothing parseable'}, expected {expected}",
    }
    return grade, explanation, {"bugs": bugs, "found": found, "missed": missed, "findings": graded, "verdict": verdict}


# ---------------------------------------------------------------- agents

def read_findings(findings_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(findings_dir.glob("finding_*.json"))]


def stub_agent(kind: str, case: dict) -> tuple[AgentResult, list[dict]]:
    """Stand-ins for the model, used only to check the harness."""
    bugs = case["bugs"]
    if kind == "oracle":      # perfect agent
        findings, verdict = [oracle.finding(b) for b in bugs], ("NOT READY" if bugs else "READY")
    elif kind == "null":      # finds nothing
        findings, verdict = [], "READY"
    elif kind == "noisy":     # files one wrong finding on top of the right ones
        findings, verdict = [oracle.finding(b) for b in bugs] + [oracle.BOGUS], "NOT READY"
    else:
        raise ValueError(kind)
    report = f"Verdict: {verdict}\n\n(stub agent: {kind})"
    trace = [{"role": "user", "content": case_prompt(case)}, {"role": "assistant", "content": report}]
    return AgentResult(report, "end_turn", f"stub-{kind}", {"input_tokens": 0, "output_tokens": 0}, 0, 0.0, trace), findings


async def run_case(case: dict, agent: str, model: str, timeout_s: int, max_iterations: int):
    if agent != "claude":
        return stub_agent(agent, case)
    findings_dir = Path(tempfile.mkdtemp(prefix="eval-findings-"))
    try:
        async with Sandbox(case["bugs"]) as sandbox:
            # Hard ceiling on the whole case, however lively the connection looks.
            result = await asyncio.wait_for(
                run_agent(case["merchant"], sandbox.url, findings_dir, model, max_iterations), timeout_s)
        return result, read_findings(findings_dir)
    finally:
        shutil.rmtree(findings_dir, ignore_errors=True)


# ---------------------------------------------------------------- runner

class MissingCredentials(Exception):
    pass


def failure_class(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    return "harness_or_serving_error"


async def run_all(args, out: Path) -> list[dict]:
    cases = load_cases(args.cases)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    results_path, errors_path = out / "results.jsonl", out / "errors.jsonl"
    rows = [json.loads(line) for line in results_path.read_text().splitlines()] if results_path.exists() else []
    done = {(r["prompt_id"], r["rep"]) for r in rows}
    todo = [(c, rep) for c in cases for rep in range(args.reps) if (c["id"], rep) not in done]
    if len(done):
        print(f"Resuming: {len(done)} case runs already recorded, {len(todo)} to go")

    semaphore, write_lock = asyncio.Semaphore(args.concurrency), asyncio.Lock()

    async def one(case: dict, rep: int) -> None:
        async with semaphore:
            started = time.monotonic()
            try:
                result, findings = await run_case(case, args.agent, args.model, args.timeout_s, args.max_iterations)
                if args.agent == "claude":
                    # A silently substituted model would make the numbers meaningless.
                    if not result.model.startswith(args.model):
                        raise RuntimeError(f"served model {result.model!r} is not the requested {args.model!r}")
                    if not result.usage.get("output_tokens"):
                        raise RuntimeError("response carried no token usage")
                grade, explanation, meta = await grade_case(case, result.report, findings)
            except BaseException as group:  # noqa: BLE001 - recorded, never scored as a model failure
                if isinstance(group, (KeyboardInterrupt, asyncio.CancelledError)):
                    raise
                exc = leaf_exception(group)
                if isinstance(exc, anthropic.AuthenticationError) or (
                        isinstance(exc, TypeError) and "authentication" in str(exc)):
                    # Every case would fail the same way; stop instead of logging 15 errors.
                    raise MissingCredentials() from None
                async with write_lock:
                    with errors_path.open("a") as f:
                        f.write(json.dumps({"prompt_id": case["id"], "rep": rep, "failure_class": failure_class(exc),
                                            "error": f"{type(exc).__name__}: {exc}"[:500],
                                            "elapsed_s": round(time.monotonic() - started, 1)}) + "\n")
                print(f"  ! {case['id']} rep{rep}: {failure_class(exc)} ({type(exc).__name__})")
                return

            kind = case["id"].split("-")[0]
            if result.stop_reason == "refusal":
                meta["failure_class"] = "refusal"
            row = {
                "prompt_id": case["id"], "rep": rep, "prompt": case_prompt(case),
                "tags": [kind, case["merchant"], "+".join(case["bugs"]) or "no bugs"],
                "model": result.model, "stop_reason": result.stop_reason,
                "status": "truncated" if result.stop_reason == "max_tokens" else "ok",
                "grade": grade, "explanation": explanation,
                "usage": result.usage, "latency_s": result.latency_s, "tool_calls": result.tool_calls,
                "in_tokens": result.usage.get("input_tokens", 0), "out_tokens": result.usage.get("output_tokens", 0),
                "meta": meta,
            }
            async with write_lock:  # write as each case finishes, so a crash loses nothing
                (out / "traces" / f"{case['id']}_rep{rep}.json").write_text(
                    json.dumps(result.trace, indent=1, ensure_ascii=False))
                with results_path.open("a") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows.append(row)
            mark = "pass" if grade["case_pass"] else "FAIL"
            print(f"  {mark}  {case['id']} rep{rep}: found {len(meta['found'])}/{len(case['bugs'])}, "
                  f"false positives {int(grade['false_pos'])}, verdict {meta['verdict']}")

    await asyncio.gather(*(one(c, rep) for c, rep in todo))
    wanted = {(c["id"], rep) for c in cases for rep in range(args.reps)}
    return [r for r in rows if (r["prompt_id"], r["rep"]) in wanted]


# ---------------------------------------------------------------- summary

def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["status"] == "ok"]
    seeded = sum(len(r["meta"]["bugs"]) for r in ok)
    found = sum(len(r["meta"]["found"]) for r in ok)
    findings = sum(len(r["meta"]["findings"]) for r in ok)
    false_pos = sum(int(r["grade"]["false_pos"]) for r in ok)
    clean = [r for r in ok if not r["meta"]["bugs"]]
    return {
        "case_runs": len(ok),
        "case_pass": sum(int(r["grade"]["case_pass"]) for r in ok),
        "bugs_seeded": seeded, "bugs_found": found, "recall_ci": wilson(found, seeded),
        "findings": findings, "false_positives": false_pos,
        "clean_runs": len(clean), "clean_runs_with_false_alarm": sum(r["grade"]["false_pos"] > 0 for r in clean),
        "verdict_right": sum(int(r["grade"]["verdict_ok"]) for r in ok),
        "tokens_in": sum(r["in_tokens"] for r in ok), "tokens_out": sum(r["out_tokens"] for r in ok),
        "minutes": round(sum(r["latency_s"] for r in ok) / 60, 1),
    }


def print_summary(s: dict, errors: int) -> None:
    def pct(a, b):
        return f"{a}/{b} ({a / b:.0%})" if b else "n/a"
    lo, hi = s["recall_ci"]
    print(f"\nCase runs scored:        {s['case_runs']}" + (f"  (+{errors} failed to run, see errors.jsonl)" if errors else ""))
    print(f"Seeded bugs found:       {pct(s['bugs_found'], s['bugs_seeded'])}   95% CI {lo:.0%}-{hi:.0%}")
    print(f"False positives:         {s['false_positives']} of {s['findings']} findings filed")
    print(f"Clean runs w/ false alarm: {pct(s['clean_runs_with_false_alarm'], s['clean_runs'])}")
    print(f"Verdict right:           {pct(s['verdict_right'], s['case_runs'])}")
    print(f"Cases fully right:       {pct(s['case_pass'], s['case_runs'])}")
    if s["tokens_out"]:
        print(f"Tokens:                  {s['tokens_in']:,} in / {s['tokens_out']:,} out, {s['minutes']} agent-minutes")


# ---------------------------------------------------------------- self-test

async def selftest() -> int:
    """Push a perfect, an empty and a noisy 'agent' through the real grader."""
    cases = load_cases()
    n, seeded = len(cases), sum(len(c["bugs"]) for c in cases)
    clean = sum(not c["bugs"] for c in cases)
    expected = {
        "oracle": {"case_pass": n, "bugs_found": seeded, "false_positives": 0},
        "null": {"case_pass": clean, "bugs_found": 0, "false_positives": 0},
        "noisy": {"case_pass": 0, "bugs_found": seeded, "false_positives": n},
    }
    failed = False
    for kind, want in expected.items():
        with tempfile.TemporaryDirectory(prefix="eval-selftest-") as tmp:
            args = argparse.Namespace(cases=None, reps=1, concurrency=4, agent=kind, model=kind,
                                      timeout_s=60, max_iterations=1)
            print(f"\n[{kind}]")
            summary = summarize(await run_all(args, Path(tmp)))
        got = {k: summary[k] for k in want}
        status = "ok" if got == want else "WRONG"
        failed |= got != want
        print(f"  -> {status}: got {got}, expected {want}")
    print("\nSelf-test " + ("FAILED" if failed else "passed: the grader scores a perfect agent 100%, "
                                                  "an empty one 0%, and counts wrong findings."))
    return int(failed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--variant", default="baseline", help="'baseline' or v1, v2, ... (a folder under evals/runs/)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reps", type=int, default=1, help="Runs per case; use 3 to measure stability")
    parser.add_argument("--cases", nargs="*", help="Only these case ids")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout-s", type=int, default=1500, help="Wall-clock ceiling per case")
    parser.add_argument("--max-iterations", type=int, default=40)
    parser.add_argument("--agent", default="claude", choices=["claude", "oracle", "null", "noisy"],
                        help="Stubs (oracle/null/noisy) exercise the harness without the model")
    parser.add_argument("--yes", action="store_true", help="Confirm a run that calls the model")
    parser.add_argument("--selftest", action="store_true", help="Check the harness itself; no model calls")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(asyncio.run(selftest()))
    if not re.fullmatch(r"baseline|v\d+", args.variant):
        sys.exit("--variant must be 'baseline' or v1, v2, ...")

    cases = load_cases(args.cases)
    if args.agent == "claude" and not args.yes:
        sys.exit(f"This will run {len(cases)} cases x {args.reps} rep(s) on {args.model}, each a full agent "
                 f"session. It calls the model and costs tokens. Re-run with --yes to start.")
    if args.agent != "claude" and args.variant == "baseline":
        sys.exit("Stub agents must not write into 'baseline'. Use --selftest, or pass another --variant.")

    out = FLOW / args.variant
    print(f"{len(cases)} cases x {args.reps} rep(s), agent: {args.agent}, model: {args.model} -> {out}")
    try:
        rows = asyncio.run(run_all(args, out))
    except MissingCredentials:
        sys.exit("No valid Anthropic credentials. Set ANTHROPIC_API_KEY and try again. Nothing was scored.")
    errors_path = out / "errors.jsonl"
    errors = len(errors_path.read_text().splitlines()) if errors_path.exists() else 0
    print_summary(summarize(rows), errors)


if __name__ == "__main__":
    main()
