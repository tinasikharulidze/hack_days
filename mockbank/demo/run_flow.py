"""See the whole mock bank work: one script that plays an app AND a customer.

Run it while the mock is running (from the mockbank folder):

    uv run python -m demo.run_flow                      # uses http://127.0.0.1:8000
    uv run python -m demo.run_flow http://127.0.0.1:9000

It tells the story in two parts:

  Part 1  A careful app:  asks permission, the customer approves, the app reads
          the accounts, makes a payment, survives an expired token.
  Part 2  A careless app: a status check fails right after the money moved. The
          app does not know the payment went through, so it pays again. The bank's
          duplicate report catches it.

It also works as a copy-and-adapt example for your own app. Each step is a
plain HTTP call. The few lines marked "the customer does this" are what a real
person would do in their browser on the bank's pages.

It starts with a reset, so it wipes whatever the mock held before.
"""

import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx

APP_ADDRESS = "http://localhost:3000/callback"   # where the bank sends the customer back (nobody listens)


def _outside_iban() -> str:
    """A well-formed Georgian IBAN that is not one of the bank's accounts (a shop at another bank).

    An IBAN is "GE", two check digits, then the account part. The check digits
    are 98 minus (the number made from the account part + "GE00", letters turned
    into numbers A=10..Z=35) modulo 97. The bank checks them, so a made-up number would be refused.
    """
    account_part = "XX0000111122223333"
    as_number = int("".join(str(int(ch, 36)) for ch in account_part + "GE00"))
    return f"GE{98 - as_number % 97:02d}{account_part}"


class Story:
    """Runs the steps and prints them in plain words (or stays quiet when told to)."""

    def __init__(self, client: httpx.Client, quiet: bool = False):
        self.http = client
        self.quiet = quiet
        self.step_number = 0
        self.headers: dict[str, str] = {}      # token + consent id, set once the app has them
        self.refresh_token = ""

    # ---------- printing ----------
    def say(self, text: str = ""):
        if not self.quiet:
            print(text)

    def step(self, text: str):
        self.step_number += 1
        self.say(f"\n{self.step_number}. {text}")

    # ---------- one HTTP call, with a clear complaint if it goes wrong ----------
    def call(self, method: str, path: str, expect: int | tuple = 200, **kwargs) -> httpx.Response:
        response = self.http.request(method, path, **kwargs)
        wanted = (expect,) if isinstance(expect, int) else expect
        if response.status_code not in wanted:
            raise RuntimeError(f"{method} {path} gave {response.status_code}, expected {wanted}: {response.text[:300]}")
        return response

    def app_headers(self, extra: dict | None = None) -> dict:
        """Headers an app sends on every data call."""
        return {**self.headers, "X-Request-ID": str(uuid.uuid4()), **(extra or {})}

    # ---------- the pieces of the journey ----------
    def get_permission(self, customer: dict) -> str:
        """Ask for consent, let the customer approve, swap the code for tokens. Returns the consent id."""
        ibans = [{"iban": a["iban"]} for a in customer["accounts"]]
        self.step(f"The app asks the bank for permission to see {customer['name']}'s accounts.")
        created = self.call("POST", "/v1/consents", expect=201, headers={
            "X-Request-ID": str(uuid.uuid4()), "PSU-IP-Address": "192.0.2.1", "TPP-Redirect-URI": APP_ADDRESS,
        }, json={
            "access": {"accounts": ibans, "balances": ibans, "transactions": ibans},
            "recurringIndicator": True, "validUntil": (date.today() + timedelta(days=30)).isoformat(),
            "frequencyPerDay": 20, "combinedServiceIndicator": False,
        }).json()
        consent_id = created["consentId"]
        self.say(f"   The bank answers: consent {consent_id[:8]}..., status '{created['consentStatus']}' (waiting).")

        self.step("The customer opens the bank's page, signs in, and approves.   (the customer does this)")
        page = urlparse(created["_links"]["scaRedirect"]["href"]).path.replace("/bank/approve/", "")
        base = f"/bank/approve/{page}"
        self.call("POST", base + "/login", data={"username": customer["username"], "password": customer["password"]})
        back = self.call("POST", base + "/decide", expect=303,
                         data={"decision": "approve", "account": [a["resource_id"] for a in customer["accounts"]]})
        code = parse_qs(urlparse(back.headers["location"]).query)["code"][0]
        self.say("   The bank sends the customer back to the app, with a one-time code.")

        self.step("The app swaps the code for an access token.")
        token = self.call("POST", "/oauth/token", data={"grant_type": "authorization_code", "code": code}).json()
        self.headers = {"Authorization": f"Bearer {token['access_token']}", "Consent-ID": consent_id}
        self.refresh_token = token["refresh_token"]
        self.say(f"   Got a token that works for {token['expires_in'] // 60} minutes, and a refresh token.")
        return consent_id

    def balance(self, account_id: str) -> Decimal:
        body = self.call("GET", f"/v1/accounts/{account_id}/balances", headers=self.app_headers()).json()
        return Decimal(next(b for b in body["balances"] if b["balanceType"] == "interimBooked")["balanceAmount"]["amount"])

    def pay(self, customer: dict, debtor_iban: str, creditor_iban: str, amount: str, reference: str) -> str:
        """Start a payment, let the customer approve it. Returns the payment id."""
        started = self.call("POST", "/v1/payments/domestic", expect=201, headers=self.app_headers({
            "PSU-IP-Address": "192.0.2.1", "TPP-Redirect-URI": APP_ADDRESS,
        }), json={
            "debtorAccount": {"iban": debtor_iban}, "instructedAmount": {"currency": "GEL", "amount": amount},
            "creditorAccount": {"iban": creditor_iban}, "creditorName": "Example Shop",
            "endToEndIdentification": reference, "remittanceInformationUnstructured": reference,
        }).json()
        path = "/bank/pay/" + started["_links"]["scaRedirect"]["href"].rsplit("/", 1)[1]
        self.call("POST", path + "/login", data={"username": customer["username"], "password": customer["password"]})
        self.call("POST", path + "/decide", expect=303, data={"decision": "approve"})
        return started["paymentId"]

    def payment_status(self, payment_id: str, expect=200) -> httpx.Response:
        return self.call("GET", f"/v1/payments/domestic/{payment_id}/status", expect=expect, headers=self.app_headers())


def customer_with_accounts(client: httpx.Client, customer_id: str = "c1") -> dict:
    """The customer's fake sign-in and accounts, from the admin page."""
    data = client.get("/admin/data").json()
    person = next(c for c in data["customers"] if c["id"] == customer_id)
    person["accounts"] = [a for a in data["accounts"] if a["customer_id"] == customer_id]
    return person


def run(client: httpx.Client, quiet: bool = False) -> dict:
    """Play both parts. Returns a few facts, so a test can check them."""
    story = Story(client, quiet)

    # ---------- Part 1 ----------
    story.say("=" * 64 + "\nPART 1: a careful app\n" + "=" * 64)
    story.call("POST", "/admin/reset")
    story.call("PUT", "/admin/settings", json={"max_consent_requests_per_day": 20})   # room for this demo's reads
    nino = customer_with_accounts(client)
    current = nino["accounts"][0]
    savings = nino["accounts"][1]
    consent_id = story.get_permission(nino)

    story.step("The app reads the accounts and the balance.")
    listed = story.call("GET", "/v1/accounts", headers=story.app_headers()).json()["accounts"]
    story.say(f"   Accounts shared: {', '.join(a['name'] + ' (' + a['currency'] + ')' for a in listed)}")
    before = story.balance(current["resource_id"])
    story.say(f"   Balance of '{current['name']}': {before} GEL")

    story.step("The app starts a payment of 25.50 GEL. The customer approves it.   (the customer does this)")
    payment_id = story.pay(nino, current["iban"], _outside_iban(), "25.50", "Order 1")
    status = story.payment_status(payment_id).json()["transactionStatus"]
    after = story.balance(current["resource_id"])
    story.say(f"   Payment status: {status}.  Balance is now {after} GEL (was {before}).")
    newest = story.call("GET", f"/v1/accounts/{current['resource_id']}/transactions?bookingStatus=booked&dateFrom=2000-01-01",
                        headers=story.app_headers()).json()["transactions"]["booked"][0]
    story.say(f"   Newest transaction: {newest['transactionAmount']['amount']} GEL to {newest['creditorName']}.")
    assert status == "ACSC" and before - after == Decimal("25.50")

    story.step("An hour passes. The access token runs out, and the app uses its refresh token.")
    story.call("POST", "/admin/time/advance", json={"minutes": 61})
    expired = story.call("GET", "/v1/accounts", expect=401, headers=story.app_headers()).json()
    story.say(f"   The bank says: {expired['tppMessages'][0]['code']}.")
    fresh = story.call("POST", "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": story.refresh_token}).json()
    story.headers["Authorization"] = f"Bearer {fresh['access_token']}"
    story.call("GET", "/v1/accounts", headers=story.app_headers())
    story.say("   New token, and the app carries on. The customer was not asked again.")

    # ---------- Part 2 ----------
    story.say("\n" + "=" * 64 + "\nPART 2: a careless app\n" + "=" * 64)
    story.step("The app pays 40.00 GEL to the savings account. The customer approves.   (the customer does this)")
    first = story.pay(nino, current["iban"], savings["iban"], "40.00", "Rent")
    story.say("   The money moves.")

    story.step("The app checks the status, but the bank has a hiccup and answers with an error.")
    story.call("POST", "/admin/faults", expect=201, json={"kind": "server_error", "operation": "payments.status",
                                                          "status_code": 503, "times": 1})
    failed = story.payment_status(first, expect=503)
    story.say(f"   Status check failed with {failed.status_code}. The app does not know the payment went through.")

    story.step("The careless app decides the payment failed, and pays again. The customer approves again.")
    story.pay(nino, current["iban"], savings["iban"], "40.00", "Rent")

    story.step("The bank's duplicate report:")
    report = story.call("GET", "/admin/payments").json()
    for item in report["duplicates"]:
        story.say(f"   PAID TWICE: {item['amount']} GEL, {item['times_paid']} times (reference {item['end_to_end_id']}).")
    story.say(f"   Completed payments in total: {report['completed_count']}.")
    story.say("\nDone. A careful app would have checked the payment again before paying a second time.")

    return {"consent_id": consent_id, "balance_before": before, "balance_after": after,
            "duplicates": report["duplicates"], "completed_count": report["completed_count"]}


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    with httpx.Client(base_url=base_url, follow_redirects=False, timeout=10) as client:
        try:
            client.get("/health")
        except httpx.ConnectError:
            sys.exit(f"Cannot reach the mock bank at {base_url}. Start it first:\n"
                     "    uv run uvicorn app.main:app --reload")
        run(client)


if __name__ == "__main__":
    main()
