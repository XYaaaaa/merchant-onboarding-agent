"""Merchant onboarding test agent.

Claude reads the payment API spec, writes pytest tests for one merchant,
runs them, investigates failures, files each confirmed defect with a
regression test, and writes a go-live readiness report.
Every action goes through the MCP server; the agent has no other access.

The model provider is picked from the environment: DeepSeek when
DEEPSEEK_API_KEY is set, otherwise Claude (ANTHROPIC_API_KEY).

Usage:
    python -m onboarding.agent.run --merchant acme_utilities
    python -m onboarding.agent.run --dry-run        # check the MCP wiring, no model call
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
import openai
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from . import anthropic_backend, deepseek_backend

BACKENDS = {"anthropic": anthropic_backend, "deepseek": deepseek_backend}


def default_provider() -> str:
    """DeepSeek when its key is set, otherwise Claude. AGENT_PROVIDER overrides both."""
    chosen = os.environ.get("AGENT_PROVIDER")
    if chosen:
        if chosen not in BACKENDS:
            raise SystemExit(f"AGENT_PROVIDER must be one of {sorted(BACKENDS)}")
        return chosen
    return "deepseek" if os.environ.get("DEEPSEEK_API_KEY") else "anthropic"


def default_model(provider: str) -> str:
    return os.environ.get("AGENT_MODEL") or BACKENDS[provider].DEFAULT_MODEL

SYSTEM = """\
You are working with a forward deployed engineer at a payment platform. A merchant is being \
onboarded and must not go live until we know the platform behaves correctly for that merchant's \
configuration. Your job is to find out, by testing the sandbox, and to write the readiness report.

The API spec (get_api_spec) contains numbered business rules. They are the contract. The \
merchant's configuration (get_merchant_config) decides how those rules apply to this merchant. \
Other merchants exist on the same sandbox; use them where a rule is about isolation between merchants.

How to work:
- Get coverage from pytest, not from one request at a time: write a test file that covers every \
rule from this merchant's point of view, including edge values (limits, replays, wrong states), and \
run it with run_pytest. One file can check dozens of things in a single turn.
- A failing test is only a lead. Use api_request to look closer at failures, then decide whether \
the API broke a rule or your test was wrong. Fix wrong tests and rerun.
- Before filing, read the rule again word for word. A response that differs from what you expected \
but matches the rule's text is not a defect.
- The sandbox keeps state between runs, so use fresh idempotency keys each time.
- File every confirmed defect with submit_finding, one defect per finding. Its regression test is \
what the platform team will run to verify the fix, and it is re-run on a fresh sandbox, so it must \
pass on a correct API and fail only because of that defect. A defect that is not filed does not count.
- Only report a defect you have reproduced. If a rule could not be tested, say so.

You have a limited number of turns, stated in the task. Budget them: the report matters more than \
one more probe, so stop testing while you still have turns left to write it.

Finish with a Markdown report for the merchant's go-live review. Start it with a line that is \
exactly "Verdict: READY" or "Verdict: NOT READY", then:
1. The main reason for the verdict, in one or two sentences.
2. Findings: one entry per filed defect, with the rule id, what was expected, what happened, and \
the request sequence that reproduces it.
3. Coverage: each rule id, and whether it passed, failed, or was not tested.
4. Open questions for the merchant or the platform team, if any.
"""


class MissingCredentials(Exception):
    pass


@dataclass
class AgentResult:
    report: str
    stop_reason: str | None
    model: str
    usage: dict[str, int]
    tool_calls: int
    latency_s: float
    trace: list[dict] = field(default_factory=list)


class _RecordingSession:
    """Wraps the MCP session so every tool result lands in the trace."""

    def __init__(self, session: ClientSession, trace: list[dict]):
        self._session, self._trace = session, trace

    async def call_tool(self, name: str, arguments: dict | None = None, **kwargs):
        result = await self._session.call_tool(name=name, arguments=arguments, **kwargs)
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        self._trace.append({"role": "tool_result", "content": text})
        return result


def server_params(api_url: str, findings_dir: Path) -> StdioServerParameters:
    # The MCP server (and the tests it runs) never needs the model API key.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["PAYMENT_API_URL"] = api_url
    env["FINDINGS_DIR"] = str(findings_dir)
    return StdioServerParameters(command=sys.executable, args=["-m", "onboarding.mcp_server.server"], env=env)


async def run_agent(
    merchant: str,
    api_url: str,
    findings_dir: Path,
    model: str | None = None,
    max_iterations: int = 50,
    on_tool_call=None,
    provider: str | None = None,
) -> AgentResult:
    """Run the agent once for one merchant. Filed findings are written to findings_dir."""
    started = time.monotonic()
    provider = provider or default_provider()
    if provider == "deepseek" and not os.environ.get("DEEPSEEK_API_KEY"):
        raise MissingCredentials("DEEPSEEK_API_KEY is not set.")
    model = model or default_model(provider)
    user_prompt = (f"Merchant to onboard: {merchant}. Test it and write the report. "
                   f"You have at most {max_iterations} turns; a turn may contain several tool calls.")
    trace: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt}]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    async with stdio_client(server_params(api_url, findings_dir)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            report, stop_reason, served_model, tool_calls = await BACKENDS[provider].drive(
                model=model, system=SYSTEM, user_prompt=user_prompt, mcp_tools=mcp_tools,
                session=_RecordingSession(session, trace), max_iterations=max_iterations,
                trace=trace, usage=usage, on_tool_call=on_tool_call,
            )

    return AgentResult(report=report, stop_reason=stop_reason, model=served_model, usage=usage,
                       tool_calls=tool_calls, latency_s=round(time.monotonic() - started, 1), trace=trace)


async def dry_run(api_url: str) -> int:
    async with stdio_client(server_params(api_url, Path("reports/findings"))) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"MCP tools: {', '.join(t.name for t in tools)}")
            health = await session.call_tool("api_request", {"method": "GET", "path": "/health"})
            if health.is_error or '"status": 200' not in health.content[0].text:
                print(f"Sandbox API is not reachable at {api_url}", file=sys.stderr)
                return 1
    print("Dry run OK: MCP server and sandbox API are reachable.")
    return 0


async def run_cli(args: argparse.Namespace) -> int:
    if args.dry_run:
        return await dry_run(args.api_url)

    stamp = f"{args.merchant}-{datetime.now():%Y%m%d-%H%M%S}"
    findings_dir = (args.out / f"{stamp}-findings").resolve()

    def show(name: str, tool_input: dict) -> None:
        detail = tool_input.get("path") or tool_input.get("title") or tool_input.get("merchant_id") or ""
        print(f"  -> {name} {detail}".rstrip())

    provider = args.provider or default_provider()
    print(f"Provider: {provider}, model: {args.model or default_model(provider)}")
    result = await run_agent(args.merchant, args.api_url, findings_dir, args.model, args.max_iterations, show, provider)
    if result.stop_reason == "refusal":
        print("The model declined this request.", file=sys.stderr)
        return 1

    report = result.report
    if result.stop_reason != "end_turn":
        report += f"\n\n> Note: the run stopped early (stop_reason: {result.stop_reason}). The report may be incomplete."
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{stamp}.md"
    path.write_text(report + "\n")
    filed = len(list(findings_dir.glob("finding_*.json"))) if findings_dir.exists() else 0
    print(f"\n{report}\n\nReport: {path}\nFindings filed: {filed}" + (f" ({findings_dir})" if filed else ""))
    print(f"Tokens: {result.usage['input_tokens']} in / {result.usage['output_tokens']} out, "
          f"{result.tool_calls} tool calls, {result.latency_s}s, model: {result.model}")
    return 0


def leaf_exception(group: BaseException) -> BaseException:
    """First real exception inside a (possibly nested) exception group."""
    exc = group
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--merchant", default="acme_utilities")
    parser.add_argument("--provider", choices=sorted(BACKENDS),
                        help="Default: deepseek if DEEPSEEK_API_KEY is set, else anthropic")
    parser.add_argument("--model", help="Default: the provider's default model")
    parser.add_argument("--api-url", default=os.environ.get("PAYMENT_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--max-iterations", type=int, default=50)
    parser.add_argument("--out", type=Path, default=Path("reports"))
    parser.add_argument("--dry-run", action="store_true", help="Check the MCP wiring without calling the model")
    args = parser.parse_args()

    code, error = 1, None
    try:
        code = asyncio.run(run_cli(args))
    # The MCP session runs in task groups, so API errors arrive wrapped in an ExceptionGroup.
    except* MissingCredentials as group:
        error = str(leaf_exception(group))
    except* openai.AuthenticationError:
        error = "DeepSeek rejected the API key. Check DEEPSEEK_API_KEY and try again."
    except* openai.APIError as group:
        error = f"DeepSeek API error: {leaf_exception(group)}"
    except* anthropic.AuthenticationError:
        error = "No valid Anthropic credentials. Set ANTHROPIC_API_KEY and try again."
    except* anthropic.RateLimitError:
        error = "Rate limited by the Anthropic API. Wait a moment and try again."
    except* anthropic.APIConnectionError:
        error = "Could not reach the Anthropic API. Check your network."
    except* anthropic.APIStatusError as group:
        error = f"Anthropic API error: {leaf_exception(group)}"
    except* anthropic.AnthropicError as group:
        error = f"Anthropic client error: {leaf_exception(group)}"
    except* TypeError as group:
        # The SDK raises TypeError when no credentials can be found at all.
        if "authentication" not in str(leaf_exception(group)):
            raise
        error = "No model credentials found. Set DEEPSEEK_API_KEY or ANTHROPIC_API_KEY and try again."
    sys.exit(error or code)


if __name__ == "__main__":
    main()
