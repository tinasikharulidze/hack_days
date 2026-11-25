"""Step 3 tests, part 1: the consent calls and their rules."""

from datetime import timedelta

from tests.helpers import (advance, consent_body, create_consent, customer, headers, request_id, today)


def codes(response):
    """The standard error code in an answer."""
    return response.json()["tppMessages"][0]["code"]


# ----- Creating -----

def test_create_consent_starts_as_received(client):
    r = create_consent(client)
    assert r.status_code == 201
    body = r.json()
    assert body["consentStatus"] == "received"
    assert set(body["_links"]) >= {"scaRedirect", "self", "status", "startAuthorisation"}
    assert body["_links"]["scaRedirect"]["href"].startswith("http://testserver/bank/approve/")
    assert r.headers["ASPSP-SCA-Approach"] == "REDIRECT"
    assert r.headers["Location"].endswith(body["consentId"])


def test_missing_required_header_is_a_format_error(client):
    h = headers()
    del h["TPP-Redirect-URI"]
    r = client.post("/v1/consents", json=consent_body(client), headers=h)
    assert r.status_code == 400 and codes(r) == "FORMAT_ERROR"


def test_redirect_uri_must_be_a_web_address(client):
    r = client.post("/v1/consents", json=consent_body(client), headers=headers("javascript:alert(1)"))
    assert r.status_code == 400 and codes(r) == "FORMAT_ERROR"


def test_unsupported_and_inconsistent_requests(client):
    cases = [
        (dict(combinedServiceIndicator=True), "PARAMETER_NOT_SUPPORTED"),
        (dict(frequencyPerDay=5), "PARAMETER_NOT_CONSISTENT"),          # more than the 4 a day allowed
        (dict(recurringIndicator=False, frequencyPerDay=2), "PARAMETER_NOT_CONSISTENT"),  # one-off = 1 read
        (dict(validUntil=(today() - timedelta(days=1)).isoformat()), "PARAMETER_NOT_CONSISTENT"),
        (dict(access={}), "PARAMETER_NOT_CONSISTENT"),                  # asks for nothing
        (dict(access={"accounts": [{"iban": "GE00XX0000000000000000"}]}), "RESOURCE_UNKNOWN"),
    ]
    for changes, expected in cases:
        r = create_consent(client, **changes)
        assert (r.status_code, codes(r)) == (400, expected), changes


def test_accounts_of_two_customers_in_one_consent_are_refused(client):
    mixed = [{"iban": customer(client, c)["accounts"][0]["iban"]} for c in ("c1", "c2")]
    r = create_consent(client, access={"accounts": mixed})
    assert r.status_code == 400 and codes(r) == "PARAMETER_NOT_CONSISTENT"


def test_daily_limit_follows_the_setting(client):
    client.put("/admin/settings", json={"max_consent_requests_per_day": 2})
    assert create_consent(client, frequencyPerDay=2).status_code == 201
    assert create_consent(client, frequencyPerDay=3).status_code == 400


def test_far_future_valid_until_is_cut_back_to_90_days(client):
    created = create_consent(client, validUntil=(today() + timedelta(days=400)).isoformat())
    info = client.get(f"/v1/consents/{created.json()['consentId']}", headers=request_id()).json()
    assert info["validUntil"] == (today() + timedelta(days=90)).isoformat()


def test_available_accounts_request_is_accepted(client):
    r = create_consent(client, access={"availableAccounts": "allAccounts"})
    assert r.status_code == 201


# ----- Reading, status, deleting -----

def test_read_status_and_unknown_consent(client):
    consent_id = create_consent(client).json()["consentId"]
    info = client.get(f"/v1/consents/{consent_id}", headers=request_id())
    assert info.status_code == 200 and info.json()["consentStatus"] == "received"
    assert info.json()["recurringIndicator"] is True
    status = client.get(f"/v1/consents/{consent_id}/status", headers=request_id())
    assert status.json() == {"consentStatus": "received"}

    missing = client.get("/v1/consents/nope/status", headers=request_id())
    assert missing.status_code == 403 and codes(missing) == "CONSENT_UNKNOWN"


def test_delete_ends_the_consent(client):
    consent_id = create_consent(client).json()["consentId"]
    r = client.delete(f"/v1/consents/{consent_id}", headers=request_id())
    assert r.status_code == 204 and r.content == b""
    status = client.get(f"/v1/consents/{consent_id}/status", headers=request_id()).json()
    assert status["consentStatus"] == "terminatedByTpp"


def test_start_authorisation_again(client):
    consent_id = create_consent(client).json()["consentId"]
    r = client.post(f"/v1/consents/{consent_id}/authorisations", headers=request_id())
    assert r.status_code == 201
    assert r.json()["scaStatus"] == "received"
    assert "/bank/approve/" in r.json()["_links"]["scaRedirect"]["href"]
    # Once the consent is deleted it can no longer be approved.
    client.delete(f"/v1/consents/{consent_id}", headers=request_id())
    again = client.post(f"/v1/consents/{consent_id}/authorisations", headers=request_id())
    assert again.status_code == 409 and codes(again) == "STATUS_INVALID"


# ----- Time -----

def test_unapproved_consent_expires_after_the_setup_time(client):
    consent_id = create_consent(client).json()["consentId"]
    advance(client, hours=23)
    assert client.get(f"/v1/consents/{consent_id}/status", headers=request_id()).json()["consentStatus"] == "received"
    advance(client, hours=2)
    assert client.get(f"/v1/consents/{consent_id}/status", headers=request_id()).json()["consentStatus"] == "expired"


def test_faults_reach_consent_calls(client):
    client.post("/admin/faults", json={"kind": "server_error", "operation": "consents.create",
                                       "status_code": 503, "times": 1})
    r = create_consent(client)
    assert r.status_code == 503 and r.content == b""      # no body, as the standard says
    assert create_consent(client).status_code == 201       # the fault used itself up
