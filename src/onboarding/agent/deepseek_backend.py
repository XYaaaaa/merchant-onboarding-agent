"""DeepSeek backend: OpenAI-compatible chat completions with a hand-written tool loop."""
from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def _tool_spec(tool) -> dict:
    return {"type": "function", "function": {
        "name": tool.name, "description": tool.description or "", "parameters": tool.input_schema}}


async def drive(*, model, system, user_prompt, mcp_tools, session, max_iterations, trace, usage, on_tool_call):
    """Run the tool loop. Returns (report, stop_reason, served_model, tool_calls)."""
    client = AsyncOpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE_URL)
    tools = [_tool_spec(t) for t in mcp_tools]
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}]
    report, stop_reason, served_model, tool_calls = "", "max_iterations", model, 0

    for _ in range(max_iterations):
        response = await client.chat.completions.create(model=model, messages=messages, tools=tools, max_tokens=16000)
        choice, served_model = response.choices[0], response.model
        message = choice.message
        if response.usage:
            usage["input_tokens"] += response.usage.prompt_tokens or 0
            usage["output_tokens"] += response.usage.completion_tokens or 0
            usage["cache_read_input_tokens"] += getattr(response.usage, "prompt_cache_hit_tokens", None) or 0

        turn: dict = {"role": "assistant", "content": message.content or ""}
        # DeepSeek rejects a tool conversation unless the model's reasoning is sent back unchanged.
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning is not None:
            turn["reasoning_content"] = reasoning
        if message.tool_calls:
            turn["tool_calls"] = [{"id": c.id, "type": "function",
                                   "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                  for c in message.tool_calls]
        messages.append(turn)
        if (message.content or "").strip():
            trace.append({"role": "assistant", "content": message.content})

        if not message.tool_calls:
            report = (message.content or "").strip()
            stop_reason = STOP_REASONS.get(choice.finish_reason, choice.finish_reason)
            break

        for call in message.tool_calls:
            tool_calls += 1
            trace.append({"role": "tool_call", "name": call.function.name, "content": call.function.arguments})
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                text = f"Error: tool arguments were not valid JSON ({exc}). Send the call again."
                trace.append({"role": "tool_result", "content": text})
            else:
                if on_tool_call:
                    on_tool_call(call.function.name, arguments)
                result = await session.call_tool(name=call.function.name, arguments=arguments)
                text = "\n".join(getattr(c, "text", "") for c in result.content)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    return report, stop_reason, served_model, tool_calls
