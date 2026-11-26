"""The rules about a payment's life: approval, money moving, cancelling.

Only the "ask the customer every time" trust level is built: every payment
waits for the customer to approve it on the bank's page.

The five statuses we use (the standard has more; these are the useful ones):

    RCVD  received, waiting for the customer        (where every payment starts)
    ACSC  completed: the money has moved            (customer approved, enough money)
    PDNG  stuck in pending, money has NOT moved     (only with the payment_stays_pending fault)
    RJCT  rejected: customer said no, nobody answered in time, or not enough money
    CANC  cancelled by the app before the customer approved

    RCVD --(customer approves)--> ACSC
         --(customer approves, fault on)--> PDNG   (stays there)
         --(customer rejects / time runs out / not enough money)--> RJCT
         --(app cancels)--> CANC

ACSC, PDNG, RJCT and CANC are final: nothing moves a payment out of them.
All times come from the mock clock (app/clock.py).
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from app import clock, faults, settings, store
from app.errors import BankError
from app.models import Account, Consent, Payment, Transaction

CURRENCY = "GEL"  # a domestic payment is in lari, from a lari account


def account_by_iban(iban: str) -> Account | None:
    """The bank's own account with this IBAN, or None if it is somebody else's."""
    return next((a for a in store.STATE.accounts.values() if a.iban == iban), None)


def refresh(payment: Payment) -> Payment:
    """Bring a payment's status up to date with the clock, and return it.

    A payment nobody approved in time becomes RJCT (the standard demands it).
    There is no background timer, so every call that touches a payment runs this first.
    """
    if payment.status == "RCVD" and clock.now() > payment.approval_deadline:
        payment.status = "RJCT"
    return payment


def find_or_404(payment_id: str, consent: Consent, product: str) -> Payment:
    """Find a payment made under this consent. Anything else looks like "unknown"."""
    payment = store.STATE.payments.get(payment_id)
    if (
        payment is None
        or payment.consent_id != consent.consent_id
        or payment.product != product
    ):
        raise BankError(404, "RESOURCE_UNKNOWN", "This payment does not exist.")
    return refresh(payment)


def new_payment(
    consent: Consent,
    product: str,
    debtor: Account,
    creditor_iban: str,
    creditor_name: str,
    amount: Decimal,
    remittance: str | None,
    end_to_end_id: str | None,
    request_id: str,
    tpp_redirect_uri: str,
) -> Payment:
    """Create a payment in status RCVD. The caller has already checked the request."""
    now = clock.now()
    payment = Payment(
        payment_id=str(uuid.uuid4()),
        consent_id=consent.consent_id,
        customer_id=consent.customer_id,
        status="RCVD",
        product=product,
        debtor_iban=debtor.iban,
        creditor_iban=creditor_iban,
        creditor_name=creditor_name,
        amount=amount,
        currency=CURRENCY,
        remittance=remittance,
        end_to_end_id=end_to_end_id,
        created_at=now,
        request_id=request_id,
        tpp_redirect_uri=tpp_redirect_uri,
        approval_deadline=now
        + timedelta(minutes=settings.get().payment_approval_timeout_minutes),
    )
    store.STATE.payments[payment.payment_id] = payment
    return payment


def has_enough_money(debtor: Account, amount: Decimal) -> bool:
    """Enough in interimAvailable (booked money minus pending money out)."""
    return store.balances(debtor.resource_id)["interimAvailable"] >= amount


def _move_money(payment: Payment, debtor: Account) -> None:
    """Record the payment as booked transactions. Balances follow, because the
    mock works balances out from the transactions.

    The debtor's account gets a transaction for money out. If the creditor is
    also one of our accounts, it gets one for money in.
    """
    today = clock.now().date()

    def booked(
        account: Account, amount: Decimal, other_name: str, other_iban: str
    ) -> Transaction:
        return Transaction(
            transaction_id=str(uuid.uuid4()),
            account_id=account.resource_id,
            status="booked",
            amount=amount,
            currency=CURRENCY,
            booking_date=today,
            value_date=today,
            counterparty_name=other_name,
            counterparty_iban=other_iban,
            remittance=payment.remittance or "Payment",
        )

    store.STATE.transactions.append(
        booked(debtor, -payment.amount, payment.creditor_name, payment.creditor_iban)
    )
    creditor = account_by_iban(payment.creditor_iban)
    if creditor is not None:
        store.STATE.transactions.append(
            booked(creditor, payment.amount, debtor.owner_name, debtor.iban)
        )


def approve(payment: Payment) -> str:
    """The customer pressed Approve. Returns what happened: "completed", "pending" or "failed".

    The "payment_stays_pending" fault is checked first: the customer approved,
    but the payment never completes and no money moves.
    """
    if faults.take("payment_stays_pending", "payments.authorise"):
        payment.status = "PDNG"
        return "pending"
    debtor = account_by_iban(payment.debtor_iban)
    # Money may have gone elsewhere since the payment was started, so check again.
    if not has_enough_money(debtor, payment.amount):
        payment.status = "RJCT"
        return "failed"
    _move_money(payment, debtor)
    payment.status = "ACSC"
    payment.completed_at = clock.now()
    return "completed"


def reject(payment: Payment) -> None:
    """The customer pressed Reject."""
    payment.status = "RJCT"


def cancel(payment: Payment) -> None:
    """The app withdraws a payment. Only possible while it waits for the customer."""
    if payment.status != "RCVD":
        raise BankError(
            405,
            "CANCELLATION_INVALID",
            f"The payment is {payment.status}; only a payment waiting for approval can be cancelled.",
        )
    payment.status = "CANC"
