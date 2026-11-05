import asyncio

from salesdesk.payments import Alert, match_order, parse_alert

BANKS = ("moniepoint.com", "gtbank.com")


def run(coro):
    return asyncio.run(coro)


def eml(frm="Moniepoint <alerts@moniepoint.com>", auth="mx.google.com; dkim=pass header.i=@moniepoint.com; spf=pass; dmarc=pass header.from=moniepoint.com",
        subject="Credit Alert: NGN38,500.00", body="Amount: NGN 38,500.00\nSender: CHIOMA OKAFOR\nNarration: NF-1 ankara\n"):
    head = f"From: {frm}\nTo: shop@gmail.com\nSubject: {subject}\nMessage-ID: <{abs(hash(body + frm))}@x>\n"
    if auth:
        head += f"Authentication-Results: {auth}\n"
    return (head + "Content-Type: text/plain; charset=utf-8\n\n" + body).encode()


def test_reads_a_signed_credit_alert():
    a = parse_alert(eml(), BANKS)
    assert a and a.amount == 38500 and a.sender == "CHIOMA OKAFOR" and a.narration == "NF-1 ankara" and a.verified


def test_html_alert_with_a_table():
    body = "<table><tr><td>Amount</td><td>: NGN 54,500.00</td></tr><tr><td>Remarks</td><td>: NF-7</td></tr></table>"
    raw = eml(body=body).replace(b"text/plain", b"text/html")
    a = parse_alert(raw, BANKS)
    assert a and a.amount == 54500 and "NF-7" in a.narration


def test_fake_alert_from_another_address_is_not_verified():
    a = parse_alert(eml(frm="Moniepoint Alerts <moniepoint.alerts@gmail.com>",
                        auth="mx.google.com; dkim=pass header.i=@gmail.com"), BANKS)
    assert a and not a.verified and "not on your bank list" in a.why


def test_spoofed_bank_address_without_a_signature_is_not_verified():
    a = parse_alert(eml(auth="mx.google.com; dkim=fail header.i=@moniepoint.com; dmarc=fail header.from=moniepoint.com"), BANKS)
    assert a and not a.verified and "signature" in a.why
    assert not parse_alert(eml(auth=""), BANKS).verified


def test_debit_alerts_and_other_mail_are_ignored():
    assert parse_alert(eml(subject="Debit Alert: NGN5,000.00", body="Amount: NGN 5,000.00 debited"), BANKS) is None
    assert parse_alert(eml(subject="Your statement", body="See attached."), BANKS) is None


def test_matching_prefers_the_order_code_then_the_name():
    orders = [{"id": 1, "total": 38500, "name": "Chioma O."}, {"id": 4, "total": 38500, "name": "Tunde A."}]
    a = Alert("m", 38500, "SOMEONE ELSE", "NF-4", "moniepoint.com", True, "")
    assert match_order(a, orders, "NF") == ([orders[1]], "amount and order code")
    b = Alert("m", 38500, "CHIOMA OKAFOR", "fabric", "moniepoint.com", True, "")
    assert match_order(b, orders, "NF") == ([orders[0]], "amount and name")
    c = Alert("m", 38500, "", "", "moniepoint.com", True, "")
    assert len(match_order(c, orders, "NF")[0]) == 2
    assert match_order(Alert("m", 1, "", "", "", True, ""), orders, "NF")[0] == []


def _awaiting(desk, address="demo-c1", name="Chioma"):
    for t in ("6 yards of the blue ankara please", "GRA", "yes"):
        run(desk.handle("whatsapp", address, t, name))


def test_signed_alert_with_the_code_confirms_by_itself(make_desk):
    d = make_desk()
    _awaiting(d)
    out = d.bank_alert(Alert("m1", 38500, "CHIOMA OKAFOR", "NF-1", "moniepoint.com", True, "signed by moniepoint.com"))
    assert d.store.get_order(1)["stage"] == "paid" and d.store.get_order(1)["paid_by"] == "bank_alert"
    assert "Payment received" in out.replies[0].text and "Bank alert matched" in out.notes[0].text
    assert not d.bank_alert(Alert("m1", 38500, "CHIOMA OKAFOR", "NF-1", "moniepoint.com", True, "")).replies  # duplicate


def test_unsigned_alert_is_left_for_the_owner(make_desk):
    d = make_desk()
    _awaiting(d)
    out = d.bank_alert(Alert("m2", 38500, "CHIOMA OKAFOR", "NF-1", "gmail.com", False, "not on your bank list"))
    assert d.store.get_order(1)["stage"] == "awaiting_payment" and not out.replies
    assert d.store.alerts(("to_check",)) and "Check this alert" in out.notes[0].text
    d.alert_confirm(d.store.alerts(("to_check",))[0]["id"])
    assert d.store.get_order(1)["stage"] == "paid"


def test_auto_confirm_can_be_switched_off(make_desk):
    d = make_desk(auto_confirm=False)
    _awaiting(d)
    d.bank_alert(Alert("m3", 38500, "CHIOMA OKAFOR", "NF-1", "moniepoint.com", True, "signed"))
    assert d.store.get_order(1)["stage"] == "awaiting_payment"


def test_alert_before_the_customer_says_paid_is_matched_when_they_do(make_desk):
    d = make_desk()
    _awaiting(d)
    d.bank_alert(Alert("m4", 38500, "", "", "moniepoint.com", True, "signed"))  # amount only: not sure yet
    assert d.store.get_order(1)["stage"] == "awaiting_payment"
    out = run(d.handle("whatsapp", "demo-c1", "I have sent it"))
    assert d.store.get_order(1)["stage"] == "paid" and "Payment received" in out.replies[0].text


def test_two_orders_with_the_same_amount_are_not_guessed(make_desk):
    d = make_desk()
    _awaiting(d, "demo-a", "Ada")
    _awaiting(d, "demo-b", "Bisi")
    d.bank_alert(Alert("m5", 38500, "", "", "moniepoint.com", True, "signed"))
    assert all(d.store.get_order(i)["stage"] == "awaiting_payment" for i in (1, 2))


def test_money_with_no_order_is_reported(make_desk):
    d = make_desk()
    out = d.bank_alert(Alert("m6", 20000, "EMEKA JOHN", "", "moniepoint.com", True, "signed"))
    assert "no order is waiting" in out.notes[0].text and d.store.alerts(("unmatched",))
