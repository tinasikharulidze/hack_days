"""Consent calls: how an app asks a customer for permission (step 3).

  POST   /v1/consents                       create a consent request
  GET    /v1/consents/{id}                  read it
  GET    /v1/consents/{id}/status           check its status
  DELETE /v1/consents/{id}                  the app withdraws it
  POST   /v1/consents/{id}/authorisations   start the customer's approval again

How it fits together (the "redirect" approach of the standard):
  1. The app creates a consent. The bank answers with a consent id and a link
     (scaRedirect) to its approval page. The consent status is "received".
  2. The app sends the customer to that link. The customer signs in and
     approves (see routers/bank_pages.py). The status becomes "valid".
  3. The bank sends the customer back to the app with a one-time code. The
     app swaps the code for an access token (see routers/oauth.py).

The consent calls themselves need no token: the token only exists after the
customer approved, and the app must be able to check a consent's status
before that.
"""

import uuid
from datetime import date, timedelta
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import clock, consent_rules, faults, settings, store
from app.errors import BankError
from app.models import Authorisation, Consent

router = APIRouter(prefix="/v1", tags=["Consents"])


# ---------- What the app sends ----------
# Field names are written exactly as in the standard (camelCase), so no
# renaming is needed between the JSON and these classes.


class AccountReference(BaseModel):
    """Points to one account by its IBAN."""

    model_config = ConfigDict(extra="forbid")
    iban: str
    currency: str | None = None


class AccountAccess(BaseModel):
    """What the app wants to see. Any other field is refused as a format error."""

    model_config = ConfigDict(extra="forbid")
    accounts: list[AccountReference] | None = None  # the account list entries
    balances: list[AccountReference] | None = None  # accounts whose balances it wants
    transactions: list[AccountReference] | None = (
        None  # accounts whose transactions it wants
    )
    availableAccounts: Literal["allAccounts"] | None = (
        None  # "all the customer's accounts"
    )


class ConsentRequest(BaseModel):
    """The body of POST /v1/consents. All five fields are required by the standard."""

    model_config = ConfigDict(extra="forbid")
    access: AccountAccess
    recurringIndicator: bool  # True = many reads over time, False = a one-off read
    validUntil: date  # the last day the app wants the consent to work
    frequencyPerDay: int = Field(ge=1)  # how many reads per day the app wants
    combinedServiceIndicator: (
        bool  # True = the app will also start payments (we do not support it)
    )


# ---------- Small helpers ----------


def _url(request: Request, path: str) -> str:
    """A full link back to this mock, e.g. http://127.0.0.1:8000/v1/consents/abc."""
    return str(request.base_url).rstrip("/") + path


def _bad(code: str, text: str) -> BankError:
    return BankError(400, code, text)


def _check_redirect_uri(uri: str) -> None:
    """The customer is sent back here, so it must be an ordinary web address."""
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _bad("FORMAT_ERROR", "TPP-Redirect-URI must be an http or https address.")


def _check_request(body: ConsentRequest, tpp_redirect_uri: str) -> None:
    """The rules a new consent request must pass. Each failure is a 400 with a standard code."""
    rules = settings.get()
    _check_redirect_uri(tpp_redirect_uri)

    if body.combinedServiceIndicator and not rules.combined_service_indicator_supported:
        raise _bad(
            "PARAMETER_NOT_SUPPORTED",
            "combinedServiceIndicator is not supported by this bank.",
        )
    if body.validUntil < clock.now().date():
        raise _bad("PARAMETER_NOT_CONSISTENT", "validUntil is in the past.")
    # Standard rule: a one-off consent allows exactly one read.
    if not body.recurringIndicator and body.frequencyPerDay != 1:
        raise _bad(
            "PARAMETER_NOT_CONSISTENT",
            "A one-off consent (recurringIndicator false) needs frequencyPerDay 1.",
        )
    if body.frequencyPerDay > rules.max_consent_requests_per_day:
        raise _bad(
            "PARAMETER_NOT_CONSISTENT",
            f"frequencyPerDay can be at most {rules.max_consent_requests_per_day}.",
        )

    access = body.access
    if not (
        access.accounts
        or access.balances
        or access.transactions
        or access.availableAccounts
    ):
        raise _bad("PARAMETER_NOT_CONSISTENT", "The access part asks for nothing.")

    # Every IBAN must exist here, and all must belong to the same customer
    # (one consent is one customer's permission).
    by_iban = {a.iban: a for a in store.STATE.accounts.values()}
    owners = set()
    for section in (access.accounts, access.balances, access.transactions):
        for reference in section or []:
            account = by_iban.get(reference.iban)
            if account is None or (
                reference.currency and reference.currency != account.currency
            ):
                raise _bad("RESOURCE_UNKNOWN", f"Unknown account: {reference.iban}.")
            owners.add(account.customer_id)
    if len(owners) > 1:
        raise _bad(
            "PARAMETER_NOT_CONSISTENT", "The accounts belong to different customers."
        )


# ---------- The calls ----------


@router.post(
    "/consents",
    status_code=201,
    dependencies=[Depends(faults.fault_before("consents.create"))],
)
async def create_consent(
    request: Request,
    body: ConsentRequest,
    x_request_id: uuid.UUID = Header(alias="X-Request-ID"),
    psu_ip_address: str = Header(
        alias="PSU-IP-Address"
    ),  # required by the standard, not used by the mock
    tpp_redirect_uri: str = Header(alias="TPP-Redirect-URI"),
):
    """Ask for a customer's permission. The consent starts as "received" (waiting for the customer)."""
    _check_request(body, tpp_redirect_uri)
    rules = settings.get()
    now = clock.now()

    consent = Consent(
        consent_id=str(uuid.uuid4()),
        access=body.access.model_dump(exclude_none=True),
        recurring_indicator=body.recurringIndicator,
        # A date too far ahead is cut back to the longest the bank allows (default 90 days).
        valid_until=min(
            body.validUntil, now.date() + timedelta(days=rules.consent_validity_days)
        ),
        frequency_per_day=body.frequencyPerDay,
        combined_service_indicator=body.combinedServiceIndicator,
        tpp_redirect_uri=tpp_redirect_uri,
        created_at=now,
        last_action=now,
    )
    # The customer's approval step starts together with the consent ("implicit start").
    authorisation = Authorisation(
        authorisation_id=str(uuid.uuid4()),
        kind="consent",
        resource_id=consent.consent_id,
        created_at=now,
    )
    store.STATE.consents[consent.consent_id] = consent
    store.STATE.authorisations[authorisation.authorisation_id] = authorisation

    path = f"/v1/consents/{consent.consent_id}"
    content = {
        "consentStatus": consent.status,
        "consentId": consent.consent_id,
        "_links": {
            "scaRedirect": {
                "href": _url(request, f"/bank/approve/{authorisation.authorisation_id}")
            },
            "self": {"href": _url(request, path)},
            "status": {"href": _url(request, path + "/status")},
            "startAuthorisation": {"href": _url(request, path + "/authorisations")},
        },
    }
    await faults.run("consents.create", "after")
    return JSONResponse(
        status_code=201,
        content=content,
        headers={
            "Location": _url(request, path),
            "X-Request-ID": str(x_request_id),
            "ASPSP-SCA-Approach": "REDIRECT",
        },
    )


@router.get(
    "/consents/{consent_id}",
    dependencies=[Depends(faults.fault_before("consents.get"))],
)
async def get_consent(
    consent_id: str, x_request_id: uuid.UUID = Header(alias="X-Request-ID")
):
    """Read a consent: what was asked, until when, and its current status."""
    consent = consent_rules.get_or_403(consent_id)
    content = {
        "access": consent.access,
        "recurringIndicator": consent.recurring_indicator,
        "validUntil": consent.valid_until.isoformat(),
        "frequencyPerDay": consent.frequency_per_day,
        "lastActionDate": consent.last_action.date().isoformat(),
        "consentStatus": consent.status,
    }
    await faults.run("consents.get", "after")
    return JSONResponse(content=content, headers={"X-Request-ID": str(x_request_id)})


@router.get(
    "/consents/{consent_id}/status",
    dependencies=[Depends(faults.fault_before("consents.status"))],
)
async def get_consent_status(
    consent_id: str, x_request_id: uuid.UUID = Header(alias="X-Request-ID")
):
    """Just the status. Apps poll this while the customer is deciding."""
    consent = consent_rules.get_or_403(consent_id)
    await faults.run("consents.status", "after")
    return JSONResponse(
        content={"consentStatus": consent.status},
        headers={"X-Request-ID": str(x_request_id)},
    )


@router.delete(
    "/consents/{consent_id}",
    status_code=204,
    dependencies=[Depends(faults.fault_before("consents.delete"))],
)
async def delete_consent(
    consent_id: str, x_request_id: uuid.UUID = Header(alias="X-Request-ID")
):
    """The app withdraws the consent. It becomes "terminatedByTpp" and stops working."""
    consent = consent_rules.get_or_403(consent_id)
    if consent.status in ("received", "valid"):
        consent_rules.set_status(consent, "terminatedByTpp")
    await faults.run("consents.delete", "after")
    return Response(status_code=204, headers={"X-Request-ID": str(x_request_id)})


@router.post(
    "/consents/{consent_id}/authorisations",
    status_code=201,
    dependencies=[Depends(faults.fault_before("consents.authorise"))],
)
async def start_authorisation(
    request: Request,
    consent_id: str,
    x_request_id: uuid.UUID = Header(alias="X-Request-ID"),
):
    """Start the customer's approval step again (for example if the first link was lost)."""
    consent = consent_rules.get_or_403(consent_id)
    if consent.status != "received":
        raise BankError(
            409,
            "STATUS_INVALID",
            f"The consent is {consent.status}; it can no longer be approved.",
        )
    authorisation = Authorisation(
        authorisation_id=str(uuid.uuid4()),
        kind="consent",
        resource_id=consent.consent_id,
        created_at=clock.now(),
    )
    store.STATE.authorisations[authorisation.authorisation_id] = authorisation
    content = {
        "scaStatus": authorisation.status,
        "authorisationId": authorisation.authorisation_id,
        "_links": {
            "scaRedirect": {
                "href": _url(request, f"/bank/approve/{authorisation.authorisation_id}")
            }
        },
    }
    await faults.run("consents.authorise", "after")
    return JSONResponse(
        status_code=201, content=content, headers={"X-Request-ID": str(x_request_id)}
    )
