"""Account information calls (step 4): what an app does with its token.

  GET /v1/accounts                                    the accounts the customer shared
  GET /v1/accounts/{id}                               one account
  GET /v1/accounts/{id}/balances                      its balances
  GET /v1/accounts/{id}/transactions                  its transactions (filtered by date and status)
  GET /v1/accounts/{id}/transactions/{transactionId}  one transaction

Every call here goes through the same checks, in this order:
  1. "before" fault (if one is on)
  2. token and consent are valid and belong together        (app/auth.py: require_access)
  3. this call is within the consent's reads per day         (app/auth.py: require_read, else 429)
  4. the account exists AND the customer shared it           (else 404 RESOURCE_UNKNOWN)
  5. the consent allows this kind of access to the account   (else 401 CONSENT_INVALID)
  6. the query is valid
then the answer is built, and the "after" fault (if one is on) runs last.

Step 4 sees an account that was not shared exactly as if it did not exist, so
an app cannot learn which other accounts the bank has.

Money amounts are text with two decimals ("12.30"), as the standard says.
Only the JSON format is built (the standard's camt.052 XML format is not).
"""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from app import consent_rules, faults, settings, store
from app.auth import require_read
from app.errors import BankError
from app.models import Account, Consent, Transaction

router = APIRouter(prefix="/v1", tags=["Accounts"])

# Every value the standard knows for bookingStatus. Which ones THIS bank supports
# is the setting supported_booking_statuses.
KNOWN_BOOKING_STATUSES = {"booked", "pending", "both", "information", "all"}
# Query parameters of the standard that this mock does not build.
UNSUPPORTED_QUERY = ("deltaList", "entryReferenceFrom")


# ---------- Small helpers ----------

def _url(request: Request, path: str) -> str:
    return str(request.base_url).rstrip("/") + path


def _money(amount: Decimal) -> str:
    """12.3 -> "12.30"."""
    return format(amount, "f")


def _reply(content: dict, x_request_id: uuid.UUID) -> JSONResponse:
    return JSONResponse(content=content, headers={"X-Request-ID": str(x_request_id)})


def _find_account(consent: Consent, account_id: str) -> Account:
    """The account, if it exists and the customer shared it. Otherwise 404, same as "unknown"."""
    account = store.STATE.accounts.get(account_id)
    if account is None or account.resource_id not in consent.granted_account_ids:
        raise BankError(404, "RESOURCE_UNKNOWN", "This account does not exist or was not shared.")
    return account


def _need(consent: Consent, account: Account, permission: str) -> None:
    """The consent must allow this kind of access to this account."""
    if not consent_rules.may(consent, account, permission):
        raise BankError(401, "CONSENT_INVALID",
                        f"The consent does not give access to {permission} of this account.")


def _balances_of(account: Account) -> list[dict]:
    return [{"balanceAmount": {"currency": account.currency, "amount": _money(amount)},
             "balanceType": kind}
            for kind, amount in store.balances(account.resource_id).items()]


def _account_json(request: Request, consent: Consent, account: Account, with_balances: bool = False) -> dict:
    """One account as the standard describes it."""
    base = f"/v1/accounts/{account.resource_id}"
    links = {}
    # Only offer links to what this consent may actually read.
    if consent_rules.may(consent, account, "balances"):
        links["balances"] = {"href": _url(request, base + "/balances")}
    if consent_rules.may(consent, account, "transactions"):
        links["transactions"] = {"href": _url(request, base + "/transactions")}
    body = {
        "resourceId": account.resource_id,
        "iban": account.iban,
        "currency": account.currency,
        "name": account.name,
        "product": account.product,
        "cashAccountType": account.cash_account_type,
        "status": account.status,
        "ownerName": account.owner_name,
        "_links": links,
    }
    if with_balances:
        body["balances"] = _balances_of(account)
    return body


def _check_with_balance(with_balance: bool | None, consent: Consent, accounts: list[Account]) -> bool:
    """The withBalance query parameter. Not supported unless the setting says so."""
    if not with_balance:
        return False
    if not settings.get().with_balance_supported:
        raise BankError(400, "PARAMETER_NOT_SUPPORTED", "withBalance is not supported by this bank.")
    # Balances are only added where the consent allows them.
    for account in accounts:
        _need(consent, account, "balances")
    return True


def _transaction_json(t: Transaction) -> dict:
    """One transaction as the standard describes it.

    Money out: the other side is the creditor. Money in: the other side is the debtor.
    Pending transactions have no booking date yet.
    """
    body = {
        "transactionId": t.transaction_id,
        "valueDate": t.value_date.isoformat(),
        "transactionAmount": {"currency": t.currency, "amount": _money(t.amount)},
        "remittanceInformationUnstructured": t.remittance[:140],  # the standard allows 140 characters
    }
    if t.booking_date is not None:
        body["bookingDate"] = t.booking_date.isoformat()
    side = "creditor" if t.amount < 0 else "debtor"
    body[f"{side}Name"] = t.counterparty_name
    body[f"{side}Account"] = {"iban": t.counterparty_iban}
    return body


# ---------- The calls ----------

@router.get("/accounts", dependencies=[Depends(faults.fault_before("accounts.list"))])
async def list_accounts(
    request: Request,
    consent: Consent = Depends(require_read),
    x_request_id: uuid.UUID = Header(alias="X-Request-ID"),
    with_balance: bool | None = Query(default=None, alias="withBalance"),
):
    """The accounts the customer shared and the app may see. An empty list is a normal 200."""
    visible = [a for a in store.STATE.accounts.values() if consent_rules.may(consent, a, "accounts")]
    with_balances = _check_with_balance(with_balance, consent, visible)
    content = {"accounts": [_account_json(request, consent, a, with_balances) for a in visible]}
    await faults.run("accounts.list", "after")
    return _reply(content, x_request_id)


@router.get("/accounts/{account_id}", dependencies=[Depends(faults.fault_before("accounts.details"))])
async def account_details(
    request: Request,
    account_id: str,
    consent: Consent = Depends(require_read),
    x_request_id: uuid.UUID = Header(alias="X-Request-ID"),
    with_balance: bool | None = Query(default=None, alias="withBalance"),
):
    """One account."""
    account = _find_account(consent, account_id)
    _need(consent, account, "accounts")
    with_balances = _check_with_balance(with_balance, consent, [account])
    content = {"account": _account_json(request, consent, account, with_balances)}
    await faults.run("accounts.details", "after")
    return _reply(content, x_request_id)


@router.get("/accounts/{account_id}/balances", dependencies=[Depends(faults.fault_before("accounts.balances"))])
async def account_balances(
    account_id: str,
    consent: Consent = Depends(require_read),
    x_request_id: uuid.UUID = Header(alias="X-Request-ID"),
):
    """interimBooked (final transactions only) and interimAvailable (also counting pending ones)."""
    account = _find_account(consent, account_id)
    _need(consent, account, "balances")
    content = {"account": {"iban": account.iban, "currency": account.currency},
               "balances": _balances_of(account)}
    await faults.run("accounts.balances", "after")
    return _reply(content, x_request_id)


@router.get("/accounts/{account_id}/transactions",
            dependencies=[Depends(faults.fault_before("accounts.transactions"))])
async def account_transactions(
    request: Request,
    account_id: str,
    consent: Consent = Depends(require_read),
    x_request_id: uuid.UUID = Header(alias="X-Request-ID"),
    booking_status: str = Query(alias="bookingStatus"),          # required by the standard
    date_from: date = Query(alias="dateFrom"),                   # required (we have no "delta" mode)
    date_to: date | None = Query(default=None, alias="dateTo"),  # optional, inclusive
    with_balance: bool | None = Query(default=None, alias="withBalance"),
):
    """Transactions of one account.

    bookingStatus: "booked" (final), "pending" (not final yet) or "both".
    A booked transaction is matched on its booking date, a pending one on its
    value date (it has no booking date yet). dateTo is included.
    """
    account = _find_account(consent, account_id)
    _need(consent, account, "transactions")

    # --- Check the query ---
    if booking_status not in KNOWN_BOOKING_STATUSES:
        raise BankError(400, "FORMAT_ERROR", f"bookingStatus must be one of {sorted(KNOWN_BOOKING_STATUSES)}.")
    if booking_status not in settings.get().supported_booking_statuses:
        raise BankError(400, "PARAMETER_NOT_SUPPORTED", f"bookingStatus '{booking_status}' is not supported by this bank.")
    for name in UNSUPPORTED_QUERY:
        if name in request.query_params:
            raise BankError(400, "PARAMETER_NOT_SUPPORTED", f"{name} is not supported by this bank.")
    if date_to is not None and date_from > date_to:
        raise BankError(400, "PERIOD_INVALID", "dateFrom is after dateTo.")
    with_balances = _check_with_balance(with_balance, consent, [account])

    # --- Pick the transactions ---
    def in_period(day: date) -> bool:
        return day >= date_from and (date_to is None or day <= date_to)

    mine = [t for t in store.STATE.transactions if t.account_id == account.resource_id]
    booked = [t for t in mine if t.status == "booked" and in_period(t.booking_date)]
    pending = [t for t in mine if t.status == "pending" and in_period(t.value_date)]
    booked.sort(key=lambda t: (t.booking_date, t.transaction_id), reverse=True)   # newest first
    pending.sort(key=lambda t: (t.value_date, t.transaction_id), reverse=True)

    report: dict = {}
    if booking_status in ("booked", "both"):
        report["booked"] = [_transaction_json(t) for t in booked]
    if booking_status in ("pending", "both"):
        report["pending"] = [_transaction_json(t) for t in pending]
    report["_links"] = {"account": {"href": _url(request, f"/v1/accounts/{account.resource_id}")}}

    content = {"account": {"iban": account.iban, "currency": account.currency}, "transactions": report}
    if with_balances:
        content["balances"] = _balances_of(account)
    await faults.run("accounts.transactions", "after")
    return _reply(content, x_request_id)


@router.get("/accounts/{account_id}/transactions/{transaction_id}",
            dependencies=[Depends(faults.fault_before("accounts.transaction"))])
async def one_transaction(
    account_id: str,
    transaction_id: str,
    consent: Consent = Depends(require_read),
    x_request_id: uuid.UUID = Header(alias="X-Request-ID"),
):
    """One transaction of one account."""
    account = _find_account(consent, account_id)
    _need(consent, account, "transactions")
    found = next((t for t in store.STATE.transactions
                  if t.account_id == account.resource_id and t.transaction_id == transaction_id), None)
    if found is None:
        raise BankError(404, "RESOURCE_UNKNOWN", "This transaction does not exist on this account.")
    await faults.run("accounts.transaction", "after")
    return _reply({"transactionsDetails": {"transactionDetails": _transaction_json(found)}}, x_request_id)
