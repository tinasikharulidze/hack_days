"""Step 4 tests: the account calls, what a consent allows, and the daily read limit."""

from datetime import timedelta

from app import store
from tests.helpers import advance, connect, customer, today


def err(response):
    """(HTTP status, standard error code) of an error answer."""
    return response.status_code, response.json()["tppMessages"][0]["code"]


def account_ids(client, customer_id="c1"):
    return [a["resource_id"] for a in customer(client, customer_id)["accounts"]]


def seed_transactions(account_id, status):
    return [t for t in store.STATE.transactions if t.account_id == account_id and t.status == status]


# ----- Account list and details -----

def test_list_shows_the_shared_accounts_with_standard_fields(client):
    h, _ = connect(client)
    r = client.get("/v1/accounts", headers=h)
    assert r.status_code == 200
    accounts = r.json()["accounts"]
    assert [a["resourceId"] for a in accounts] == account_ids(client)
    first = accounts[0]
    assert set(first) >= {"resourceId", "iban", "currency", "name", "product", "cashAccountType",
                          "status", "ownerName", "_links"}
    assert set(first["_links"]) == {"balances", "transactions"}
    assert r.headers["X-Request-ID"] == h["X-Request-ID"]


def test_list_only_shows_what_the_customer_chose_to_share(client):
    one = account_ids(client)[1]
    h, _ = connect(client, account_ids=[one])
    assert [a["resourceId"] for a in client.get("/v1/accounts", headers=h).json()["accounts"]] == [one]


def test_all_accounts_consent_lists_but_cannot_read_balances(client):
    h, _ = connect(client, access={"availableAccounts": "allAccounts"})
    assert len(client.get("/v1/accounts", headers=h).json()["accounts"]) == 3
    first = account_ids(client)[0]
    assert client.get(f"/v1/accounts/{first}", headers=h).status_code == 200
    assert err(client.get(f"/v1/accounts/{first}/balances", headers=h)) == (401, "CONSENT_INVALID")


def test_account_details(client):
    h, _ = connect(client)
    first = account_ids(client)[0]
    r = client.get(f"/v1/accounts/{first}", headers=h)
    assert r.status_code == 200 and r.json()["account"]["resourceId"] == first


def test_an_account_that_was_not_shared_looks_unknown(client):
    ids = account_ids(client)
    h, _ = connect(client, account_ids=[ids[0]])
    for path in (f"/v1/accounts/{ids[1]}", f"/v1/accounts/{ids[1]}/balances",
                 f"/v1/accounts/{ids[1]}/transactions?bookingStatus=booked&dateFrom=2020-01-01"):
        assert err(client.get(path, headers=h)) == (404, "RESOURCE_UNKNOWN"), path
    # Another customer's account, and an account that does not exist at all, look the same.
    other = account_ids(client, "c2")[0]
    assert err(client.get(f"/v1/accounts/{other}", headers=h)) == (404, "RESOURCE_UNKNOWN")
    assert err(client.get("/v1/accounts/nope", headers=h)) == (404, "RESOURCE_UNKNOWN")


def test_a_consent_without_balances_cannot_read_them(client):
    ibans = [{"iban": a["iban"]} for a in customer(client)["accounts"]]
    h, _ = connect(client, access={"accounts": ibans})
    first = account_ids(client)[0]
    assert client.get(f"/v1/accounts/{first}", headers=h).status_code == 200
    assert err(client.get(f"/v1/accounts/{first}/balances", headers=h)) == (401, "CONSENT_INVALID")
    assert err(client.get(f"/v1/accounts/{first}/transactions?bookingStatus=booked&dateFrom=2020-01-01",
                          headers=h)) == (401, "CONSENT_INVALID")
    # Links are only offered for what the consent allows.
    assert client.get(f"/v1/accounts/{first}", headers=h).json()["account"]["_links"] == {}


# ----- Balances -----

def test_balances_match_the_sample_data(client):
    h, _ = connect(client)
    admin = {a["resource_id"]: a for a in client.get("/admin/data").json()["accounts"]}
    for rid, account in admin.items():
        if account["customer_id"] != "c1":
            continue
        body = client.get(f"/v1/accounts/{rid}/balances", headers=h).json()
        assert body["account"] == {"iban": account["iban"], "currency": account["currency"]}
        by_type = {b["balanceType"]: b["balanceAmount"] for b in body["balances"]}
        assert set(by_type) == {"interimBooked", "interimAvailable"}
        assert by_type["interimBooked"]["amount"] == account["balances"]["interimBooked"]
        assert by_type["interimAvailable"]["currency"] == account["currency"]
        assert len(by_type["interimBooked"]["amount"].split(".")[1]) == 2   # text with two decimals


# ----- Transactions -----

def tx_url(account_id, **query):
    query = {"bookingStatus": "both", "dateFrom": "2000-01-01", **query}
    return f"/v1/accounts/{account_id}/transactions?" + "&".join(f"{k}={v}" for k, v in query.items())


def test_booked_pending_and_both(client):
    h, _ = connect(client)
    first = account_ids(client)[0]
    booked, pending = seed_transactions(first, "booked"), seed_transactions(first, "pending")
    assert pending  # the current account really has pending transactions

    r = client.get(tx_url(first, bookingStatus="booked"), headers=h).json()["transactions"]
    assert len(r["booked"]) == len(booked) and "pending" not in r
    r = client.get(tx_url(first, bookingStatus="pending"), headers=h).json()["transactions"]
    assert len(r["pending"]) == len(pending) and "booked" not in r
    r = client.get(tx_url(first, bookingStatus="both"), headers=h).json()["transactions"]
    assert len(r["booked"]) == len(booked) and len(r["pending"]) == len(pending)
    assert r["_links"]["account"]["href"].endswith(f"/v1/accounts/{first}")
    # Pending transactions have no booking date; booked ones do.
    assert all("bookingDate" not in t for t in r["pending"])
    assert all("bookingDate" in t for t in r["booked"])
    # Newest first.
    dates = [t["bookingDate"] for t in r["booked"]]
    assert dates == sorted(dates, reverse=True)


def test_transaction_fields_and_sides(client):
    h, _ = connect(client)
    first = account_ids(client)[0]
    items = client.get(tx_url(first), headers=h).json()["transactions"]["booked"]
    out = next(t for t in items if t["transactionAmount"]["amount"].startswith("-"))
    money_in = next(t for t in items if not t["transactionAmount"]["amount"].startswith("-"))
    assert set(out) >= {"transactionId", "bookingDate", "valueDate", "transactionAmount",
                        "creditorName", "creditorAccount", "remittanceInformationUnstructured"}
    assert "debtorName" not in out
    assert "debtorName" in money_in and "creditorName" not in money_in


def test_date_filters(client):
    h, _ = connect(client)
    first = account_ids(client)[0]
    booked = sorted(seed_transactions(first, "booked"), key=lambda t: t.booking_date)
    middle = booked[len(booked) // 2].booking_date
    expected = [t for t in booked if t.booking_date >= middle]
    r = client.get(tx_url(first, bookingStatus="booked", dateFrom=middle.isoformat()), headers=h)
    assert len(r.json()["transactions"]["booked"]) == len(expected)
    # dateTo is included: asking for exactly one day returns that day's transactions.
    day = [t for t in booked if t.booking_date == middle]
    r = client.get(tx_url(first, bookingStatus="booked", dateFrom=middle.isoformat(),
                          dateTo=middle.isoformat()), headers=h)
    assert len(r.json()["transactions"]["booked"]) == len(day)
    # Nothing in the future.
    future = (today() + timedelta(days=30)).isoformat()
    r = client.get(tx_url(first, bookingStatus="both", dateFrom=future), headers=h)
    assert r.json()["transactions"]["booked"] == [] and r.json()["transactions"]["pending"] == []


def test_transaction_query_errors(client):
    h, _ = connect(client)
    first = account_ids(client)[0]
    base = f"/v1/accounts/{first}/transactions"
    assert err(client.get(base + "?dateFrom=2020-01-01", headers=h)) == (400, "FORMAT_ERROR")      # no bookingStatus
    assert err(client.get(base + "?bookingStatus=booked", headers=h)) == (400, "FORMAT_ERROR")     # no dateFrom
    assert err(client.get(tx_url(first, bookingStatus="xyz"), headers=h)) == (400, "FORMAT_ERROR")
    assert err(client.get(tx_url(first, bookingStatus="all"), headers=h)) == (400, "PARAMETER_NOT_SUPPORTED")
    assert err(client.get(tx_url(first, deltaList="true"), headers=h)) == (400, "PARAMETER_NOT_SUPPORTED")
    assert err(client.get(tx_url(first, dateFrom="2026-02-01", dateTo="2026-01-01"), headers=h)) == (400, "PERIOD_INVALID")


def test_supported_booking_statuses_is_a_setting(client):
    h, _ = connect(client)
    first = account_ids(client)[0]
    client.put("/admin/settings", json={"supported_booking_statuses": ["booked"]})
    assert err(client.get(tx_url(first, bookingStatus="pending"), headers=h)) == (400, "PARAMETER_NOT_SUPPORTED")
    assert client.get(tx_url(first, bookingStatus="booked"), headers=h).status_code == 200


def test_one_transaction(client):
    h, _ = connect(client)
    ids = account_ids(client)
    items = client.get(tx_url(ids[0], bookingStatus="booked"), headers=h).json()["transactions"]["booked"]
    wanted = items[0]
    r = client.get(f"/v1/accounts/{ids[0]}/transactions/{wanted['transactionId']}", headers=h)
    assert r.status_code == 200 and r.json()["transactionsDetails"] == wanted
    assert err(client.get(f"/v1/accounts/{ids[0]}/transactions/nope", headers=h)) == (404, "RESOURCE_UNKNOWN")
    # A transaction id from another account is not found on this one.
    assert err(client.get(f"/v1/accounts/{ids[1]}/transactions/{wanted['transactionId']}", headers=h)) == (404, "RESOURCE_UNKNOWN")


# ----- withBalance -----

def test_with_balance_is_refused_unless_the_setting_allows_it(client):
    h, _ = connect(client)
    first = account_ids(client)[0]
    assert err(client.get("/v1/accounts?withBalance=true", headers=h)) == (400, "PARAMETER_NOT_SUPPORTED")
    assert client.get("/v1/accounts?withBalance=false", headers=h).status_code == 200

    client.put("/admin/settings", json={"with_balance_supported": True})
    listed = client.get("/v1/accounts?withBalance=true", headers=h).json()["accounts"]
    assert all("balances" in a for a in listed)
    report = client.get(tx_url(first, withBalance="true"), headers=h).json()
    assert len(report["balances"]) == 2


# ----- Daily limit -----

def test_the_fifth_read_of_the_day_is_refused(client):
    h, _ = connect(client, reads=4)
    for _ in range(4):
        assert client.get("/v1/accounts", headers=h).status_code == 200
    r = client.get("/v1/accounts", headers=h)
    assert err(r) == (429, "ACCESS_EXCEEDED")
    # Every kind of read counts the same, and the refusal is not counted.
    first = account_ids(client)[0]
    assert client.get(f"/v1/accounts/{first}/balances", headers=h).status_code == 429


def test_the_limit_starts_again_the_next_day(client):
    client.put("/admin/settings", json={"token_validity_minutes": 3000})   # tokens outlive the wait
    h, _ = connect(client, reads=4)
    for _ in range(4):
        client.get("/v1/accounts", headers=h)
    assert client.get("/v1/accounts", headers=h).status_code == 429
    advance(client, hours=25)
    assert client.get("/v1/accounts", headers=h).status_code == 200


def test_a_one_off_consent_allows_one_read(client):
    h, _ = connect(client, recurringIndicator=False, frequencyPerDay=1, reads=1)
    assert client.get("/v1/accounts", headers=h).status_code == 200
    assert err(client.get("/v1/accounts", headers=h)) == (429, "ACCESS_EXCEEDED")


def test_calls_that_fail_the_access_check_are_not_counted(client):
    h, _ = connect(client, reads=4)
    bad = {**h, "Authorization": "Bearer nonsense"}
    for _ in range(6):
        assert err(client.get("/v1/accounts", headers=bad)) == (401, "TOKEN_UNKNOWN")
    for _ in range(4):
        assert client.get("/v1/accounts", headers=h).status_code == 200


def test_the_limit_is_per_consent(client):
    h1, _ = connect(client, reads=4)
    h2, _ = connect(client, reads=4)
    for _ in range(4):
        client.get("/v1/accounts", headers=h1)
    assert client.get("/v1/accounts", headers=h1).status_code == 429
    assert client.get("/v1/accounts", headers=h2).status_code == 200


# ----- Access check, faults -----

def test_account_calls_need_token_and_consent(client):
    h, _ = connect(client)
    no_token = {k: v for k, v in h.items() if k != "Authorization"}
    assert err(client.get("/v1/accounts", headers=no_token)) == (401, "TOKEN_INVALID")
    no_consent = {k: v for k, v in h.items() if k != "Consent-ID"}
    assert err(client.get("/v1/accounts", headers=no_consent)) == (400, "FORMAT_ERROR")
    no_request_id = {k: v for k, v in h.items() if k != "X-Request-ID"}
    assert err(client.get("/v1/accounts", headers=no_request_id)) == (400, "FORMAT_ERROR")
    advance(client, minutes=61)
    assert err(client.get("/v1/accounts", headers=h)) == (401, "TOKEN_EXPIRED")


def test_faults_reach_account_calls(client):
    h, _ = connect(client)
    first = account_ids(client)[0]
    client.post("/admin/faults", json={"kind": "server_error", "operation": "accounts.balances",
                                       "status_code": 503, "times": 1})
    r = client.get(f"/v1/accounts/{first}/balances", headers=h)
    assert r.status_code == 503 and r.content == b""
    assert client.get(f"/v1/accounts/{first}/balances", headers=h).status_code == 200
    # A 429 fault on an account call carries the standard code.
    client.post("/admin/faults", json={"kind": "server_error", "operation": "accounts.list",
                                       "status_code": 429, "times": 1})
    assert err(client.get("/v1/accounts", headers=h)) == (429, "ACCESS_EXCEEDED")


def test_an_expired_consent_stops_account_calls(client):
    client.put("/admin/settings", json={"token_validity_minutes": 240})
    h, _ = connect(client, recurringIndicator=False, frequencyPerDay=1, reads=50)
    advance(client, minutes=61)
    assert err(client.get("/v1/accounts", headers=h)) == (401, "CONSENT_EXPIRED")
