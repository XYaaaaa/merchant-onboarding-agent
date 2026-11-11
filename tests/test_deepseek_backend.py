"""The DeepSeek backend, driven by a scripted stand-in for DeepSeek's API.

The stand-in plays a short session: look up the merchant, file a finding,
write the report. It also enforces DeepSeek's rule that reasoning_content
must come back unchanged on every later request.
"""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import oracle  # noqa: E402
import run_eval  # noqa: E402
from onboarding.agent import deepseek_backend, run as agent_run  # noqa: E402


def scripted_deepseek(seen: list):
    app = FastAPI()

    def reply(content=None, tool=None, reasoning=None, finish="stop"):
        message = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool:
            name, args = tool
            message["tool_calls"] = [{"id": f"call_{len(seen)}", "type": "function",
                                      "function": {"name": name, "arguments": json.dumps(args)}}]
            finish = "tool_calls"
        return {"id": "x", "object": "chat.completion", "created": 0, "model": "deepseek-v4-pro",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                          "prompt_cache_hit_tokens": 60}}

    @app.post("/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        seen.append(body)
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        if any("reasoning_content" not in m for m in assistants):
            return JSONResponse(status_code=400, content={"error": {"message": "reasoning_content missing"}})
        if body.get("tool_choice") == "none":
            return reply(content="Verdict: NOT READY\n\nRan out of turns; R2-R10 not tested.", reasoning="Wrap up.")
        step = len(assistants)
        if step == 0:
            return reply(tool=("get_merchant_config", {"merchant_id": "nova_telecom"}), reasoning="Check config.")
        if step == 1:
            finding = oracle.finding("B08")
            return reply(tool=("submit_finding", finding), reasoning="Refund went through; file it.")
        return reply(content="Verdict: NOT READY\n\nRefunds are not blocked (R8).", reasoning="Done.")

    return app


@pytest.fixture
def fake_deepseek(monkeypatch):
    seen: list = []
    server = uvicorn.Server(uvicorn.Config(scripted_deepseek(seen), host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    monkeypatch.setattr(deepseek_backend, "BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    yield seen
    server.should_exit = True
    thread.join(timeout=5)


def test_deepseek_key_selects_deepseek(fake_deepseek, monkeypatch):
    assert agent_run.default_provider() == "deepseek"
    assert agent_run.default_model("deepseek") == "deepseek-v4-pro"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert agent_run.default_provider() == "anthropic"


def test_full_case_through_deepseek_backend(fake_deepseek, tmp_path):
    case = {"id": "single-B08", "merchant": "nova_telecom", "bugs": ["B08"]}

    async def go():
        result, findings = await run_eval.run_case(case, "claude", None, 120, 10, None)
        return result, findings, await run_eval.grade_case(case, result.report, findings)

    result, findings, (grade, _, meta) = asyncio.run(go())

    # The agent loop: 3 model turns, 2 tool calls, real MCP tools, real sandbox.
    assert len(fake_deepseek) == 3 and result.tool_calls == 2
    assert result.model == "deepseek-v4-pro" and result.stop_reason == "end_turn"
    assert result.usage["input_tokens"] == 300 and result.usage["cache_read_input_tokens"] == 180
    assert [t["role"] for t in result.trace].count("tool_result") == 2

    # Tools were offered in OpenAI function format, and tool results went back.
    names = {t["function"]["name"] for t in fake_deepseek[0]["tools"]}
    assert "submit_finding" in names and "api_request" in names
    assert [m["role"] for m in fake_deepseek[2]["messages"]].count("tool") == 2

    # The finding was accepted by the MCP server and holds up under replay.
    assert len(findings) == 1 and meta["found"] == ["B08"]
    assert grade == {"case_pass": 1.0, "recall": 1.0, "false_pos": 0.0, "verdict_ok": 1.0}


def test_out_of_turns_still_produces_a_report(fake_deepseek, tmp_path):
    result = asyncio.run(_one_turn_short(tmp_path))
    assert result.stop_reason == "max_iterations"
    assert run_eval.read_verdict(result.report) == "NOT READY"
    final_request = fake_deepseek[-1]
    assert final_request["tool_choice"] == "none"
    assert "out of turns" in final_request["messages"][-1]["content"]


async def _one_turn_short(tmp_path):
    async with run_eval.Sandbox(["B08"]) as sandbox:
        return await agent_run.run_agent("nova_telecom", sandbox.url, tmp_path, max_iterations=2)
