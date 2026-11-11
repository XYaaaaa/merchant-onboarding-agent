"""Claude backend: Anthropic SDK tool runner over the MCP tools."""
from __future__ import annotations

import json

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool

DEFAULT_MODEL = "claude-opus-5-5"


async def drive(*, model, system, user_prompt, mcp_tools, session, max_iterations, trace, usage, on_tool_call):
    """Run the tool loop. Returns (report, stop_reason, served_model, tool_calls)."""
    client = AsyncAnthropic()
    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        # The conversation grows every turn; cache the shared prefix.
        cache_control={"type": "ephemeral"},
        # If the model declines a request, the API retries it on a fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=system,
        tools=[async_mcp_tool(t, session) for t in mcp_tools],
        max_iterations=max_iterations,
        messages=[{"role": "user", "content": user_prompt}],
    )

    last, tool_calls = None, 0
    async for message in runner:
        last = message
        for key in usage:
            usage[key] += getattr(message.usage, key, None) or 0
        for block in message.content:
            if block.type == "text" and block.text.strip():
                trace.append({"role": "assistant", "content": block.text})
            elif block.type == "tool_use":
                tool_calls += 1
                trace.append({"role": "tool_call", "name": block.name,
                              "content": json.dumps(block.input, indent=2, ensure_ascii=False)})
                if on_tool_call:
                    on_tool_call(block.name, block.input)

    if last is None:
        raise RuntimeError("The model returned no message")
    report = "\n".join(b.text for b in last.content if b.type == "text").strip()
    return report, last.stop_reason, last.model, tool_calls
