"""The bank's memory.

STATE holds everything the mock knows (customers, accounts, consents,
payments). It lives in memory only, so restarting the mock gives a clean
bank. Other modules import STATE and read or change it directly.
"""

from decimal import Decimal

from app import clock, faults, settings
from app.models import State
from app.seed import build_state

STATE: State = build_state()


def reset_all() -> None:
    """Return the whole mock to its starting condition.

    Fresh sample data, no consents or payments, starting settings, no
    faults, and the clock back in sync with the real one.
    """
    # Replace the contents of the existing object instead of making a new one,
    # so modules that already imported STATE keep seeing the current data.
    STATE.__dict__.update(build_state().__dict__)
    settings.reset()
    faults.clear()
    clock.reset()


def balances(account_id: str) -> dict[str, Decimal]:
    """Work out an account's balances from its transactions.

    interimBooked    = opening balance plus all booked transactions
    interimAvailable = interimBooked plus pending transactions (money in or out
                       that is not final yet)
    """
    account = STATE.accounts[account_id]
    mine = [t for t in STATE.transactions if t.account_id == account_id]
    booked = account.opening_balance + sum(t.amount for t in mine if t.status == "booked")
    pending = sum(t.amount for t in mine if t.status == "pending")
    return {"interimBooked": booked, "interimAvailable": booked + pending}
