"""Reference regression test for each seeded bug.

Used to check the eval harness itself: an "agent" that files exactly these
must score 100%, and they double as proof that every bug can be caught
through the public API alone.
"""

PREAMBLE = '''
import hashlib, hmac, os, uuid
import httpx

BASE = os.environ["PAYMENT_API_URL"]
ACME, NOVA = "acme_utilities", "nova_telecom"

def h(merchant, key=None):
    headers = {"X-Merchant-Id": merchant}
    if key:
        headers["Idempotency-Key"] = key
    return headers

def create(merchant, amount=1000, method="card", currency="USD", key=None):
    body = {"amount": amount, "currency": currency, "payment_method": method, "payer_ref": "tok_test"}
    return httpx.post(f"{BASE}/v1/payments", json=body, headers=h(merchant, key or uuid.uuid4().hex))

def captured(merchant, amount=1000):
    pid = create(merchant, amount=amount).json()["id"]
    assert httpx.post(f"{BASE}/v1/payments/{pid}/capture", headers=h(merchant)).status_code == 200
    return pid
'''

TESTS = {
    "B01": ("R5", "Replaying an Idempotency-Key creates a second payment", '''
def test_replay_returns_original_payment():
    key = uuid.uuid4().hex
    first = create(ACME, key=key)
    again = create(ACME, key=key)
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
'''),
    "B02": ("R8", "Partial refunds can exceed the captured amount", '''
def test_refunds_cannot_exceed_captured_total():
    pid = captured(ACME, 1000)
    url = f"{BASE}/v1/payments/{pid}/refunds"
    assert httpx.post(url, json={"amount": 600}, headers=h(ACME)).status_code == 201
    assert httpx.post(url, json={"amount": 600}, headers=h(ACME)).status_code == 422
'''),
    "B03": ("R2", "Payment method allowlist is not enforced", '''
def test_method_not_enabled_is_rejected():
    assert create(NOVA, method="bank_transfer").status_code == 422
'''),
    "B04": ("R9", "A merchant can read another merchant's payment", '''
def test_other_merchant_cannot_read_payment():
    pid = create(ACME).json()["id"]
    assert httpx.get(f"{BASE}/v1/payments/{pid}", headers=h(NOVA)).status_code == 404
'''),
    "B05": ("R6", "A captured payment can be captured again", '''
def test_second_capture_is_rejected():
    pid = captured(ACME)
    assert httpx.post(f"{BASE}/v1/payments/{pid}/capture", headers=h(ACME)).status_code == 409
'''),
    "B06": ("R10", "Webhook signature does not verify with the merchant's secret", '''
def test_webhook_signature_verifies():
    captured(ACME)
    deliveries = httpx.get(f"{BASE}/v1/webhook-deliveries", headers=h(ACME)).json()["deliveries"]
    assert deliveries
    last = deliveries[-1]
    expected = hmac.new(b"whsec_sandbox_acme_123", last["payload"].encode(), hashlib.sha256).hexdigest()
    assert last["signature"] == expected
'''),
    "B07": ("R4", "amount equal to max_amount is rejected", '''
def test_amount_at_limit_is_accepted():
    assert create(NOVA, amount=100000).status_code == 201
'''),
    "B08": ("R8", "Refunds go through for a merchant with refunds disabled", '''
def test_refund_disabled_merchant_cannot_refund():
    pid = captured(NOVA)
    r = httpx.post(f"{BASE}/v1/payments/{pid}/refunds", json={"amount": 100}, headers=h(NOVA))
    assert r.status_code == 403
'''),
    "B09": ("R5", "Idempotency keys are shared across merchants", '''
def test_idempotency_keys_are_per_merchant():
    key = uuid.uuid4().hex
    assert create(ACME, key=key).status_code == 201
    other = create(NOVA, key=key)
    assert other.status_code == 201
    assert other.json()["merchant_id"] == NOVA
'''),
    "B10": ("R7", "Capture amount may exceed the authorized amount", '''
def test_capture_above_authorized_is_rejected():
    pid = create(ACME, amount=1000).json()["id"]
    r = httpx.post(f"{BASE}/v1/payments/{pid}/capture", json={"amount": 1001}, headers=h(ACME))
    assert r.status_code == 422
'''),
}


def finding(bug_id: str) -> dict:
    rule_id, title, body = TESTS[bug_id]
    return {"rule_id": rule_id, "title": title, "test_code": PREAMBLE + body}


# A finding that is wrong: the API is right, the test is not.
BOGUS = {"rule_id": "R5", "title": "Creating a payment returns the wrong status",
         "test_code": PREAMBLE + '''
def test_create_returns_200():
    assert create(ACME).status_code == 200
'''}
