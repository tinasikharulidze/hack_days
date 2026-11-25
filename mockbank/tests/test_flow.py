"""Step 3 tests, part 2: the approval pages, the token step and the access check."""

import pytest
from fastapi import Depends

from app.auth import require_access
from app.main import create_app
from tests.helpers import (advance, approval_path, approve, code_from, create_consent, customer,
                           get_tokens, request_id)

STATUS = "/v1/consents/{}/status"


def status_of(client, consent_id):
    return client.get(STATUS.format(consent_id), headers=request_id()).json()["consentStatus"]


def token_call(client, **form):
    return client.post("/oauth/token", data=form)


# ----- The approval pages -----

def test_sign_in_page_is_branded_and_describes_the_request(client):
    created = create_consent(client)
    page = client.get(approval_path(created))
    assert page.status_code == 200
    assert "#553EFE" in page.text and "#FD6AD2" in page.text      # brand violet and pink
    assert "Test bank" in page.text and "balances" in page.text


def test_wrong_password_shows_an_error_and_no_progress(client):
    created = create_consent(client)
    path = approval_path(created)
    r = client.post(path + "/login", data={"username": "nino", "password": "wrong"})
    assert "Wrong username or password" in r.text
    # Deciding without signing in is refused.
    r = client.post(path + "/decide", data={"decision": "approve"})
    assert r.status_code == 400 and "sign in" in r.text.lower()


def test_unknown_link_gives_a_closed_page(client):
    r = client.get("/bank/approve/not-a-real-id")
    assert r.status_code == 400 and "closed" in r.text.lower()


def test_approve_sends_the_customer_back_with_a_code(client):
    created = create_consent(client)
    redirect = approve(client, created)
    assert redirect.status_code == 303
    location = redirect.headers["location"]
    assert location.startswith("https://app.example/callback?")
    assert "code=" in location and f"consentId={created.json()['consentId']}" in location
    assert status_of(client, created.json()["consentId"]) == "valid"


def test_reject_sends_an_error_and_the_consent_is_rejected(client):
    created = create_consent(client)
    person = customer(client)
    path = approval_path(created)
    client.post(path + "/login", data={"username": person["username"], "password": person["password"]})
    r = client.post(path + "/decide", data={"decision": "reject"})
    assert r.status_code == 303 and "error=access_denied" in r.headers["location"]
    assert status_of(client, created.json()["consentId"]) == "rejected"
    # The page cannot be used again.
    assert client.get(path).status_code == 400


def test_customer_can_share_fewer_accounts_than_asked(client):
    created = create_consent(client)
    person = customer(client)
    one = person["accounts"][0]["resource_id"]
    approve(client, created, account_ids=[one])
    from app import store
    consent = store.STATE.consents[created.json()["consentId"]]
    assert consent.granted_account_ids == [one]


def test_approving_with_no_account_chosen_is_refused(client):
    created = create_consent(client)
    person = customer(client)
    path = approval_path(created)
    client.post(path + "/login", data={"username": person["username"], "password": person["password"]})
    r = client.post(path + "/decide", data={"decision": "approve"})
    assert r.status_code == 200 and "at least one account" in r.text
    assert status_of(client, created.json()["consentId"]) == "received"


def test_another_customer_cannot_approve_accounts_that_are_not_theirs(client):
    created = create_consent(client, customer_id="c1")
    other = customer(client, "c2")
    path = approval_path(created)
    page = client.post(path + "/login", data={"username": other["username"], "password": other["password"]})
    assert "None of the accounts" in page.text and 'value="approve"' not in page.text
    # Even a hand-made request is refused: nothing chosen is allowed.
    forged = client.post(path + "/decide", data={"decision": "approve",
                                                 "account": customer(client)["accounts"][0]["resource_id"]})
    assert "at least one account" in forged.text


def test_redirect_uri_keeps_its_own_query(client):
    from tests.helpers import consent_body, headers
    created = client.post("/v1/consents", json=consent_body(client),
                          headers=headers("https://app.example/cb?session=42"))
    location = approve(client, created).headers["location"]
    assert "session=42" in location and "code=" in location


def test_auto_approve_skips_the_screens(client):
    client.put("/admin/settings", json={"auto_approve": True})
    created = create_consent(client)
    r = client.get(approval_path(created))
    assert r.status_code == 303 and "code=" in r.headers["location"]
    assert status_of(client, created.json()["consentId"]) == "valid"


# ----- The token step -----

def test_code_becomes_tokens(client):
    consent_id, tokens = get_tokens(client)
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["access_token"] and tokens["refresh_token"] and tokens["consent_id"] == consent_id


def test_a_code_works_only_once(client):
    created = create_consent(client)
    code = code_from(approve(client, created))
    assert token_call(client, grant_type="authorization_code", code=code).status_code == 200
    again = token_call(client, grant_type="authorization_code", code=code)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_a_code_expires(client):
    created = create_consent(client)
    code = code_from(approve(client, created))
    advance(client, minutes=11)
    r = token_call(client, grant_type="authorization_code", code=code)
    assert r.json()["error"] == "invalid_grant"


def test_token_step_errors_use_the_oauth_format(client):
    assert token_call(client, grant_type="password").json()["error"] == "unsupported_grant_type"
    assert token_call(client, grant_type="authorization_code").json()["error"] == "invalid_request"
    assert token_call(client, grant_type="authorization_code", code="nope").json()["error"] == "invalid_grant"
    assert token_call(client, grant_type="refresh_token").json()["error"] == "invalid_request"
    assert token_call(client, grant_type="refresh_token", refresh_token="nope").json()["error"] == "invalid_grant"


def test_refresh_gives_a_new_access_token(client):
    consent_id, tokens = get_tokens(client)
    advance(client, minutes=61)          # the access token has run out, the consent has not
    r = token_call(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert r.status_code == 200
    assert r.json()["access_token"] != tokens["access_token"]
    assert status_of(client, consent_id) == "valid"


def test_refresh_stops_when_the_consent_ends(client):
    consent_id, tokens = get_tokens(client)
    client.delete(f"/v1/consents/{consent_id}", headers=request_id())
    r = token_call(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert r.json()["error"] == "invalid_grant"


def test_fault_on_the_token_step(client):
    client.post("/admin/faults", json={"kind": "server_error", "operation": "oauth.token",
                                       "status_code": 503, "times": 1})
    r = token_call(client, grant_type="refresh_token", refresh_token="x")
    assert r.status_code == 503 and r.content == b""


# ----- How long a consent lives -----

def test_recurring_consent_expires_at_its_end_date(client):
    consent_id, _ = get_tokens(client)               # valid for 30 days in the helper
    advance(client, days=29)
    assert status_of(client, consent_id) == "valid"
    advance(client, days=2)
    assert status_of(client, consent_id) == "expired"


def test_one_off_consent_lasts_one_hour(client):
    consent_id, _ = get_tokens(client, recurringIndicator=False, frequencyPerDay=1)
    advance(client, minutes=59)
    assert status_of(client, consent_id) == "valid"
    advance(client, minutes=2)
    assert status_of(client, consent_id) == "expired"


def test_consent_expires_early_fault(client):
    client.post("/admin/faults", json={"kind": "consent_expires_early", "times": 1})
    created = create_consent(client)
    approve(client, created)
    consent_id = created.json()["consentId"]
    assert status_of(client, consent_id) == "valid"
    advance(client, minutes=3)                       # normally valid for 30 days
    assert status_of(client, consent_id) == "expired"


# ----- The access check (used by steps 4 and 5) -----

@pytest.fixture()
def probe(app, client):
    """The real app plus one test-only route that is guarded by require_access."""
    @app.get("/_probe")
    def probe_route(consent=Depends(require_access)):
        return {"consent": consent.consent_id}
    return client


def probe_call(client, token=None, consent_id=None):
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if consent_id:
        h["Consent-ID"] = consent_id
    return client.get("/_probe", headers=h)


def err(response):
    return response.status_code, response.json()["tppMessages"][0]["code"]


def test_access_check_accepts_a_good_token_and_consent(probe):
    consent_id, tokens = get_tokens(probe)
    r = probe_call(probe, tokens["access_token"], consent_id)
    assert r.status_code == 200 and r.json() == {"consent": consent_id}


def test_access_check_token_problems(probe):
    consent_id, tokens = get_tokens(probe)
    assert err(probe_call(probe, None, consent_id)) == (401, "TOKEN_INVALID")
    assert err(probe_call(probe, "made-up", consent_id)) == (401, "TOKEN_UNKNOWN")
    advance(probe, minutes=61)
    assert err(probe_call(probe, tokens["access_token"], consent_id)) == (401, "TOKEN_EXPIRED")


def test_access_check_consent_problems(probe):
    consent_id, tokens = get_tokens(probe)
    other_id, other_tokens = get_tokens(probe, customer_id="c2")
    assert err(probe_call(probe, tokens["access_token"])) == (400, "FORMAT_ERROR")           # no Consent-ID
    assert err(probe_call(probe, tokens["access_token"], "nope")) == (403, "CONSENT_UNKNOWN")
    # A token for one consent cannot open another one.
    assert err(probe_call(probe, tokens["access_token"], other_id)) == (401, "CONSENT_INVALID")
    # A deleted consent no longer works, even with a token that has not expired.
    probe.delete(f"/v1/consents/{consent_id}", headers=request_id())
    assert err(probe_call(probe, tokens["access_token"], consent_id)) == (401, "CONSENT_INVALID")


def test_access_check_expired_consent(probe):
    # Make tokens outlive the one-hour consent, so the consent is what runs out first.
    probe.put("/admin/settings", json={"token_validity_minutes": 240})
    consent_id, tokens = get_tokens(probe, recurringIndicator=False, frequencyPerDay=1)
    advance(probe, minutes=30)
    assert probe_call(probe, tokens["access_token"], consent_id).status_code == 200
    advance(probe, minutes=40)                       # past the one hour of a one-off consent
    assert err(probe_call(probe, tokens["access_token"], consent_id)) == (401, "CONSENT_EXPIRED")


def test_admin_data_lists_the_fake_sign_ins(client):
    people = client.get("/admin/data").json()["customers"]
    assert all(p["username"] and p["password"] for p in people)
