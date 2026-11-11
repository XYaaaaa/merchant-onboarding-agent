"""MCP server: the only door between the agent and the payment sandbox.

Tools:
  get_api_spec         - OpenAPI spec, including the business rules
  list_merchants       - merchant ids and status
  get_merchant_config  - one merchant's sandbox configuration
  api_request          - call the sandbox API as a merchant
  run_pytest           - run a pytest file against the sandbox

Run: python -m onboarding.mcp_server.server   (stdio transport)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.mcpserver import MCPServer

from onboarding.api.config import load_merchants

API_URL = os.environ.get("PAYMENT_API_URL", "http://127.0.0.1:8000").rstrip("/")
PYTEST_TIMEOUT_S = int(os.environ.get("PYTEST_TIMEOUT_S", "120"))
MAX_OUTPUT_CHARS = 12_000

mcp = MCPServer("payment-sandbox")


@mcp.tool()
def get_api_spec() -> str:
    """Return the sandbox payment API's OpenAPI spec as JSON.

    info.description holds the numbered business rules (R1..R10) that the API
    promises to follow. Read this first.
    """
    return httpx.get(f"{API_URL}/openapi.json", timeout=10).text


@mcp.tool()
def list_merchants() -> list[dict]:
    """List all merchants configured on the sandbox, with their status."""
    return [{"id": m.id, "name": m.name, "status": m.status} for m in load_merchants().values()]


@mcp.tool()
def get_merchant_config(merchant_id: str) -> dict:
    """Return one merchant's sandbox configuration: payment methods, currencies,
    amount limit, refund flag, callback URL and (fake, sandbox-only) webhook secret."""
    merchant = load_merchants().get(merchant_id)
    if merchant is None:
        raise ValueError(f"Unknown merchant '{merchant_id}'. Use list_merchants to see valid ids.")
    return merchant.to_dict()


@mcp.tool()
def api_request(
    method: str,
    path: str,
    merchant_id: Optional[str] = None,
    body: Optional[dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Send one HTTP request to the sandbox payment API and return status + body.

    Args:
        method: GET or POST.
        path: Path starting with "/", e.g. "/v1/payments".
        merchant_id: Sent as the X-Merchant-Id header. Omit to test missing auth.
        body: JSON request body for POST.
        idempotency_key: Sent as the Idempotency-Key header.
    """
    method = method.upper()
    if method not in {"GET", "POST"}:
        raise ValueError("method must be GET or POST")
    if not path.startswith("/") or "://" in path:
        raise ValueError('path must start with "/" and stay on the sandbox host')
    headers = {}
    if merchant_id:
        headers["X-Merchant-Id"] = merchant_id
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    response = httpx.request(method, f"{API_URL}{path}", json=body, headers=headers, timeout=10)
    try:
        payload: Any = response.json()
    except json.JSONDecodeError:
        payload = response.text
    return {"status": response.status_code, "body": payload}


@mcp.tool()
def run_pytest(test_code: str) -> dict:
    """Run a pytest file against the sandbox and return the result.

    The code runs in a fresh temp directory. `httpx` and `pytest` are available.
    Read the API base URL from the PAYMENT_API_URL environment variable.
    State persists in the sandbox between runs, so use fresh idempotency keys
    (e.g. uuid4) in every test.

    Args:
        test_code: Full contents of a Python test file.
    """
    with tempfile.TemporaryDirectory(prefix="agent-tests-") as tmp:
        (Path(tmp) / "test_generated.py").write_text(test_code)
        env = {**os.environ, "PAYMENT_API_URL": API_URL}
        env.pop("ANTHROPIC_API_KEY", None)  # generated code never needs it
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "test_generated.py"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=PYTEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return {"exit_code": None, "passed": False, "output": f"Timed out after {PYTEST_TIMEOUT_S}s"}
    output = (proc.stdout + proc.stderr).strip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:2000] + "\n...[middle truncated]...\n" + output[-(MAX_OUTPUT_CHARS - 2000):]
    return {"exit_code": proc.returncode, "passed": proc.returncode == 0, "output": output}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
