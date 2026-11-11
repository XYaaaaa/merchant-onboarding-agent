"""End to end: real API over HTTP, real MCP server over stdio, real MCP client."""
import asyncio
import json
import os
import socket
import sys
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from onboarding.api.app import create_app


@pytest.fixture(scope="module")
def api_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # B08 is on so the generated test below has a real defect to catch.
    server = uvicorn.Server(uvicorn.Config(create_app(bugs={"B08"}), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


def call_tools(api_url, calls, findings_dir):
    """Open an MCP session and run [(tool, args), ...]; returns parsed results."""
    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "onboarding.mcp_server.server"],
            env={**os.environ, "PAYMENT_API_URL": api_url, "FINDINGS_DIR": str(findings_dir)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                results = []
                for tool, args in calls:
                    result = await session.call_tool(tool, args)
                    results.append((result.is_error, result.content[0].text))
                return names, results
    return asyncio.run(_run())


GENERATED_TEST = '''
import os, uuid, httpx
BASE = os.environ["PAYMENT_API_URL"]
H = {"X-Merchant-Id": "nova_telecom"}

def test_refunds_disabled_for_nova():
    body = {"amount": 500, "currency": "USD", "payment_method": "card", "payer_ref": "tok_x"}
    pay = httpx.post(f"{BASE}/v1/payments", json=body, headers={**H, "Idempotency-Key": uuid.uuid4().hex}).json()
    httpx.post(f"{BASE}/v1/payments/{pay['id']}/capture", headers=H)
    r = httpx.post(f"{BASE}/v1/payments/{pay['id']}/refunds", json={"amount": 100}, headers=H)
    assert r.status_code == 403
'''


def test_mcp_tools_end_to_end(api_url, tmp_path):
    names, results = call_tools(api_url, [
        ("get_api_spec", {}),
        ("get_merchant_config", {"merchant_id": "nova_telecom"}),
        ("api_request", {"method": "POST", "path": "/v1/payments", "merchant_id": "acme_utilities",
                         "idempotency_key": "mcp-test-1",
                         "body": {"amount": 1200, "currency": "USD", "payment_method": "card", "payer_ref": "tok_x"}}),
        ("api_request", {"method": "GET", "path": "http://evil.example/x"}),
        ("run_pytest", {"test_code": GENERATED_TEST}),
        ("submit_finding", {"rule_id": "R8", "title": "Refund allowed for nova", "test_code": GENERATED_TEST}),
        ("submit_finding", {"rule_id": "R1", "title": "Not a defect", "test_code": "def test_ok():\n    assert True\n"}),
    ], findings_dir=tmp_path)
    assert names == {"get_api_spec", "list_merchants", "get_merchant_config", "api_request", "run_pytest",
                     "submit_finding"}

    spec, config, created, bad_path, pytest_run, filed, rejected = results
    assert "R8 Refunds" in json.loads(spec[1])["info"]["description"]
    assert json.loads(config[1])["refund_allowed"] is False
    assert json.loads(created[1])["status"] == 201
    assert bad_path[0] is True  # refused to leave the sandbox host

    run = json.loads(pytest_run[1])
    assert run["passed"] is False and "assert 201 == 403" in run["output"]  # seeded bug B08 caught

    # A finding is filed only when its test really fails on the sandbox.
    assert json.loads(filed[1])["accepted"] is True
    assert json.loads(rejected[1])["accepted"] is False
    stored = [json.loads(p.read_text()) for p in tmp_path.glob("finding_*.json")]
    assert [f["rule_id"] for f in stored] == ["R8"]
