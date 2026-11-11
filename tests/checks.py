"""One check per business rule. Each raises AssertionError if the API breaks the rule.

Used twice: every check must pass on the clean API, and each seeded bug
must make exactly its own check fail (see test_seeded_bugs.py).
"""
from __future__ import annotations

import hashlib
import hmac
import uuid

ACME, NOVA, GYM = "acme_utilities", "nova_telecom", "old_gym"
ACME_SECRET = "whsec_sandbox_acme_123"


def h(merchant: str, key: str | None = None) -> dict:
    headers = {"X-Merchant-Id": merchant}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def body(amount=1000, method="card", currency="USD") -> dict:
    return {"amount": amount, "currency": currency, "payment_method": method, "payer_ref": "tok_test_payer"}


def create(client, merchant=ACME, key=None, **kw):
    return client.post("/v1/payments", json=body(**kw), headers=h(merchant, key or uuid.uuid4().hex))


def captured(client, merchant=ACME, amount=1000) -> str:
    pid = create(client, merchant, amount=amount).json()["id"]
    assert client.post(f"/v1/payments/{pid}/capture", headers=h(merchant)).status_code == 200
    return pid


def expect(response, status: int, code: str | None = None):
    assert response.status_code == status, f"expected {status}, got {response.status_code}: {response.text}"
    if code:
        assert response.json()["error"]["code"] == code, response.text
    return response


def r1_merchant_auth(c):
    expect(c.post("/v1/payments", json=body(), headers={"Idempotency-Key": "k"}), 401, "unknown_merchant")
    expect(create(c, "nobody"), 401, "unknown_merchant")
    expect(create(c, GYM), 403, "merchant_inactive")


def r2_payment_methods(c):
    expect(create(c, ACME, method="bank_transfer"), 201)
    expect(create(c, NOVA, method="bank_transfer"), 422, "payment_method_not_allowed")


def r3_currencies(c):
    expect(create(c, NOVA, currency="EUR"), 201)
    expect(create(c, ACME, currency="EUR"), 422, "currency_not_supported")


def r4_amount_limit(c):
    expect(create(c, NOVA, amount=100000), 201)
    expect(create(c, NOVA, amount=100001), 422, "amount_exceeds_limit")
    expect(create(c, NOVA, amount=0), 422, "invalid_request")


def r5_idempotent_replay(c):
    first = expect(create(c, ACME, key="order-1"), 201).json()
    again = expect(create(c, ACME, key="order-1"), 200).json()
    assert again["id"] == first["id"]
    expect(create(c, ACME, key="order-1", amount=2000), 409, "idempotency_conflict")
    expect(c.post("/v1/payments", json=body(), headers=h(ACME)), 400, "missing_idempotency_key")


def r5_idempotency_per_merchant(c):
    a = expect(create(c, ACME, key="shared-key"), 201).json()
    n = expect(create(c, NOVA, key="shared-key"), 201).json()
    assert a["id"] != n["id"] and n["merchant_id"] == NOVA


def r6_state_machine(c):
    pid = captured(c)
    expect(c.post(f"/v1/payments/{pid}/capture", headers=h(ACME)), 409, "invalid_state")
    expect(c.post(f"/v1/payments/{pid}/void", headers=h(ACME)), 409, "invalid_state")

    voided = create(c).json()["id"]
    expect(c.post(f"/v1/payments/{voided}/void", headers=h(ACME)), 200)
    expect(c.post(f"/v1/payments/{voided}/capture", headers=h(ACME)), 409, "invalid_state")

    fresh = create(c).json()["id"]
    expect(c.post(f"/v1/payments/{fresh}/refunds", json={"amount": 1}, headers=h(ACME)), 409, "invalid_state")


def r7_capture_amount(c):
    pid = create(c, amount=1000).json()["id"]
    expect(c.post(f"/v1/payments/{pid}/capture", json={"amount": 1001}, headers=h(ACME)), 422, "capture_exceeds_authorized")
    partial = expect(c.post(f"/v1/payments/{pid}/capture", json={"amount": 400}, headers=h(ACME)), 200).json()
    assert partial["status"] == "captured" and partial["captured_amount"] == 400


def r8_refund_total(c):
    pid = captured(c, amount=1000)
    url = f"/v1/payments/{pid}/refunds"
    expect(c.post(url, json={"amount": 600}, headers=h(ACME)), 201)
    assert c.get(f"/v1/payments/{pid}", headers=h(ACME)).json()["status"] == "partially_refunded"
    expect(c.post(url, json={"amount": 600}, headers=h(ACME)), 422, "refund_exceeds_captured")
    expect(c.post(url, json={"amount": 400}, headers=h(ACME)), 201)
    assert c.get(f"/v1/payments/{pid}", headers=h(ACME)).json()["status"] == "refunded"
    expect(c.post(url, json={"amount": 1}, headers=h(ACME)), 409, "invalid_state")


def r8_refund_flag(c):
    pid = captured(c, NOVA)
    expect(c.post(f"/v1/payments/{pid}/refunds", json={"amount": 100}, headers=h(NOVA)), 403, "refund_not_allowed")


def r9_isolation(c):
    pid = create(c, ACME).json()["id"]
    expect(c.get(f"/v1/payments/{pid}", headers=h(NOVA)), 404, "payment_not_found")
    expect(c.get(f"/v1/payments/{pid}", headers=h(ACME)), 200)


def r10_webhooks(c):
    pid = captured(c, ACME)
    expect(c.post(f"/v1/payments/{pid}/refunds", json={"amount": 1000}, headers=h(ACME)), 201)
    deliveries = c.get("/v1/webhook-deliveries", headers=h(ACME)).json()["deliveries"]
    assert [d["type"] for d in deliveries] == ["payment.captured", "refund.succeeded"]
    for d in deliveries:
        expected = hmac.new(ACME_SECRET.encode(), d["payload"].encode(), hashlib.sha256).hexdigest()
        assert d["signature"] == expected, "signature does not verify with the merchant's webhook_secret"
    assert c.get("/v1/webhook-deliveries", headers=h(NOVA)).json()["deliveries"] == []


ALL = [
    r1_merchant_auth, r2_payment_methods, r3_currencies, r4_amount_limit,
    r5_idempotent_replay, r5_idempotency_per_merchant, r6_state_machine,
    r7_capture_amount, r8_refund_total, r8_refund_flag, r9_isolation, r10_webhooks,
]

# Which check is expected to catch each seeded bug.
DETECTED_BY = {
    "B01": r5_idempotent_replay,
    "B02": r8_refund_total,
    "B03": r2_payment_methods,
    "B04": r9_isolation,
    "B05": r6_state_machine,
    "B06": r10_webhooks,
    "B07": r4_amount_limit,
    "B08": r8_refund_flag,
    "B09": r5_idempotency_per_merchant,
    "B10": r7_capture_amount,
}
