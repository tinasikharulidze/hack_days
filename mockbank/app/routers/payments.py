"""Payment calls (step 5): how an app sends money out of a customer's account.

  POST   /v1/payments/{product}                       start a payment
  GET    /v1/payments/{product}/{id}                  read it
  GET    /v1/payments/{product}/{id}/status           check its status
  DELETE /v1/payments/{product}/{id}                  cancel it (only while it waits for the customer)
  POST   /v1/payments/{product}/{id}/authorisations   get a fresh approval link

{product} is "domestic" (a single payment in lari). "foreign" is a real
product of the standard but this bank does not offer it.

How a payment goes (the customer approves every payment, "level 1" trust):
  1. The app starts a payment. The bank answers 201, status RCVD, plus a link
     (scaRedirect) to its approval page. No money has moved.
  2. The customer opens the link, signs in, and presses Approve or Reject
     (see routers/payment_pages.py). Approve moves the money.
  3. The app asks for the status until it is final.

Same checks as the account calls, with one difference: a payment is not a
"read", so it does not count towards the daily reads (it uses require_access,
not require_read).
"""

import uuid
from decimal import Decimal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import clock, faults, payment_rules, settings, store
from app.auth import require_access
from app.errors import BankError
from app.iban import is_valid_iban
from app.models import Authorisation, Consent, Payment

router = APIRouter(prefix="/v1", tags=["Payments"])

KNOWN_PRODUCTS = {"domestic", "foreign"}   # the standard's list; we offer only some (a setting)


# ---------- What the app sends ----------

class AccountReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    iban: str
    currency: str | None = None

    @field_validator("iban")
    @classmethod
    def iban_must_be_valid(cls, value: str) -> str:
        if not is_valid_iban(value):
            raise ValueError("not a valid IBAN")
        return value


class InstructedAmount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str
    amount: str   # text like "25.50", as the standard says

    @field_validator("amount")
    @classmethod
    def amount_must_be_money(cls, value: str) -> str:
        # Digits, optionally a dot and one or two decimals, and more than zero.
        if not (value.replace(".", "", 1).isdigit() and len(value.partition(".")[2]) <= 2
                and Decimal(value) > 0):
            raise ValueError("must be a positive amount with at most 2 decimals, like 25.50")
        return value


class PaymentRequest(BaseModel):
    """The body of POST /v1/payments/domestic."""
    model_config = ConfigDict(extra="forbid")
    debtorAccount: AccountReference       # the account the money leaves
    instructedAmount: InstructedAmount
    creditorAccount: AccountReference     # the account the money goes to
    creditorName: str = Field(min_length=1, max_length=70)
    endToEndIdentification: str | None = Field(default=None, max_length=35)
    remittanceInformationUnstructured: str | None = Field(default=None, max_length=140)


# ---------- Small helpers ----------

def _url(request: Request, path: str) -> str:
    return str(request.base_url).rstrip("/") + path


def _money(amount: Decimal) -> str:
    return format(amount, "f")


def _check_product(product: str) -> None:
    """Unknown product -> 404. Known but not offered here -> 403."""
    if product not in KNOWN_PRODUCTS:
        raise BankError(404, "PRODUCT_UNKNOWN", f"Unknown payment product '{product}'.")
    if product not in settings.get().supported_payment_products:
        raise BankError(403, "PRODUCT_INVALID", f"This bank does not offer the '{product}' product.")


def _check_redirect_uri(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BankError(400, "FORMAT_ERROR", "TPP-Redirect-URI must be an http or https address.")


def _start_authorisation(payment: Payment) -> Authorisation:
    """Make the approval step for a payment (the bank's page is built from it)."""
    authorisation = Authorisation(authorisation_id=str(uuid.uuid4()), kind="payment",
                                  resource_id=payment.payment_id, created_at=clock.now())
    store.STATE.authorisations[authorisation.authorisation_id] = authorisation
    return authorisation


def _payment_json(payment: Payment) -> dict:
    """A payment as the standard describes it, plus its status."""
    body = {
        "debtorAccount": {"iban": payment.debtor_iban},
        "instructedAmount": {"currency": payment.currency, "amount": _money(payment.amount)},
        "creditorAccount": {"iban": payment.creditor_iban},
        "creditorName": payment.creditor_name,
        "transactionStatus": payment.status,
    }
    if payment.end_to_end_id:
        body["endToEndIdentification"] = payment.end_to_end_id
    if payment.remittance:
        body["remittanceInformationUnstructured"] = payment.remittance
    return body


def _check_request(consent: Consent, body: PaymentRequest, tpp_redirect_uri: str):
    """The rules a new payment must pass. Returns the account the money leaves.

    Same rule as for account calls: an account that was not shared in the
    consent looks exactly like an account that does not exist.
    """
    _check_redirect_uri(tpp_redirect_uri)

    debtor = payment_rules.account_by_iban(body.debtorAccount.iban)
    if (debtor is None or debtor.resource_id not in consent.granted_account_ids
            or (body.debtorAccount.currency and body.debtorAccount.currency != debtor.currency)):
        raise BankError(400, "RESOURCE_UNKNOWN", "The paying account does not exist or was not shared.")

    # A domestic payment is lari from a lari account.
    if body.instructedAmount.currency != payment_rules.CURRENCY or debtor.currency != payment_rules.CURRENCY:
        raise BankError(400, "PARAMETER_NOT_CONSISTENT",
                        f"A domestic payment must be in {payment_rules.CURRENCY}, from a {payment_rules.CURRENCY} account.")
    if body.creditorAccount.iban == debtor.iban:
        raise BankError(400, "PARAMETER_NOT_CONSISTENT", "The paying and receiving account are the same.")
    receiver = payment_rules.account_by_iban(body.creditorAccount.iban)
    if receiver is not None and receiver.currency != payment_rules.CURRENCY:
        raise BankError(400, "PARAMETER_NOT_CONSISTENT", "The receiving account is not a lari account.")

    if not payment_rules.has_enough_money(debtor, Decimal(body.instructedAmount.amount)):
        raise BankError(400, "PAYMENT_FAILED", "Not enough money on the account.")
    return debtor


def _created_answer(request: Request, payment: Payment, authorisation: Authorisation | None,
                    x_request_id: uuid.UUID) -> JSONResponse:
    """The 201 answer to "start a payment"."""
    path = f"/v1/payments/{payment.product}/{payment.payment_id}"
    links = {"self": {"href": _url(request, path)}, "status": {"href": _url(request, path + "/status")}}
    if authorisation is not None:
        links = {"scaRedirect": {"href": _url(request, f"/bank/pay/{authorisation.authorisation_id}")}, **links}
    return JSONResponse(status_code=201, content={
        "transactionStatus": payment.status, "paymentId": payment.payment_id, "_links": links,
    }, headers={"Location": _url(request, path), "X-Request-ID": str(x_request_id),
                "ASPSP-SCA-Approach": "REDIRECT"})


# ---------- The calls ----------

@router.post("/payments/{product}", status_code=201,
             dependencies=[Depends(faults.fault_before("payments.initiate"))])
async def start_payment(
    request: Request,
    product: str,
    body: PaymentRequest,
    consent: Consent = Depends(require_access),
    x_request_id: uuid.UUID = Header(alias="X-Request-ID"),
    psu_ip_address: str = Header(alias="PSU-IP-Address"),    # required by the standard, not used by the mock
    tpp_redirect_uri: str = Header(alias="TPP-Redirect-URI"),
):
    """Start a payment. It waits (status RCVD) until the customer approves it on the bank's page."""
    _check_product(product)

    # Setting deduplicate_by_request_id: the same X-Request-ID gives back the first payment.
    if settings.get().deduplicate_by_request_id:
        earlier = next((p for p in store.STATE.payments.values()
                        if p.consent_id == consent.consent_id and p.request_id == str(x_request_id)), None)
        if earlier is not None:
            link = next((a for a in store.STATE.authorisations.values() if a.resource_id == earlier.payment_id), None)
            return _created_answer(request, payment_rules.refresh(earlier), link, x_request_id)

    debtor = _check_request(consent, body, tpp_redirect_uri)
    payment = payment_rules.new_payment(
        consent, product, debtor, body.creditorAccount.iban, body.creditorName,
        Decimal(body.instructedAmount.amount), body.remittanceInformationUnstructured,
        body.endToEndIdentification, str(x_request_id), tpp_redirect_uri)
    authorisation = _start_authorisation(payment)

    # An "after" fault lands here: the payment exists, but the app is told it failed.
    await faults.run("payments.initiate", "after")
    return _created_answer(request, payment, authorisation, x_request_id)


@router.get("/payments/{product}/{payment_id}",
            dependencies=[Depends(faults.fault_before("payments.get"))])
async def get_payment(product: str, payment_id: str, consent: Consent = Depends(require_access),
                      x_request_id: uuid.UUID = Header(alias="X-Request-ID")):
    """Read a payment: what was asked and its current status."""
    _check_product(product)
    payment = payment_rules.find_or_404(payment_id, consent, product)
    await faults.run("payments.get", "after")
    return JSONResponse(content=_payment_json(payment), headers={"X-Request-ID": str(x_request_id)})


@router.get("/payments/{product}/{payment_id}/status",
            dependencies=[Depends(faults.fault_before("payments.status"))])
async def get_payment_status(product: str, payment_id: str, consent: Consent = Depends(require_access),
                             x_request_id: uuid.UUID = Header(alias="X-Request-ID")):
    """Just the status. Apps ask this repeatedly while the customer decides."""
    _check_product(product)
    payment = payment_rules.find_or_404(payment_id, consent, product)
    await faults.run("payments.status", "after")
    return JSONResponse(content={"transactionStatus": payment.status}, headers={"X-Request-ID": str(x_request_id)})


@router.delete("/payments/{product}/{payment_id}", status_code=202,
               dependencies=[Depends(faults.fault_before("payments.cancel"))])
async def cancel_payment(product: str, payment_id: str, consent: Consent = Depends(require_access),
                         x_request_id: uuid.UUID = Header(alias="X-Request-ID")):
    """Cancel a payment that still waits for the customer. Any other status: 405 CANCELLATION_INVALID."""
    _check_product(product)
    payment = payment_rules.find_or_404(payment_id, consent, product)
    payment_rules.cancel(payment)
    await faults.run("payments.cancel", "after")
    return JSONResponse(status_code=202, content={"transactionStatus": payment.status},
                        headers={"X-Request-ID": str(x_request_id)})


@router.post("/payments/{product}/{payment_id}/authorisations", status_code=201,
             dependencies=[Depends(faults.fault_before("payments.authorise"))])
async def start_payment_authorisation(request: Request, product: str, payment_id: str,
                                      consent: Consent = Depends(require_access),
                                      x_request_id: uuid.UUID = Header(alias="X-Request-ID")):
    """Get a fresh approval link (for example if the first one was lost). Only while the payment waits."""
    _check_product(product)
    payment = payment_rules.find_or_404(payment_id, consent, product)
    if payment.status != "RCVD":
        raise BankError(409, "STATUS_INVALID", f"The payment is {payment.status}; it can no longer be approved.")
    authorisation = _start_authorisation(payment)
    content = {"scaStatus": authorisation.status, "authorisationId": authorisation.authorisation_id,
               "_links": {"scaRedirect": {"href": _url(request, f"/bank/pay/{authorisation.authorisation_id}")}}}
    await faults.run("payments.authorise", "after")
    return JSONResponse(status_code=201, content=content, headers={"X-Request-ID": str(x_request_id)})
