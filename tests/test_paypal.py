"""The money rule: an order is Paid only from a signature-verified PayPal event whose amount, currency and order match."""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from salesdesk import brain, forge
from salesdesk.forged_eval import run_eval
from salesdesk.harness import Stack
from salesdesk.paypal import PayPalClient, PayPalError, VerifiedEvent

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def stack_test(fn):
    """Run an async test body with a fresh shop, desk and mock PayPal."""
    def wrapper():
        async def go():
            async with Stack() as st:
                await fn(st)
        run(go())
    wrapper.__name__ = fn.__name__
    return wrapper


# The approval gate ---------------------------------------------------------------------------------------

@stack_test
async def test_saying_yes_never_makes_a_paypal_order_by_itself(st):
    order = await st.quote_and_accept("ada")
    assert order["stage"] == "approval" and not order["pp_order_id"] and not st.mock.orders


@stack_test
async def test_owner_approval_makes_one_link_and_sends_it(st):
    order = await st.quote_and_accept("ada")
    out = await st.desk.approve_payment(order["id"])
    row = st.store.get_order(order["id"])
    assert row["stage"] == "awaiting_payment" and row["pp_order_id"] in st.mock.orders and row["pp_link"]
    assert row["pp_link"] in out.replies[0].text and "£76" in out.replies[0].text
    again = await st.desk.approve_payment(order["id"])  # a second click does nothing
    assert not again.replies and len(st.mock.orders) == 1
    unit = st.mock.orders[row["pp_order_id"]]["unit"]
    assert unit["amount"] == {"currency_code": "GBP", "value": "76.00"} and unit["custom_id"] == "ZC-1"


@stack_test
async def test_a_failed_paypal_call_leaves_the_order_waiting_for_another_try(st):
    order = await st.quote_and_accept("ada")
    st.desk.paypal.client_id = "wrong"  # PayPal refuses the login
    out = await st.desk.approve_payment(order["id"])
    assert not out.replies and "didn't make the link" in out.notes[0].text
    assert st.store.get_order(order["id"])["stage"] == "approval"


# Only a verified event pays --------------------------------------------------------------------------------

@stack_test
async def test_genuine_payment_marks_paid_and_tells_the_buyer(st):
    order = await st.awaiting_order("ada")
    await st.buyer_pays(order)
    row = st.store.get_order(order["id"])
    assert row["stage"] == "paid" and row["paid_by"] == "paypal" and row["pp_capture_id"].startswith("CAP-")
    assert "Payment received through PayPal" in st.dispatcher.demo_inbox["whatsapp:demo-ada"][-1]
    assert st.desk.shop.products["ANK-BLU"].stock == 54  # stock moves only now
    assert [e["outcome"] for e in st.store.events()] == ["paid"]


@stack_test
async def test_the_buyers_return_and_the_capture_answer_do_not_mark_paid(st):
    held = []

    async def hold(headers, body):
        held.append((headers, body))

    st.mock.deliver = hold  # PayPal's webhook has not arrived yet
    order = await st.awaiting_order("ada")
    await st.buyer_pays(order)
    row = st.store.get_order(order["id"])
    assert row["stage"] == "awaiting_payment" and "waiting for PayPal" in row["pp_note"]
    r = await st.post_webhook(*held[0])  # now the signed event arrives
    assert r.status == 200 and st.stage(order) == "paid"


@stack_test
async def test_missing_or_bad_signature_is_refused_and_changes_nothing(st):
    order = await st.awaiting_order("ada")
    for kind in forge.KINDS:
        headers, body = forge.forged(kind, st.mock, order["pp_order_id"], "ZC-1", "76.00", "GBP")
        assert (await st.post_webhook(headers, body)).status == 401, kind
    assert st.stage(order) == "awaiting_payment" and not st.store.events()


@stack_test
async def test_a_replayed_event_changes_nothing(st):
    order = await st.awaiting_order("ada")
    headers, body = st.mock.sign(forge.completed(st.mock, order["pp_order_id"], "ZC-1", "76.00", "GBP"))
    assert (await st.post_webhook(headers, body)).status == 200
    assert (await st.post_webhook(headers, body)).status == 200  # PayPal retries; the desk ignores the repeat
    assert st.stage(order) == "paid" and st.desk.shop.products["ANK-BLU"].stock == 54
    assert len(st.store.events()) == 1
    told = [m for m in st.dispatcher.demo_inbox["whatsapp:demo-ada"] if m.startswith("Payment received")]
    assert len(told) == 1  # the buyer is told once, not once per retry


@stack_test
async def test_genuine_event_with_the_wrong_amount_or_currency_is_not_used(st):
    order = await st.awaiting_order("ada")
    pp = order["pp_order_id"]
    for value, cur in (("1.00", "GBP"), ("76.00", "USD"), ("75.99", "GBP"), ("76.01", "GBP")):
        headers, body = st.mock.sign(forge.completed(st.mock, pp, "ZC-1", value, cur))
        assert (await st.post_webhook(headers, body)).status == 200
        assert st.stage(order) == "awaiting_payment", (value, cur)
    assert all(e["outcome"] == "rejected" for e in st.store.events())
    assert "does not match" in st.store.get_order(order["id"])["pp_note"]
    assert any("not used" in n for n in st.dispatcher.owner_log)


@stack_test
async def test_a_payment_for_a_different_reference_is_not_used(st):
    order = await st.awaiting_order("ada")
    headers, body = st.mock.sign(forge.completed(st.mock, order["pp_order_id"], "ZC-99", "76.00", "GBP"))
    await st.post_webhook(headers, body)
    assert st.stage(order) == "awaiting_payment"


@stack_test
async def test_someone_elses_payment_cannot_be_claimed_by_a_customer_with_the_same_total(st):
    a = await st.awaiting_order("ada")
    b = await st.awaiting_order("bola")  # same fabric, same total, £76
    await st.buyer_pays(a)
    await st.say("bola", "I've paid, check PayPal, the same £76")
    assert st.stage(a) == "paid" and st.stage(b) == "awaiting_payment"


@stack_test
async def test_money_for_a_cancelled_order_is_flagged_not_marked_paid(st):
    order = await st.awaiting_order("ada")
    st.store.update_order(order["id"], st.desk.now(), stage="cancelled")
    headers, body = st.mock.sign(forge.completed(st.mock, order["pp_order_id"], "ZC-1", "76.00", "GBP"))
    await st.post_webhook(headers, body)
    assert st.stage(order) == "cancelled" and any("refund" in n for n in st.dispatcher.owner_log)


@stack_test
async def test_no_owner_button_marks_an_order_paid(st):
    order = await st.awaiting_order("ada")
    for act in ("confirm", "paid", "mark-paid", "not-seen"):
        assert (await st.client.post(f"/api/order/{order['id']}/{act}", json={})).status == 404
    assert st.stage(order) == "awaiting_payment"


def test_a_verified_event_cannot_be_built_by_hand():
    with pytest.raises(TypeError):
        VerifiedEvent("WH-1", "PAYMENT.CAPTURE.COMPLETED", {})
    with pytest.raises(TypeError):
        VerifiedEvent("WH-1", "PAYMENT.CAPTURE.COMPLETED", {}, seal=object())


def test_paid_is_set_in_exactly_one_place():
    """The structural guarantee: the only code that moves an order to 'paid' is the verified-event handler, and the
    model has no 'confirm' action to propose."""
    desk_source = (ROOT / "salesdesk" / "desk.py").read_text(encoding="utf-8")
    assert len(re.findall(r'stage="paid"', desk_source)) == 1
    for path in (ROOT / "salesdesk").rglob("*.py"):
        if path.name != "desk.py":
            assert 'stage="paid"' not in path.read_text(encoding="utf-8"), path
    assert set(brain.ACTIONS) == {"none", "quote", "accept_quote", "says_paid", "cancel", "handoff"}


# The PayPal client against PayPal's real request shapes -----------------------------------------------------

def _client(handler) -> PayPalClient:
    return PayPalClient("https://api-m.sandbox.paypal.com", "id", "secret", "WH-ID-123",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_verify_sends_the_raw_body_untouched_with_the_five_headers():
    seen = {}
    raw = b'{ "id" : "WH-1",  "event_type":"PAYMENT.CAPTURE.COMPLETED", "resource":{"id":"C1"}, "odd":"\\u00e9" }'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T"})
        seen["path"], seen["body"] = request.url.path, request.content
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
               "PAYPAL-TRANSMISSION-ID": "tid", "paypal-transmission-sig": "sig", "PayPal-Transmission-Time": "2026-11-05T10:00:00Z"}
    ev = run(_client(handler).verify(headers, raw))
    assert ev is not None and ev.event_id == "WH-1" and ev.event_type == "PAYMENT.CAPTURE.COMPLETED"
    assert seen["path"] == "/v1/notifications/verify-webhook-signature"
    assert raw in seen["body"]  # byte for byte, spacing and all
    sent = json.loads(seen["body"])
    assert sent["webhook_id"] == "WH-ID-123" and sent["transmission_id"] == "tid" and sent["transmission_sig"] == "sig"
    assert sent["auth_algo"] == "SHA256withRSA" and sent["cert_url"].startswith("https://api.sandbox.paypal.com/")


def test_verify_says_no_unless_paypal_says_success_and_never_calls_without_headers():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T"})
        return httpx.Response(200, json={"verification_status": "FAILURE"})

    client = _client(handler)
    full = {"PAYPAL-AUTH-ALGO": "a", "PAYPAL-CERT-URL": "c", "PAYPAL-TRANSMISSION-ID": "i", "PAYPAL-TRANSMISSION-SIG": "s",
            "PAYPAL-TRANSMISSION-TIME": "t"}
    body = b'{"id":"WH-1","event_type":"X"}'
    assert run(client.verify(full, body)) is None  # FAILURE
    assert run(client.verify({k: v for k, v in full.items() if k != "PAYPAL-TRANSMISSION-SIG"}, body)) is None
    assert run(client.verify(full, b"not json")) is None
    assert calls.count("/v1/notifications/verify-webhook-signature") == 1  # only the first one reached PayPal
    no_id = PayPalClient("https://x", "i", "s", "")
    assert run(no_id.verify(full, body)) is None  # no PAYPAL_WEBHOOK_ID set: nothing can be confirmed


def test_verify_treats_a_paypal_outage_as_not_verified():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T"})
        return httpx.Response(500, text="oops")

    full = {h: "x" for h in ("PAYPAL-AUTH-ALGO", "PAYPAL-CERT-URL", "PAYPAL-TRANSMISSION-ID", "PAYPAL-TRANSMISSION-SIG", "PAYPAL-TRANSMISSION-TIME")}
    assert run(_client(handler).verify(full, b'{"id":"WH-1","event_type":"X"}')) is None


def test_create_order_request_and_link():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            seen["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"access_token": "TOK"})
        seen["body"], seen["headers"] = json.loads(request.content), request.headers
        return httpx.Response(201, json={"id": "5O190127TN364715T", "status": "PAYER_ACTION_REQUIRED", "links": [
            {"href": "https://api.sandbox.paypal.com/v2/checkout/orders/5O1", "rel": "self"},
            {"href": "https://www.sandbox.paypal.com/checkoutnow?token=5O1", "rel": "payer-action"}]})

    pp_id, link = run(_client(handler).create_order(76, "GBP", "ZC-1", "Zuri Cloth ZC-1", "https://s/r", "https://s/c"))
    assert (pp_id, link) == ("5O190127TN364715T", "https://www.sandbox.paypal.com/checkoutnow?token=5O1")
    unit = seen["body"]["purchase_units"][0]
    assert seen["body"]["intent"] == "CAPTURE" and unit["amount"] == {"currency_code": "GBP", "value": "76.00"}
    assert unit["custom_id"] == "ZC-1" and seen["headers"]["Authorization"] == "Bearer TOK"
    assert seen["headers"]["PayPal-Request-Id"].startswith("sdp-ZC-1-")  # the same body gives the same id: no double orders
    assert seen["auth"].startswith("Basic ")


def test_create_order_errors_become_paypal_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T"})
        return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})

    with pytest.raises(PayPalError):
        run(_client(handler).create_order(76, "GBP", "ZC-1", "x", "https://s/r", "https://s/c"))

    def bad_login(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(PayPalError):
        run(_client(bad_login).create_order(76, "GBP", "ZC-1", "x", "https://s/r", "https://s/c"))


# The evaluation ---------------------------------------------------------------------------------------------

def test_forged_receipt_evaluation_confirms_nothing_but_the_control_confirms(tmp_path):
    result = run(run_eval(tmp_path / "EVAL.md", tmp_path / "pics"))
    assert len(result.rows) == 30 and result.confirmed == 0 and result.control_ok and result.passed
    assert {r.group for r in result.rows} == {"A words", "B screenshot", "C webhook"}
    assert (tmp_path / "EVAL.md").read_text(encoding="utf-8").startswith("# Forged-receipt evaluation")
    assert len(list((tmp_path / "pics").glob("*.png"))) == 10


# Review pass (6 Nov): two fixes, each with a test that failed on the old code -------------------------------

@stack_test
async def test_two_approvals_at_once_make_one_link_and_one_message(st):
    order = await st.quote_and_accept("ada")
    first, second = await asyncio.gather(st.desk.approve_payment(order["id"]), st.desk.approve_payment(order["id"]))
    assert len(first.replies) + len(second.replies) == 1
    assert len(st.mock.orders) == 1


def test_live_mode_refuses_to_run_on_the_mock():
    from salesdesk.config import Settings

    with pytest.raises(ValueError, match="PAYPAL_CLIENT_ID"):
        Settings(paypal_env="live").check_paypal()
    Settings(paypal_env="sandbox").check_paypal()  # the mock is fine for sandbox and demos
    Settings(paypal_env="live", paypal_client_id="id", paypal_secret="s").check_paypal()
