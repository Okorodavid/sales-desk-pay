"""Fake PayPal messages, for the demo's "forge a payment" button and the forged-receipt evaluation.

Each kind is something a dishonest buyer (or anyone who knows the webhook address) could really send. None of
them may ever mark an order Paid."""

from __future__ import annotations

import json
import secrets

from .mockpaypal import MockPayPal

KINDS = {
    "unsigned": "A 'payment completed' message with no PayPal signature at all",
    "bad_signature": "A complete-looking message with a made-up signature",
    "wrong_key": "A message signed with the attacker's own key, not PayPal's",
    "tampered_amount": "A real signed message for £1, edited afterwards to say £76",
    "tampered_order": "A real signed message for another order, edited to point at this one",
}


def completed(mock: MockPayPal, pp_order_id: str, code: str, value: str, currency: str) -> dict:
    """A PAYMENT.CAPTURE.COMPLETED event as PayPal would describe the order."""
    return mock.event("PAYMENT.CAPTURE.COMPLETED", {
        "id": "CAP-" + secrets.token_hex(6).upper(), "status": "COMPLETED", "final_capture": True,
        "amount": {"currency_code": currency, "value": value}, "custom_id": code, "invoice_id": code,
        "supplementary_data": {"related_ids": {"order_id": pp_order_id}}}, "Payment completed")


def forged(kind: str, mock: MockPayPal, pp_order_id: str, code: str, value: str, currency: str) -> tuple[dict, bytes]:
    """Headers and body of a forged message of this kind, aimed at the order `pp_order_id`."""
    event = completed(mock, pp_order_id, code, value, currency)
    if kind == "unsigned":
        return {"Content-Type": "application/json"}, json.dumps(event).encode()
    if kind == "bad_signature":
        headers, body = mock.sign(event)
        headers["PAYPAL-TRANSMISSION-SIG"] = "bm90LXJlYWxseS1hLXNpZ25hdHVyZQ=="
        return headers, body
    if kind == "wrong_key":
        return mock.sign(event, key=secrets.token_bytes(32))
    if kind == "tampered_amount":
        cheap = completed(mock, pp_order_id, code, "1.00", currency)
        headers, body = mock.sign(cheap)
        return headers, body.replace(b'"value":"1.00"', f'"value":"{value}"'.encode())
    if kind == "tampered_order":
        other = completed(mock, "SOMEONE-ELSES-ORDER", code, value, currency)
        headers, body = mock.sign(other)
        return headers, body.replace(b"SOMEONE-ELSES-ORDER", pp_order_id.encode())
    raise ValueError(kind)
