"""Seeded bugs for the eval.

The API is correct by default. Setting SEEDED_BUGS=B01,B04 (or passing
`bugs=` to create_app) switches on deliberate defects, so we can measure
how many of them the test agent finds. The agent never sees this file:
it only reaches the API through the MCP server.
"""
from __future__ import annotations

import os

CATALOG: dict[str, str] = {
    "B01": "R5  Replaying an Idempotency-Key creates a second payment",
    "B02": "R8  Several partial refunds can add up to more than the captured amount",
    "B03": "R2  Payment method allowlist is not enforced",
    "B04": "R9  A merchant can read another merchant's payment",
    "B05": "R6  A captured payment can be captured again",
    "B06": "R10 Webhook signature is computed with the wrong key",
    "B07": "R4  amount == max_amount is rejected (off by one)",
    "B08": "R8  refund_allowed=false is ignored",
    "B09": "R5  Idempotency keys are shared across merchants",
    "B10": "R7  Capture amount may exceed the authorized amount",
}


def parse(value: str | None) -> frozenset[str]:
    ids = {b.strip().upper() for b in (value or "").split(",") if b.strip()}
    unknown = ids - CATALOG.keys()
    if unknown:
        raise ValueError(f"Unknown seeded bug ids: {sorted(unknown)}")
    return frozenset(ids)


def from_env() -> frozenset[str]:
    return parse(os.environ.get("SEEDED_BUGS"))
