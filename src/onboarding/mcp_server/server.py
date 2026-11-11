"""MCP server: the only door between the agent and the payment sandbox.

Tools:
  get_api_spec         - OpenAPI spec, including the business rules
  list_merchants       - merchant ids and status
  get_merchant_config  - one merchant's sandbox configuration
  api_request          - call the sandbox API as a merchant
  run_pytest           - run a pytest file against the sandbox
  submit_finding       - file a confirmed defect with a regression test

Run: python -m onboarding.mcp_server.server   (stdio transport)
"""
from __future__ import annotations

import json
import logging
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
FINDINGS_DIR = Path(os.environ.get("FINDINGS_DIR", "reports/findings"))

mcp = MCPServer("payment-sandbox")

# Keep the terminal readable: one log line per sandbox request is noise.
logging.getLogger("httpx").setLevel(logging.WARNING)


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


def _execute_pytest(test_code: str) -> tuple[int | None, str]:
    """Run test_code in a fresh temp dir. Returns (pytest exit code or None on timeout, output)."""
    with tempfile.TemporaryDirectory(prefix="agent-tests-") as tmp:
        (Path(tmp) / "test_generated.py").write_text(test_code)
        # Generated code never needs model credentials.
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        env["PAYMENT_API_URL"] = API_URL
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "test_generated.py"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=PYTEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return None, f"Timed out after {PYTEST_TIMEOUT_S}s"
    output = (proc.stdout + proc.stderr).strip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:2000] + "\n...[middle truncated]...\n" + output[-(MAX_OUTPUT_CHARS - 2000):]
    return proc.returncode, output


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
    exit_code, output = _execute_pytest(test_code)
    return {"exit_code": exit_code, "passed": exit_code == 0, "output": output}


@mcp.tool()
def submit_finding(rule_id: str, title: str, test_code: str) -> dict:
    """File one confirmed platform defect, with a regression test that proves it.

    The test is the evidence. It must be a self-contained pytest file that PASSES
    on an API that follows the spec and FAILS on this defect, so assert the
    behaviour the spec requires. It will be re-run later against a fresh sandbox,
    so it must create all the data it needs and use fresh idempotency keys.
    Cover one defect per finding, with the smallest test that shows it.

    The test is run now. The finding is rejected if the test does not fail
    with an assertion (it passes, or it cannot be collected).

    Args:
        rule_id: The business rule that is broken, e.g. "R8".
        title: One line saying what is wrong.
        test_code: Full contents of the pytest file.
    """
    exit_code, output = _execute_pytest(test_code)
    if exit_code != 1:  # pytest: 0 = all passed, 1 = tests failed, anything else = could not run
        reason = ("The test passes on the sandbox, so it does not demonstrate a defect."
                  if exit_code == 0 else "The test could not be run cleanly (collection error or timeout).")
        return {"accepted": False, "reason": reason, "output": output}
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    number = len(list(FINDINGS_DIR.glob("finding_*.json"))) + 1
    path = FINDINGS_DIR / f"finding_{number:02d}.json"
    path.write_text(json.dumps({"rule_id": rule_id, "title": title, "test_code": test_code}, indent=2))
    return {"accepted": True, "finding_id": path.stem}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
