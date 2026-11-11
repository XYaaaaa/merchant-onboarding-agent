"""Merchant onboarding test agent.

Claude reads the payment API spec, writes pytest tests for one merchant,
runs them, investigates failures, and writes a go-live readiness report.
Every action goes through the MCP server; the agent has no other access.

Usage:
    python -m onboarding.agent.run --merchant acme_utilities
    python -m onboarding.agent.run --dry-run        # check the MCP wiring, no model call
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")

SYSTEM = """\
You are working with a forward deployed engineer at a payment platform. A merchant is being \
onboarded and must not go live until we know the platform behaves correctly for that merchant's \
configuration. Your job is to find out, by testing the sandbox, and to write the readiness report.

The API spec (get_api_spec) contains numbered business rules. They are the contract. The \
merchant's configuration (get_merchant_config) decides how those rules apply to this merchant. \
Other merchants exist on the same sandbox; use them where a rule is about isolation between merchants.

How to work:
- Write pytest tests that cover every rule from this merchant's point of view, including edge \
values (limits, replays, wrong states), and run them with run_pytest.
- A failing test is only a lead. Before reporting it, reproduce it with api_request and decide \
whether the API broke a rule or your test was wrong. Fix wrong tests and rerun.
- The sandbox keeps state between runs, so use fresh idempotency keys each time.
- Only report a defect you have reproduced. If a rule could not be tested, say so.

Finish with a Markdown report for the merchant's go-live review:
1. Verdict: READY or NOT READY, in one line, with the main reason.
2. Findings: one entry per defect, with the rule id, what was expected, what happened, and the \
exact request sequence that reproduces it.
3. Coverage: each rule id, and whether it passed, failed, or was not tested.
4. Open questions for the merchant or the platform team, if any.
"""


def server_params(api_url: str) -> StdioServerParameters:
    # The MCP server (and the tests it runs) never needs the model API key.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["PAYMENT_API_URL"] = api_url
    return StdioServerParameters(command=sys.executable, args=["-m", "onboarding.mcp_server.server"], env=env)


async def run(merchant: str, model: str, api_url: str, max_iterations: int, out_dir: Path, dry_run: bool) -> int:
    async with stdio_client(server_params(api_url)) as (read, write):
        async with ClientSession(read, write) as mcp_client:
            await mcp_client.initialize()
            mcp_tools = (await mcp_client.list_tools()).tools
            print(f"MCP tools: {', '.join(t.name for t in mcp_tools)}")
            if dry_run:
                health = await mcp_client.call_tool("api_request", {"method": "GET", "path": "/health"})
                if health.is_error or '"status": 200' not in health.content[0].text:
                    print(f"Sandbox API is not reachable at {api_url}", file=sys.stderr)
                    return 1
                print("Dry run OK: MCP server and sandbox API are reachable.")
                return 0

            client = AsyncAnthropic()
            runner = client.beta.messages.tool_runner(
                model=model,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                # If the model declines a request, the API retries it on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM,
                tools=[async_mcp_tool(t, mcp_client) for t in mcp_tools],
                max_iterations=max_iterations,
                messages=[{"role": "user", "content": f"Merchant to onboard: {merchant}. Test it and write the report."}],
            )

            last = None
            tokens_in = tokens_out = 0
            async for message in runner:
                last = message
                tokens_in += message.usage.input_tokens
                tokens_out += message.usage.output_tokens
                for block in message.content:
                    if block.type == "tool_use":
                        detail = block.input.get("path") or block.input.get("merchant_id") or ""
                        print(f"  -> {block.name} {detail}".rstrip())

            if last is None:
                print("The model returned nothing.", file=sys.stderr)
                return 1
            if last.stop_reason == "refusal":
                print("The model declined this request.", file=sys.stderr)
                return 1

            report = "\n".join(b.text for b in last.content if b.type == "text").strip()
            if last.stop_reason != "end_turn":
                report += f"\n\n> Note: the run stopped early (stop_reason: {last.stop_reason}). The report may be incomplete."

            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{merchant}-{datetime.now():%Y%m%d-%H%M%S}.md"
            path.write_text(report + "\n")
            print(f"\n{report}\n\nSaved to {path}")
            print(f"Tokens: {tokens_in} in / {tokens_out} out, model: {last.model}")
            return 0


def _leaf(group: BaseExceptionGroup) -> BaseException:
    """First real exception inside a (possibly nested) exception group."""
    exc: BaseException = group
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--merchant", default="acme_utilities")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-url", default=os.environ.get("PAYMENT_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--max-iterations", type=int, default=40)
    parser.add_argument("--out", type=Path, default=Path("reports"))
    parser.add_argument("--dry-run", action="store_true", help="Check the MCP wiring without calling the model")
    args = parser.parse_args()

    code, error = 1, None
    try:
        code = asyncio.run(run(args.merchant, args.model, args.api_url, args.max_iterations, args.out, args.dry_run))
    # The MCP session runs in task groups, so API errors arrive wrapped in an ExceptionGroup.
    except* anthropic.AuthenticationError:
        error = "No valid Anthropic credentials. Set ANTHROPIC_API_KEY and try again."
    except* anthropic.RateLimitError:
        error = "Rate limited by the Anthropic API. Wait a moment and try again."
    except* anthropic.APIConnectionError:
        error = "Could not reach the Anthropic API. Check your network."
    except* anthropic.APIStatusError as group:
        error = f"Anthropic API error: {_leaf(group)}"
    except* anthropic.AnthropicError as group:
        error = f"Anthropic client error: {_leaf(group)}"
    except* TypeError as group:
        # The SDK raises TypeError when no credentials can be found at all.
        if "authentication" not in str(_leaf(group)):
            raise
        error = "No Anthropic credentials found. Set ANTHROPIC_API_KEY and try again."
    sys.exit(error or code)

if __name__ == "__main__":
    main()
