"""The rules about a consent's life: when it expires, what approving it does.

Kept apart from the web code so the routes stay short and these rules are
easy to find. Every time is read from the mock clock (app/clock.py), so a
test can jump forward and see a consent expire.

Life of a consent:

    received --(customer approves)--> valid --(time runs out)--> expired
        |                               |
        |                               +--(app deletes it)--> terminatedByTpp
        +--(customer rejects)--> rejected
        +--(nobody answers in time)--> expired
"""

from datetime import date, datetime, time, timedelta, timezone

from app import clock, faults, settings, store
from app.errors import BankError
from app.models import Consent, Customer


def set_status(consent: Consent, status: str) -> None:
    """Change a consent's status and remember when."""
    consent.status = status
    consent.last_action = clock.now()


def refresh(consent: Consent) -> Consent:
    """Bring a consent's status up to date with the clock, and return it.

    A consent does not change by itself, because the mock has no background
    timer. So every call that touches a consent runs this first.
    """
    now = clock.now()
    rules = settings.get()
    if consent.status == "received":
        # Nobody approved in time (default: 24 hours).
        deadline = consent.created_at + timedelta(hours=rules.consent_setup_timeout_hours)
        if now > deadline:
            set_status(consent, "expired")
    elif consent.status == "valid" and consent.expires_at and now >= consent.expires_at:
        set_status(consent, "expired")
    return consent


def get_or_403(consent_id: str) -> Consent:
    """Find a consent or answer 403 CONSENT_UNKNOWN, as the standard says."""
    consent = store.STATE.consents.get(consent_id)
    if consent is None:
        raise BankError(403, "CONSENT_UNKNOWN", "This consent does not exist.")
    return refresh(consent)


def _end_of_day(day: date) -> datetime:
    """The last moment of a date, in UTC."""
    return datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)


def approve(consent: Consent, customer: Customer, granted_account_ids: list[str]) -> None:
    """The customer said yes: the consent becomes valid and gets its expiry time.

    Recurring consent: valid until the requested date, but never longer than
    consent_validity_days (default 90) from now.
    One-off consent: valid only for one_off_consent_validity_minutes (default 60).
    """
    now = clock.now()
    rules = settings.get()
    if consent.recurring_indicator:
        longest = now + timedelta(days=rules.consent_validity_days)
        expires = min(_end_of_day(consent.valid_until), longest)
    else:
        expires = min(_end_of_day(consent.valid_until),
                      now + timedelta(minutes=rules.one_off_consent_validity_minutes))

    # The "consent_expires_early" fault: the consent dies after 2 minutes instead.
    if faults.take("consent_expires_early", "consents.authorise"):
        expires = now + timedelta(minutes=2)

    consent.customer_id = customer.id
    consent.granted_account_ids = list(granted_account_ids)
    consent.expires_at = expires
    set_status(consent, "valid")


def reject(consent: Consent) -> None:
    """The customer said no."""
    set_status(consent, "rejected")


def referenced_ibans(consent: Consent) -> set[str]:
    """Every IBAN the app named in its request (accounts, balances or transactions)."""
    ibans: set[str] = set()
    for section in ("accounts", "balances", "transactions"):
        for reference in consent.access.get(section) or []:
            ibans.add(reference["iban"])
    return ibans


def may(consent: Consent, account, permission: str) -> bool:
    """May this consent use `permission` on this account?

    permission is "accounts" (list and details), "balances" or "transactions".
    Two things must both be true:
      1. the customer shared this account when approving, and
      2. the app asked for this kind of access to it when creating the consent.

    "accounts" is lenient: an account counts as visible if the app asked about
    it in any section, or asked for "allAccounts". Balances and transactions
    are strict: the IBAN must be in that very section. (A consent that only
    says "allAccounts" therefore lists accounts but cannot read balances.)
    """
    if account.resource_id not in consent.granted_account_ids:
        return False
    access = consent.access
    asked = {ref["iban"] for ref in access.get(permission) or []}
    if permission == "accounts":
        return bool(access.get("availableAccounts")) or account.iban in referenced_ibans(consent)
    return account.iban in asked


def count_read(consent: Consent) -> None:
    """Count one data read for today, or refuse with 429 if the daily limit is used up.

    The limit is the consent's frequencyPerDay (never above max_consent_requests_per_day).
    "Today" is the mock clock's date (UTC). Refused calls are not counted.
    """
    today = clock.now().date().isoformat()
    used = consent.reads_by_day.get(today, 0)
    if used >= consent.frequency_per_day:
        raise BankError(429, "ACCESS_EXCEEDED",
                        f"This consent allows {consent.frequency_per_day} reads a day. Try again tomorrow.")
    consent.reads_by_day[today] = used + 1
