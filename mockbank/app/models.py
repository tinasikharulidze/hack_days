"""The things the mock bank keeps track of.

These are plain data containers with no logic. Later steps fill them in:
consents and tokens in step 3, accounts in step 4, payments in step 5.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass
class Customer:
    """A person who owns accounts at the bank (the standard calls them the PSU)."""
    id: str
    name: str
    # Fake sign-in used on the mock bank's approval page (step 3). Test values only.
    username: str = ""
    password: str = ""


@dataclass
class Account:
    resource_id: str          # the id used in URLs, e.g. /v1/accounts/{resource_id}
    customer_id: str
    iban: str
    currency: str             # ISO 4217, e.g. "GEL"
    name: str
    product: str
    owner_name: str
    opening_balance: Decimal  # balance before the first transaction we keep
    cash_account_type: str = "CACC"  # CACC = current account, SVGS = savings
    status: str = "enabled"


@dataclass
class Transaction:
    transaction_id: str
    account_id: str           # the account's resource_id
    status: str               # "booked" (final) or "pending" (not yet final)
    amount: Decimal           # negative = money out, positive = money in
    currency: str
    booking_date: date | None  # pending transactions have no booking date yet
    value_date: date
    counterparty_name: str
    counterparty_iban: str
    remittance: str           # the payment description


@dataclass
class Consent:
    """The customer's permission for an app to read their data (step 3)."""
    consent_id: str
    status: str = "received"  # received, valid, rejected, expired, revokedByPsu, ...
    access: dict = field(default_factory=dict)   # which accounts, balances, transactions
    recurring_indicator: bool = True              # False = one-off consent
    valid_until: date | None = None
    frequency_per_day: int = 4                    # data reads allowed per day
    combined_service_indicator: bool = False
    tpp_redirect_uri: str | None = None           # where to send the customer after approval
    customer_id: str | None = None                # known once the customer approves
    granted_account_ids: list[str] = field(default_factory=list)  # accounts the customer shared
    created_at: datetime | None = None
    last_action: datetime | None = None           # last time the status changed
    expires_at: datetime | None = None            # set when the customer approves
    reads_by_day: dict[str, int] = field(default_factory=dict)  # date text -> number of reads


@dataclass
class Token:
    """An access token: the short-lived key an app shows with every call (step 3)."""
    access_token: str
    consent_id: str
    customer_id: str
    expires_at: datetime


@dataclass
class RefreshToken:
    """A longer-lived key an app swaps for a new access token. It lasts as long as its consent."""
    refresh_token: str
    consent_id: str
    customer_id: str


@dataclass
class Authorisation:
    """The customer's approval step for a consent or a payment (steps 3 and 5)."""
    authorisation_id: str
    kind: str                 # "consent" or "payment"
    resource_id: str          # the consent id or payment id it belongs to
    # The standard's scaStatus values we use:
    #   received          = waiting for the customer
    #   psuAuthenticated  = customer signed in, has not decided yet
    #   finalised         = customer approved
    #   failed            = customer rejected
    status: str = "received"
    created_at: datetime | None = None
    customer_id: str | None = None        # set once the customer signs in
    # One-time code handed to the app after approval. The app swaps it for a token.
    code: str | None = None
    code_expires_at: datetime | None = None
    code_used: bool = False


@dataclass
class Payment:
    """A single domestic payment (step 5)."""
    payment_id: str
    consent_id: str
    customer_id: str | None
    status: str               # RCVD, PDNG, ACTC, ACCP, ACSC, RJCT, CANC
    product: str              # "domestic"
    debtor_iban: str
    creditor_iban: str
    creditor_name: str
    amount: Decimal
    currency: str
    remittance: str | None
    end_to_end_id: str | None
    created_at: datetime
    approval_deadline: datetime  # after this, an unapproved payment becomes RJCT


@dataclass
class State:
    """Everything in the mock bank. Lives in memory and resets on restart."""
    customers: dict[str, Customer]
    accounts: dict[str, Account]
    transactions: list[Transaction]
    consents: dict[str, Consent] = field(default_factory=dict)
    tokens: dict[str, Token] = field(default_factory=dict)                  # by access token
    refresh_tokens: dict[str, RefreshToken] = field(default_factory=dict)   # by refresh token
    authorisations: dict[str, Authorisation] = field(default_factory=dict)
    payments: dict[str, Payment] = field(default_factory=dict)
