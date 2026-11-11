"""Mock multi-merchant payment API (sandbox).

The business rules in RULES are the contract. They are published in the
OpenAPI description, so anything reading the spec can test against them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import bugs as seeded
from .config import Merchant, load_merchants

RULES = """
Sandbox payment API. Amounts are integers in minor units (cents).
Every error response has the shape `{"error": {"code": "...", "message": "..."}}`.

## Business rules

- **R1 Merchant auth** - every `/v1` request needs an `X-Merchant-Id` header.
  Missing or unknown merchant: `401 unknown_merchant`.
  Merchant whose status is not `active`: `403 merchant_inactive`.
- **R2 Payment methods** - `payment_method` must be in the merchant's configured
  `payment_methods`, else `422 payment_method_not_allowed`.
- **R3 Currencies** - `currency` must be in the merchant's configured
  `currencies`, else `422 currency_not_supported`.
- **R4 Amount limit** - `1 <= amount <= max_amount` (inclusive) for the merchant,
  else `422 amount_exceeds_limit`. Non-positive amounts: `422 invalid_request`.
- **R5 Idempotency** - `POST /v1/payments` requires an `Idempotency-Key` header
  (`400 missing_idempotency_key`). Keys are scoped per merchant. Replaying a key
  with the same body returns the original payment with status `200` and creates
  nothing new. Replaying a key with a different body: `409 idempotency_conflict`.
- **R6 State machine** - `authorized -> captured` (capture),
  `authorized -> voided` (void), `captured -> partially_refunded -> refunded`
  (refunds). Any other transition: `409 invalid_state`.
- **R7 Capture** - optional `amount`, default is the full authorized amount.
  `1 <= amount <= authorized amount`, else `422 capture_exceeds_authorized`.
  A payment can be captured once.
- **R8 Refunds** - only if the merchant has `refund_allowed: true`
  (else `403 refund_not_allowed`), only on `captured` / `partially_refunded`
  payments, and the sum of all refunds must never exceed the captured amount
  (else `422 refund_exceeds_captured`).
- **R9 Isolation** - a merchant can only see and act on its own payments.
  Anything else: `404 payment_not_found`.
- **R10 Webhooks** - `payment.captured`, `payment.voided` and `refund.succeeded`
  each queue exactly one event for the merchant's `callback_url`. The
  `X-Signature` header is the hex HMAC-SHA256 of the raw payload using the
  merchant's `webhook_secret`. `GET /v1/webhook-deliveries` lists the merchant's
  own deliveries (payload + signature).
"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class CreatePayment(BaseModel):
    amount: int = Field(gt=0, description="Minor units, e.g. 1050 = $10.50")
    currency: str = Field(examples=["USD"])
    payment_method: str = Field(examples=["card"])
    payer_ref: str = Field(description="Opaque payer token. Never a card number.")


class CaptureBody(BaseModel):
    amount: Optional[int] = Field(default=None, gt=0)


class RefundBody(BaseModel):
    amount: int = Field(gt=0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """In-memory state. Fine for a sandbox; resets on restart."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.payments: dict[str, dict] = {}
        self.idempotency: dict[tuple, tuple[str, str]] = {}
        self.deliveries: list[dict] = []
        self.sink: dict[str, list[dict]] = {}


def create_app(
    merchants: dict[str, Merchant] | None = None,
    bugs: frozenset[str] | set[str] | None = None,
) -> FastAPI:
    merchants = merchants if merchants is not None else load_merchants()
    active_bugs = frozenset(bugs) if bugs is not None else seeded.from_env()
    store = Store()

    app = FastAPI(title="Sandbox Payment API", version="0.1.0", description=RULES)
    app.state.store = store
    app.state.bugs = active_bugs

    def bug(bug_id: str) -> bool:
        return bug_id in active_bugs

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", []))
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request", "message": f"{where}: {first.get('msg')}"}},
        )

    # R1
    def current_merchant(x_merchant_id: Optional[str] = Header(default=None)) -> Merchant:
        merchant = merchants.get(x_merchant_id or "")
        if merchant is None:
            raise ApiError(401, "unknown_merchant", "Missing or unknown X-Merchant-Id")
        if merchant.status != "active":
            raise ApiError(403, "merchant_inactive", f"Merchant is {merchant.status}")
        return merchant

    # R9
    def own_payment(payment_id: str, merchant: Merchant) -> dict:
        payment = store.payments.get(payment_id)
        if payment is None or (payment["merchant_id"] != merchant.id and not bug("B04")):
            raise ApiError(404, "payment_not_found", "No such payment")
        return payment

    # R10
    def queue_webhook(event_type: str, merchant: Merchant, payment: dict, tasks: BackgroundTasks) -> None:
        event = {
            "id": f"evt_{uuid.uuid4().hex[:16]}",
            "type": event_type,
            "merchant_id": merchant.id,
            "created_at": _now(),
            "data": json.loads(json.dumps(payment)),
        }
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
        key = merchant.id if bug("B06") else merchant.webhook_secret
        delivery = {
            "event_id": event["id"],
            "type": event_type,
            "merchant_id": merchant.id,
            "url": merchant.callback_url,
            "payload": payload,
            "signature": hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest(),
            "status": "queued",
            "attempts": 0,
        }
        store.deliveries.append(delivery)
        if os.environ.get("WEBHOOK_HTTP_DELIVERY") == "1":
            tasks.add_task(_deliver, delivery)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    @app.post("/v1/payments", status_code=201, tags=["payments"], summary="Create (authorize) a payment")
    def create_payment(
        body: CreatePayment,
        merchant: Merchant = Depends(current_merchant),
        idempotency_key: Optional[str] = Header(default=None),
    ):
        if not idempotency_key:
            raise ApiError(400, "missing_idempotency_key", "Idempotency-Key header is required")
        if body.payment_method not in merchant.payment_methods and not bug("B03"):
            raise ApiError(422, "payment_method_not_allowed", f"{body.payment_method} is not enabled for this merchant")
        if body.currency not in merchant.currencies:
            raise ApiError(422, "currency_not_supported", f"{body.currency} is not enabled for this merchant")
        over_limit = body.amount >= merchant.max_amount if bug("B07") else body.amount > merchant.max_amount
        if over_limit:
            raise ApiError(422, "amount_exceeds_limit", f"Limit is {merchant.max_amount}")

        fingerprint = json.dumps(body.model_dump(), sort_keys=True)
        idem_key = (idempotency_key,) if bug("B09") else (merchant.id, idempotency_key)
        with store.lock:
            seen = store.idempotency.get(idem_key)
            if seen and not bug("B01"):
                seen_fingerprint, payment_id = seen
                if seen_fingerprint != fingerprint:
                    raise ApiError(409, "idempotency_conflict", "Key was already used with a different body")
                return JSONResponse(status_code=200, content=store.payments[payment_id])

            payment = {
                "id": f"pay_{uuid.uuid4().hex[:16]}",
                "merchant_id": merchant.id,
                "status": "authorized",
                "amount": body.amount,
                "currency": body.currency,
                "payment_method": body.payment_method,
                "payer_ref": body.payer_ref,
                "captured_amount": 0,
                "refunded_amount": 0,
                "refunds": [],
                "created_at": _now(),
            }
            store.payments[payment["id"]] = payment
            store.idempotency[idem_key] = (fingerprint, payment["id"])
        return payment

    @app.get("/v1/payments/{payment_id}", tags=["payments"], summary="Get a payment")
    def get_payment(payment_id: str, merchant: Merchant = Depends(current_merchant)):
        return own_payment(payment_id, merchant)

    @app.post("/v1/payments/{payment_id}/capture", tags=["payments"], summary="Capture an authorized payment")
    def capture_payment(
        payment_id: str,
        tasks: BackgroundTasks,
        body: Optional[CaptureBody] = None,
        merchant: Merchant = Depends(current_merchant),
    ):
        with store.lock:
            payment = own_payment(payment_id, merchant)
            allowed = {"authorized", "captured"} if bug("B05") else {"authorized"}
            if payment["status"] not in allowed:
                raise ApiError(409, "invalid_state", f"Cannot capture a {payment['status']} payment")
            amount = body.amount if body and body.amount is not None else payment["amount"]
            if amount > payment["amount"] and not bug("B10"):
                raise ApiError(422, "capture_exceeds_authorized", f"Authorized amount is {payment['amount']}")
            payment["status"] = "captured"
            payment["captured_amount"] = amount
            queue_webhook("payment.captured", merchant, payment, tasks)
        return payment

    @app.post("/v1/payments/{payment_id}/void", tags=["payments"], summary="Void an authorized payment")
    def void_payment(payment_id: str, tasks: BackgroundTasks, merchant: Merchant = Depends(current_merchant)):
        with store.lock:
            payment = own_payment(payment_id, merchant)
            if payment["status"] != "authorized":
                raise ApiError(409, "invalid_state", f"Cannot void a {payment['status']} payment")
            payment["status"] = "voided"
            queue_webhook("payment.voided", merchant, payment, tasks)
        return payment

    @app.post("/v1/payments/{payment_id}/refunds", status_code=201, tags=["payments"], summary="Refund a captured payment")
    def refund_payment(
        payment_id: str,
        body: RefundBody,
        tasks: BackgroundTasks,
        merchant: Merchant = Depends(current_merchant),
    ):
        with store.lock:
            payment = own_payment(payment_id, merchant)
            if not merchant.refund_allowed and not bug("B08"):
                raise ApiError(403, "refund_not_allowed", "Refunds are disabled for this merchant")
            if payment["status"] not in {"captured", "partially_refunded"}:
                raise ApiError(409, "invalid_state", f"Cannot refund a {payment['status']} payment")
            already = 0 if bug("B02") else payment["refunded_amount"]
            if already + body.amount > payment["captured_amount"]:
                raise ApiError(422, "refund_exceeds_captured", f"Captured amount is {payment['captured_amount']}")
            refund = {"id": f"ref_{uuid.uuid4().hex[:16]}", "payment_id": payment_id, "amount": body.amount,
                      "status": "succeeded", "created_at": _now()}
            payment["refunds"].append(refund)
            payment["refunded_amount"] += body.amount
            fully = payment["refunded_amount"] >= payment["captured_amount"]
            payment["status"] = "refunded" if fully else "partially_refunded"
            queue_webhook("refund.succeeded", merchant, payment, tasks)
        return refund

    @app.get("/v1/webhook-deliveries", tags=["webhooks"], summary="List this merchant's webhook deliveries")
    def list_deliveries(merchant: Merchant = Depends(current_merchant)):
        return {"deliveries": [d for d in store.deliveries if d["merchant_id"] == merchant.id]}

    # A stand-in for the merchant's own webhook receiver, so the demo is self-contained.
    @app.post("/sink/{merchant_id}", include_in_schema=False)
    async def sink_receive(merchant_id: str, request: Request):
        raw = (await request.body()).decode()
        store.sink.setdefault(merchant_id, []).append(
            {"payload": raw, "signature": request.headers.get("x-signature")}
        )
        return {"received": True}

    @app.get("/sink/{merchant_id}", include_in_schema=False)
    def sink_list(merchant_id: str):
        return {"received": store.sink.get(merchant_id, [])}

    return app


def _deliver(delivery: dict, max_attempts: int = 3) -> None:
    """Best-effort HTTP delivery with retries. Runs after the response is sent."""
    for attempt in range(1, max_attempts + 1):
        delivery["attempts"] = attempt
        try:
            response = httpx.post(
                delivery["url"],
                content=delivery["payload"],
                headers={"Content-Type": "application/json", "X-Signature": delivery["signature"]},
                timeout=3.0,
            )
            if response.status_code < 300:
                delivery["status"] = "delivered"
                return
        except httpx.HTTPError:
            pass
    delivery["status"] = "failed"


def app_factory() -> FastAPI:
    """Entry point for `uvicorn onboarding.api.app:app_factory --factory`."""
    return create_app()
