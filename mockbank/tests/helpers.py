"""Small helpers shared by the step 3 tests: make a consent, approve it, get a token."""

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse


def today():
    return datetime.now(timezone.utc).date()


def headers(redirect="https://app.example/callback"):
    """The headers the standard requires on a create-consent call."""
    return {"X-Request-ID": str(uuid.uuid4()), "PSU-IP-Address": "192.0.2.1",
            "TPP-Redirect-URI": redirect}


def request_id():
    return {"X-Request-ID": str(uuid.uuid4())}


def customer(client, customer_id="c1"):
    """The customer's sign-in and accounts, read from /admin/data."""
    data = client.get("/admin/data").json()
    person = next(c for c in data["customers"] if c["id"] == customer_id)
    person["accounts"] = [a for a in data["accounts"] if a["customer_id"] == customer_id]
    return person


def consent_body(client, customer_id="c1", **changes):
    """A valid consent request for one customer's accounts. Override any field with keyword arguments."""
    ibans = [{"iban": a["iban"]} for a in customer(client, customer_id)["accounts"]]
    body = {
        "access": {"accounts": ibans, "balances": ibans, "transactions": ibans},
        "recurringIndicator": True,
        "validUntil": (today() + timedelta(days=30)).isoformat(),
        "frequencyPerDay": 4,
        "combinedServiceIndicator": False,
    }
    body.update(changes)
    return body


def create_consent(client, customer_id="c1", **changes):
    """Create a consent and return the whole response."""
    return client.post("/v1/consents", json=consent_body(client, customer_id, **changes), headers=headers())


def approval_path(created):
    """The path of the approval page, taken from the scaRedirect link in the create answer."""
    return urlparse(created.json()["_links"]["scaRedirect"]["href"]).path


def approve(client, created, customer_id="c1", account_ids=None):
    """Play the customer: sign in, choose accounts, press Approve. Returns the redirect answer."""
    person = customer(client, customer_id)
    path = approval_path(created)
    client.post(path + "/login", data={"username": person["username"], "password": person["password"]})
    chosen = account_ids or [a["resource_id"] for a in person["accounts"]]
    return client.post(path + "/decide", data={"decision": "approve", "account": chosen})


def code_from(redirect_response):
    """The one-time code in the redirect back to the app."""
    return parse_qs(urlparse(redirect_response.headers["location"]).query)["code"][0]


def get_tokens(client, customer_id="c1", **changes):
    """Whole flow in one go: create, approve, swap the code. Returns (consent_id, token answer)."""
    created = create_consent(client, customer_id, **changes)
    assert created.status_code == 201, created.text
    redirect = approve(client, created, customer_id)
    answer = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code_from(redirect)})
    return created.json()["consentId"], answer.json()


def advance(client, **kwargs):
    """Move the mock clock forward, e.g. advance(client, minutes=90)."""
    minutes = kwargs.get("minutes", 0) + kwargs.get("hours", 0) * 60 + kwargs.get("days", 0) * 1440
    assert client.post("/admin/time/advance", json={"minutes": minutes}).status_code == 200


def connect(client, customer_id="c1", account_ids=None, reads=50, **consent_changes):
    """The whole journey in one go, for the account tests.

    Creates a consent, approves it, swaps the code for a token, and returns the
    headers an app would send on every account call, plus the consent id.
    `reads` is how many reads a day the consent allows (50 so most tests never hit the limit).
    """
    if reads > 4:   # the bank's daily limit is a setting, so raise it for this consent
        client.put("/admin/settings", json={"max_consent_requests_per_day": reads})
    changes = {"frequencyPerDay": reads, **consent_changes}
    created = create_consent(client, customer_id, **changes)
    assert created.status_code == 201, created.text
    code = code_from(approve(client, created, customer_id, account_ids))
    token = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code}).json()
    consent_id = created.json()["consentId"]
    return {"Authorization": f"Bearer {token['access_token']}", "Consent-ID": consent_id,
            "X-Request-ID": str(uuid.uuid4())}, token
