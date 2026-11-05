"""PayPal: make a payment link for an approved order, and check that a message really came from PayPal.

The one rule of this product lives here: **an order becomes Paid only from a `VerifiedEvent`**, and the only
code that can build a `VerifiedEvent` is `PayPalClient.verify`, which asks PayPal's own
verify-webhook-signature API whether the message is genuine. A screenshot, a customer's word, an
unsigned or tampered request cannot produce one.

Money flow (Orders v2, intent CAPTURE):
1. The owner approves the order -> `create_order` -> PayPal returns a buyer link -> the robot sends it.
2. The buyer pays on PayPal's page and is sent back to /paypal/return -> `capture`.
3. PayPal posts PAYMENT.CAPTURE.COMPLETED to /paypal/webhook -> `verify` -> the desk checks the amount,
   the currency and the order, and only then marks it Paid.
The capture call's own answer is never used to mark an order Paid."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx

log = logging.getLogger("salesdesk.paypal")

MOCK_WEBHOOK_ID = "MOCK-WEBHOOK-ID"
HEADERS = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig",
           "paypal-transmission-time")
_SEAL = object()


class PayPalError(Exception):
    """PayPal refused or could not be reached. The order stays where it was and the owner is told."""


@dataclass(frozen=True)
class VerifiedEvent:
    """A webhook message that PayPal itself confirmed is genuine. Cannot be built anywhere else."""

    event_id: str
    event_type: str
    resource: dict
    seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.seal is not _SEAL:
            raise TypeError("a VerifiedEvent can only come from PayPalClient.verify")

    @property
    def pp_order_id(self) -> str:
        related = ((self.resource.get("supplementary_data") or {}).get("related_ids") or {})
        return str(related.get("order_id") or "")

    @property
    def status(self) -> str:
        return str(self.resource.get("status") or "").upper()

    @property
    def currency(self) -> str:
        return str((self.resource.get("amount") or {}).get("currency_code") or "").upper()

    @property
    def value(self) -> Decimal | None:
        try:
            return Decimal(str((self.resource.get("amount") or {}).get("value")))
        except (InvalidOperation, TypeError):
            return None

    @property
    def capture_id(self) -> str:
        return str(self.resource.get("id") or "")

    @property
    def custom_id(self) -> str:
        return str(self.resource.get("custom_id") or "")


def decimal_text(amount: int) -> str:
    return f"{Decimal(int(amount)):.2f}"


class PayPalClient:
    def __init__(self, base_url: str, client_id: str, secret: str, webhook_id: str,
                 http: httpx.AsyncClient | None = None, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.client_id, self.secret, self.webhook_id = client_id, secret, webhook_id
        self.http = http or httpx.AsyncClient(timeout=timeout)
        self._token = ""

    @classmethod
    def from_settings(cls, settings, http: httpx.AsyncClient | None = None) -> "PayPalClient":
        if settings.paypal_mock:
            return cls(settings.paypal_base, "mock-client", "mock-secret", MOCK_WEBHOOK_ID, http)
        return cls(settings.paypal_base, settings.paypal_client_id, settings.paypal_secret, settings.paypal_webhook_id, http)

    @property
    def is_mock(self) -> bool:
        return "/mock-paypal" in self.base

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _auth(self, force: bool = False) -> dict:
        if force or not self._token:
            try:
                r = await self.http.post(f"{self.base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                                         auth=(self.client_id, self.secret))
            except httpx.HTTPError as exc:
                raise PayPalError(f"couldn't reach PayPal: {exc}") from exc
            if r.status_code != 200:
                raise PayPalError(f"PayPal login failed ({r.status_code}). Check PAYPAL_CLIENT_ID and PAYPAL_SECRET.")
            self._token = r.json().get("access_token", "")
        return {"Authorization": f"Bearer {self._token}"}

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        extra = kwargs.pop("headers", {})
        for attempt in (0, 1):
            headers = {**(await self._auth(force=attempt == 1)), **extra}
            try:
                r = await self.http.request(method, f"{self.base}{path}", headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise PayPalError(f"couldn't reach PayPal: {exc}") from exc
            if r.status_code != 401 or attempt:
                return r
        return r  # pragma: no cover

    # The owner-approved payment link ------------------------------------------------------------------
    async def create_order(self, amount: int, currency: str, reference: str, description: str,
                           return_url: str, cancel_url: str) -> tuple[str, str]:
        """A PayPal order for one chat order. Returns (PayPal order id, the link the buyer opens).

        `reference` (the shop's order code, like ZC-12) is sent as custom_id and as the idempotency key, so
        pressing Approve twice gives the same PayPal order, never two."""
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference, "custom_id": reference, "invoice_id": reference,
                "description": description[:127],
                "amount": {"currency_code": currency, "value": decimal_text(amount)},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url, "cancel_url": cancel_url,
                "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING"}}},
        }
        r = await self._call("POST", "/v2/checkout/orders", json=body,
                             headers={"PayPal-Request-Id": f"sdp-{reference}", "Prefer": "return=representation"})
        if r.status_code not in (200, 201):
            raise PayPalError(f"PayPal wouldn't create the order ({r.status_code}): {r.text[:200]}")
        data = r.json()
        link = next((link["href"] for rel in ("payer-action", "approve") for link in data.get("links", [])
                     if link.get("rel") == rel), "")
        if not data.get("id") or not link:
            raise PayPalError("PayPal's answer had no order id or buyer link")
        return str(data["id"]), link

    async def capture(self, pp_order_id: str) -> str:
        """Collect the money after the buyer approved. Returns PayPal's status text, for the log only: this
        answer never marks anything Paid. The signed webhook does."""
        r = await self._call("POST", f"/v2/checkout/orders/{pp_order_id}/capture", content=b"{}",
                             headers={"Content-Type": "application/json", "PayPal-Request-Id": f"sdp-cap-{pp_order_id}"})
        if r.status_code not in (200, 201):
            raise PayPalError(f"PayPal wouldn't capture ({r.status_code}): {r.text[:200]}")
        return str(r.json().get("status") or "")

    # Is this message really from PayPal? ----------------------------------------------------------------
    async def verify(self, headers, raw: bytes) -> VerifiedEvent | None:
        """Ask PayPal's verify-webhook-signature API. None for anything that isn't a genuine event.

        The raw body goes to PayPal untouched: parsing it and writing it back out can break the check."""
        low = {k.lower(): v for k, v in headers.items()}
        if not all(low.get(h) for h in HEADERS) or not self.webhook_id:
            return None
        try:
            text = raw.decode("utf-8")
            event = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(event, dict) or not event.get("id") or not event.get("event_type"):
            return None
        j = json.dumps
        body = ('{"auth_algo":%s,"cert_url":%s,"transmission_id":%s,"transmission_sig":%s,"transmission_time":%s,'
                '"webhook_id":%s,"webhook_event":%s}') % (
            j(low["paypal-auth-algo"]), j(low["paypal-cert-url"]), j(low["paypal-transmission-id"]),
            j(low["paypal-transmission-sig"]), j(low["paypal-transmission-time"]), j(self.webhook_id), text)
        try:
            r = await self._call("POST", "/v1/notifications/verify-webhook-signature", content=body.encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
        except PayPalError as exc:
            log.warning("verify-webhook-signature: %s", exc)
            return None
        if r.status_code != 200 or r.json().get("verification_status") != "SUCCESS":
            return None
        resource = event.get("resource")
        return VerifiedEvent(str(event["id"]), str(event["event_type"]), resource if isinstance(resource, dict) else {}, _SEAL)


def basic_auth_ok(header: str, client_id: str, secret: str) -> bool:
    """For the mock PayPal: did the caller log in with the expected client id and secret?"""
    if not header.lower().startswith("basic "):
        return False
    try:
        got = base64.b64decode(header[6:]).decode()
    except Exception:
        return False
    return got == f"{client_id}:{secret}"
