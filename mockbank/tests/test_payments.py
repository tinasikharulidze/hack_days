"""Step 5 tests: starting payments, the customer's approval, money moving, faults, duplicates."""

import uuid
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from app.seed import make_iban
from tests.helpers import advance, connect, customer

OUTSIDE_IBAN = make_iban("0000111122223333")      # a valid IBAN that is not one of our accounts
REDIRECT = "https://app.example/pay-done"


def err(response):
    return response.status_code, response.json()["tppMessages"][0]["code"]


def pay_headers(h, request_id=None):
    """The headers a payment start needs, on top of the token and consent id."""
    return {**h, "X-Request-ID": request_id or str(uuid.uuid4()), "PSU-IP-Address": "192.0.2.1",
            "TPP-Redirect-URI": REDIRECT}


def read_headers(h):
    """Headers for the other payment calls (no redirect address needed)."""
    return {**h, "X-Request-ID": str(uuid.uuid4())}


def accounts_of(client, customer_id="c1"):
    return customer(client, customer_id)["accounts"]


def available(client, iban):
    data = client.get("/admin/data").json()["accounts"]
    return Decimal(next(a for a in data if a["iban"] == iban)["balances"]["interimAvailable"])


def booked(client, iban):
    data = client.get("/admin/data").json()["accounts"]
    return Decimal(next(a for a in data if a["iban"] == iban)["balances"]["interimBooked"])


def payment_body(client, amount="25.50", **changes):
    body = {
        "debtorAccount": {"iban": accounts_of(client)[0]["iban"]},
        "instructedAmount": {"currency": "GEL", "amount": amount},
        "creditorAccount": {"iban": OUTSIDE_IBAN},
        "creditorName": "Example Shop",
        "endToEndIdentification": "ORDER-1",
        "remittanceInformationUnstructured": "Order 1",
    }
    body.update(changes)
    return body


def start(client, h, product="domestic", request_id=None, amount="25.50", **changes):
    return client.post(f"/v1/payments/{product}", json=payment_body(client, amount, **changes),
                       headers=pay_headers(h, request_id))


def link_path(created):
    return urlparse(created.json()["_links"]["scaRedirect"]["href"]).path


def decide(client, created, decision="approve", customer_id="c1"):
    """Play the customer: sign in on the payment page and press Approve or Reject."""
    person = customer(client, customer_id)
    path = link_path(created)
    client.post(path + "/login", data={"username": person["username"], "password": person["password"]})
    return client.post(path + "/decide", data={"decision": decision})


def status_of(client, h, payment_id):
    r = client.get(f"/v1/payments/domestic/{payment_id}/status", headers=read_headers(h))
    return r.json()["transactionStatus"]


def paid(client, h, **changes):
    """Start a payment and approve it. Returns the payment id."""
    created = start(client, h, **changes)
    assert created.status_code == 201, created.text
    decide(client, created)
    return created.json()["paymentId"]


# ----- Starting a payment -----

def test_start_gives_received_and_a_link_and_moves_no_money(client):
    h, _ = connect(client)
    iban = accounts_of(client)[0]["iban"]
    before = available(client, iban)
    r = start(client, h)
    assert r.status_code == 201
    body = r.json()
    assert body["transactionStatus"] == "RCVD" and body["paymentId"]
    assert set(body["_links"]) == {"scaRedirect", "self", "status"}
    assert body["_links"]["scaRedirect"]["href"].startswith("http://testserver/bank/pay/")
    assert r.headers["ASPSP-SCA-Approach"] == "REDIRECT" and r.headers["Location"].endswith(body["paymentId"])
    assert available(client, iban) == before


def test_read_payment_and_status(client):
    h, _ = connect(client)
    created = start(client, h).json()
    pid = created["paymentId"]
    body = client.get(f"/v1/payments/domestic/{pid}", headers=read_headers(h)).json()
    assert body["transactionStatus"] == "RCVD" and body["creditorName"] == "Example Shop"
    assert body["instructedAmount"] == {"currency": "GEL", "amount": "25.50"}
    assert body["endToEndIdentification"] == "ORDER-1"
    assert status_of(client, h, pid) == "RCVD"
    assert err(client.get("/v1/payments/domestic/nope", headers=read_headers(h))) == (404, "RESOURCE_UNKNOWN")


def test_products(client):
    h, _ = connect(client)
    assert err(start(client, h, product="foreign")) == (403, "PRODUCT_INVALID")
    assert err(start(client, h, product="sepa")) == (404, "PRODUCT_UNKNOWN")


def test_a_bad_payment_request_is_refused(client):
    h, _ = connect(client)
    mine = accounts_of(client)
    usd = next(a for a in mine if a["currency"] == "USD")
    own_gel = mine[1]["iban"]       # the savings account, also lari
    other_usd = next(a for a in accounts_of(client, "c2") if a["currency"] == "EUR")
    other_gel = next(a for a in accounts_of(client, "c2") if a["currency"] == "GEL")
    huge = str(available(client, mine[0]["iban"]) + 1)
    cases = [
        (dict(instructedAmount={"currency": "USD", "amount": "5.00"}), (400, "PARAMETER_NOT_CONSISTENT")),
        (dict(debtorAccount={"iban": usd["iban"]}), (400, "PARAMETER_NOT_CONSISTENT")),     # not a lari account
        (dict(debtorAccount={"iban": other_gel["iban"]}), (400, "RESOURCE_UNKNOWN")),      # somebody else's account
        (dict(debtorAccount={"iban": OUTSIDE_IBAN}), (400, "RESOURCE_UNKNOWN")),           # not even our bank
        (dict(creditorAccount={"iban": mine[0]["iban"]}), (400, "PARAMETER_NOT_CONSISTENT")),  # to itself
        (dict(creditorAccount={"iban": other_usd["iban"]}), (400, "PARAMETER_NOT_CONSISTENT")),  # lari to a euro account
        (dict(creditorAccount={"iban": "GE00XX0000000000000000"}), (400, "FORMAT_ERROR")),
        (dict(instructedAmount={"currency": "GEL", "amount": "0"}), (400, "FORMAT_ERROR")),
        (dict(instructedAmount={"currency": "GEL", "amount": "abc"}), (400, "FORMAT_ERROR")),
        (dict(instructedAmount={"currency": "GEL", "amount": "1.234"}), (400, "FORMAT_ERROR")),
        (dict(instructedAmount={"currency": "GEL", "amount": huge}), (400, "PAYMENT_FAILED")),   # not enough money
        (dict(endToEndIdentification="x" * 36), (400, "FORMAT_ERROR")),
        (dict(creditorName=""), (400, "FORMAT_ERROR")),
        (dict(surprise=1), (400, "FORMAT_ERROR")),
    ]
    for changes, expected in cases:
        assert err(start(client, h, **changes)) == expected, changes
    assert own_gel                                   # (savings account exists and is lari)
    # Nothing was created by any of the refused requests.
    assert client.get("/admin/payments").json()["payments"] == []


def test_a_payment_can_go_to_another_account_of_the_same_customer(client):
    h, _ = connect(client)
    savings = accounts_of(client)[1]["iban"]
    assert start(client, h, creditorAccount={"iban": savings}).status_code == 201


def test_missing_headers(client):
    h, _ = connect(client)
    no_redirect = {k: v for k, v in pay_headers(h).items() if k != "TPP-Redirect-URI"}
    r = client.post("/v1/payments/domestic", json=payment_body(client), headers=no_redirect)
    assert err(r) == (400, "FORMAT_ERROR")
    no_consent = {k: v for k, v in pay_headers(h).items() if k != "Consent-ID"}
    assert err(client.post("/v1/payments/domestic", json=payment_body(client), headers=no_consent)) == (400, "FORMAT_ERROR")
    no_token = {k: v for k, v in pay_headers(h).items() if k != "Authorization"}
    assert err(client.post("/v1/payments/domestic", json=payment_body(client), headers=no_token)) == (401, "TOKEN_INVALID")
    bad_redirect = {**pay_headers(h), "TPP-Redirect-URI": "javascript:alert(1)"}
    assert err(client.post("/v1/payments/domestic", json=payment_body(client), headers=bad_redirect)) == (400, "FORMAT_ERROR")


def test_only_shared_accounts_can_pay(client):
    shared = accounts_of(client)[1]["resource_id"]            # the customer shares only the savings account
    h, _ = connect(client, account_ids=[shared])
    # Paying from the current account (not shared) looks like an unknown account.
    assert err(start(client, h)) == (400, "RESOURCE_UNKNOWN")
    ok = start(client, h, debtorAccount={"iban": accounts_of(client)[1]["iban"]})
    assert ok.status_code == 201


def test_a_payment_belongs_to_its_consent(client):
    h1, _ = connect(client)
    h2, _ = connect(client)
    pid = start(client, h1).json()["paymentId"]
    assert err(client.get(f"/v1/payments/domestic/{pid}", headers=read_headers(h2))) == (404, "RESOURCE_UNKNOWN")


def test_payments_do_not_count_as_reads(client):
    h, _ = connect(client, reads=4)
    for _ in range(6):
        assert start(client, h, amount="1.00").status_code == 201
    pid = start(client, h).json()["paymentId"]
    for _ in range(6):
        assert status_of(client, h, pid) == "RCVD"
    for _ in range(4):                                     # all 4 daily reads are still there
        assert client.get("/v1/accounts", headers=read_headers(h)).status_code == 200
    assert client.get("/v1/accounts", headers=read_headers(h)).status_code == 429


# ----- The customer's approval -----

def test_approving_completes_the_payment_and_moves_the_money(client):
    h, _ = connect(client)
    iban = accounts_of(client)[0]["iban"]
    before_booked, before_available = booked(client, iban), available(client, iban)
    created = start(client, h)
    redirect = decide(client, created)
    pid = created.json()["paymentId"]

    assert redirect.status_code == 303
    location = redirect.headers["location"]
    assert location.startswith(REDIRECT + "?") and f"paymentId={pid}" in location and "error" not in location
    assert status_of(client, h, pid) == "ACSC"
    assert booked(client, iban) == before_booked - Decimal("25.50")
    assert available(client, iban) == before_available - Decimal("25.50")


def test_the_payment_shows_up_in_the_transactions(client):
    h, _ = connect(client)
    account = accounts_of(client)[0]
    paid(client, h)
    r = client.get(f"/v1/accounts/{account['resource_id']}/transactions?bookingStatus=booked&dateFrom=2000-01-01",
                   headers=read_headers(h)).json()["transactions"]["booked"]
    newest = r[0]
    assert newest["transactionAmount"] == {"currency": "GEL", "amount": "-25.50"}
    assert newest["creditorName"] == "Example Shop" and newest["creditorAccount"]["iban"] == OUTSIDE_IBAN
    assert newest["remittanceInformationUnstructured"] == "Order 1"


def test_money_arrives_when_the_receiver_is_one_of_our_accounts(client):
    h, _ = connect(client)
    receiver = next(a for a in accounts_of(client, "c2") if a["currency"] == "GEL")
    before = booked(client, receiver["iban"])
    paid(client, h, creditorAccount={"iban": receiver["iban"]})
    assert booked(client, receiver["iban"]) == before + Decimal("25.50")


def test_reject_leaves_the_money_alone(client):
    h, _ = connect(client)
    iban = accounts_of(client)[0]["iban"]
    before = booked(client, iban)
    created = start(client, h)
    r = decide(client, created, "reject")
    assert r.status_code == 303 and "error=access_denied" in r.headers["location"]
    assert status_of(client, h, created.json()["paymentId"]) == "RJCT"
    assert booked(client, iban) == before
    assert client.get(link_path(created)).status_code == 400          # the page is closed now


def test_the_page_describes_the_payment_and_is_branded(client):
    h, _ = connect(client)
    page = client.get(link_path(start(client, h)))
    assert page.status_code == 200
    assert "25.50 GEL" in page.text and "Example Shop" in page.text
    assert "#553EFE" in page.text and "Test bank" in page.text


def test_only_the_owner_of_the_account_can_approve(client):
    h, _ = connect(client)
    created = start(client, h)
    other = customer(client, "c2")
    path = link_path(created)
    page = client.post(path + "/login", data={"username": other["username"], "password": other["password"]})
    assert "not from one of your accounts" in page.text
    r = client.post(path + "/decide", data={"decision": "approve"})
    assert r.status_code == 400 and "sign in" in r.text.lower()
    assert status_of(client, h, created.json()["paymentId"]) == "RCVD"


def test_wrong_password_and_deciding_without_signing_in(client):
    h, _ = connect(client)
    created = start(client, h)
    path = link_path(created)
    assert "Wrong username or password" in client.post(path + "/login", data={"username": "nino", "password": "x"}).text
    assert client.post(path + "/decide", data={"decision": "approve"}).status_code == 400
    assert client.get("/bank/pay/not-a-real-id").status_code == 400


def test_a_payment_nobody_approves_is_rejected_after_the_time_allowed(client):
    h, _ = connect(client)
    created = start(client, h)
    pid = created.json()["paymentId"]
    advance(client, minutes=14)
    assert status_of(client, h, pid) == "RCVD"
    advance(client, minutes=2)                 # past the 15 minutes
    assert status_of(client, h, pid) == "RJCT"
    assert client.get(link_path(created)).status_code == 400
    client.put("/admin/settings", json={"payment_approval_timeout_minutes": 60})
    created = start(client, h)
    advance(client, minutes=30)
    assert status_of(client, h, created.json()["paymentId"]) == "RCVD"


def test_not_enough_money_at_approval_time(client):
    h, _ = connect(client)
    iban = accounts_of(client)[0]["iban"]
    big = str((available(client, iban) * Decimal("0.6")).quantize(Decimal("0.01")))
    first, second = start(client, h, amount=big), start(client, h, amount=big)
    assert first.status_code == 201 and second.status_code == 201    # each is fine on its own
    decide(client, first)
    r = decide(client, second)
    assert "error=payment_failed" in r.headers["location"]
    assert status_of(client, h, second.json()["paymentId"]) == "RJCT"
    assert status_of(client, h, first.json()["paymentId"]) == "ACSC"


def test_auto_approve_skips_the_screens(client):
    client.put("/admin/settings", json={"auto_approve": True})
    h, _ = connect(client)
    created = start(client, h)
    r = client.get(link_path(created))
    assert r.status_code == 303 and "paymentId=" in r.headers["location"]
    assert status_of(client, h, created.json()["paymentId"]) == "ACSC"


def test_a_fresh_approval_link(client):
    h, _ = connect(client)
    created = start(client, h)
    pid = created.json()["paymentId"]
    again = client.post(f"/v1/payments/domestic/{pid}/authorisations", headers=read_headers(h))
    assert again.status_code == 201 and "/bank/pay/" in again.json()["_links"]["scaRedirect"]["href"]
    decide(client, created)
    assert err(client.post(f"/v1/payments/domestic/{pid}/authorisations", headers=read_headers(h))) == (409, "STATUS_INVALID")


# ----- Cancelling -----

def test_cancel_only_while_waiting(client):
    h, _ = connect(client)
    created = start(client, h)
    pid = created.json()["paymentId"]
    r = client.delete(f"/v1/payments/domestic/{pid}", headers=read_headers(h))
    assert r.status_code == 202 and r.json() == {"transactionStatus": "CANC"}
    assert status_of(client, h, pid) == "CANC"
    assert client.get(link_path(created)).status_code == 400                   # cannot be approved any more
    assert err(client.delete(f"/v1/payments/domestic/{pid}", headers=read_headers(h))) == (405, "CANCELLATION_INVALID")

    done = paid(client, h)
    assert err(client.delete(f"/v1/payments/domestic/{done}", headers=read_headers(h))) == (405, "CANCELLATION_INVALID")


# ----- Faults -----

def test_payment_stays_pending_fault(client):
    client.put("/admin/settings", json={"token_validity_minutes": 3000})   # the token must outlive the wait
    h, _ = connect(client)
    iban = accounts_of(client)[0]["iban"]
    before = booked(client, iban)
    client.post("/admin/faults", json={"kind": "payment_stays_pending", "times": 1})
    created = start(client, h)
    r = decide(client, created)
    assert r.status_code == 303                                   # the customer approved...
    pid = created.json()["paymentId"]
    assert status_of(client, h, pid) == "PDNG"                    # ...but the payment never completes
    assert booked(client, iban) == before                         # and no money moved
    advance(client, hours=5)
    assert status_of(client, h, pid) == "PDNG"                    # it stays pending, even much later
    assert err(client.delete(f"/v1/payments/domestic/{pid}", headers=read_headers(h))) == (405, "CANCELLATION_INVALID")
    assert paid(client, h)                                        # the fault was used up: the next payment is fine


def test_a_failed_answer_after_the_payment_was_made_leaves_a_payment_behind(client):
    """Fault phase "after": the bank made the payment, but the app was told it failed."""
    h, _ = connect(client)
    client.post("/admin/faults", json={"kind": "server_error", "operation": "payments.initiate",
                                       "phase": "after", "status_code": 503, "times": 1})
    r = start(client, h)
    assert r.status_code == 503 and r.content == b""
    listed = client.get("/admin/payments").json()["payments"]
    assert len(listed) == 1 and listed[0]["status"] == "RCVD"    # the payment exists; the app does not know its id


def test_a_fault_before_creates_nothing(client):
    h, _ = connect(client)
    client.post("/admin/faults", json={"kind": "server_error", "operation": "payments.initiate",
                                       "status_code": 503, "times": 1})
    assert start(client, h).status_code == 503
    assert client.get("/admin/payments").json()["payments"] == []


def test_a_429_on_a_payment_call_has_no_body(client):
    h, _ = connect(client)
    client.post("/admin/faults", json={"kind": "server_error", "operation": "payments.*",
                                       "status_code": 429, "times": 1})
    r = start(client, h)
    assert r.status_code == 429 and r.content == b""


# ----- Paying twice -----

def test_the_same_payment_made_twice_is_reported(client):
    h, _ = connect(client)
    paid(client, h)
    paid(client, h)
    paid(client, h, amount="99.00")                       # a different amount: not the same payment
    report = client.get("/admin/payments").json()
    assert report["completed_count"] == 3
    assert len(report["duplicates"]) == 1
    assert report["duplicates"][0]["times_paid"] == 2 and report["duplicates"][0]["amount"] == "25.50"


def test_a_naive_retry_after_a_failed_status_check_pays_twice(client):
    """The story this testbed exists for: the money moved, the app never learned it, so it paid again."""
    h, _ = connect(client)
    first = start(client, h)
    decide(client, first)                                  # the customer approves: money moves
    client.post("/admin/faults", json={"kind": "server_error", "operation": "payments.status",
                                       "status_code": 503, "times": 1})
    pid = first.json()["paymentId"]
    assert client.get(f"/v1/payments/domestic/{pid}/status", headers=read_headers(h)).status_code == 503
    # The app does not know what happened, so it starts the payment again, and the customer approves again.
    decide(client, start(client, h))
    report = client.get("/admin/payments").json()
    assert len(report["duplicates"]) == 1 and report["duplicates"][0]["times_paid"] == 2


def test_the_bank_does_not_catch_repeats_by_default_but_can(client):
    h, _ = connect(client)
    same = str(uuid.uuid4())
    one, two = start(client, h, request_id=same), start(client, h, request_id=same)
    assert one.json()["paymentId"] != two.json()["paymentId"]
    assert len(client.get("/admin/payments").json()["payments"]) == 2

    client.post("/admin/reset")
    h, _ = connect(client)
    client.put("/admin/settings", json={"deduplicate_by_request_id": True})
    one, two = start(client, h, request_id=same), start(client, h, request_id=same)
    assert one.json()["paymentId"] == two.json()["paymentId"]
    assert one.json()["_links"]["scaRedirect"] == two.json()["_links"]["scaRedirect"]
    assert len(client.get("/admin/payments").json()["payments"]) == 1


def test_admin_payments_lists_what_the_bank_has_seen(client):
    h, _ = connect(client)
    pid = paid(client, h, request_id=None)
    listed = client.get("/admin/payments").json()["payments"]
    assert listed[0]["payment_id"] == pid and listed[0]["status"] == "ACSC" and listed[0]["amount"] == "25.50"
    assert "password" not in str(listed)
