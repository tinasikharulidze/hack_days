"""Sample data: two customers with several accounts and transactions.

Everything here is made up. The bank code "MB" is not a real bank, and the
IBANs are valid in form (their check digits are correct) so apps that
validate IBANs accept them.

The data is generated with a fixed random seed, so every start gives the
same accounts, amounts and descriptions. Only the dates move, because they
are counted back from today.
"""

import random
import uuid
from datetime import timedelta
from decimal import Decimal

from app import clock
from app.models import Account, Customer, State, Transaction

BANK_CODE = "MB"  # fictional bank code used in every IBAN of the mock

# Money-in and money-out patterns: (counterparty, description, direction, min, max)
# direction: +1 = money in, -1 = money out. Amounts are in the account's currency.
CURRENT_PATTERNS = [
    ("Example LLC", "Salary", +1, 2400, 3000),
    ("Supermarket", "Groceries", -1, 15, 120),
    ("Pharmacy", "Medicine", -1, 8, 60),
    ("Electricity provider", "Electricity bill", -1, 30, 90),
    ("Water utility", "Water bill", -1, 10, 25),
    ("Mobile operator", "Mobile top-up", -1, 10, 40),
    ("Taxi service", "Taxi ride", -1, 5, 25),
    ("Restaurant", "Dinner", -1, 20, 80),
    ("Friend", "Transfer to friend", -1, 20, 200),
]
SAVINGS_PATTERNS = [
    ("Own current account", "Transfer from current account", +1, 200, 800),
    ("MockBank", "Interest", +1, 5, 40),
]


def make_iban(account_number: str) -> str:
    """Build a Georgian-format IBAN with correct check digits.

    A Georgian IBAN has 22 characters: "GE", 2 check digits, 2 letters for
    the bank, then 16 digits for the account.
    """
    bban = BANK_CODE + account_number
    # The IBAN check: turn letters into numbers (A=10 ... Z=35), append the
    # country and "00", and take the remainder when dividing by 97.
    numeric = "".join(str(int(ch, 36)) for ch in bban + "GE00")
    check = 98 - int(numeric) % 97
    return f"GE{check:02d}{bban}"


def _money(rng: random.Random, low: float, high: float) -> Decimal:
    """A random amount with two decimals."""
    return Decimal(str(round(rng.uniform(low, high), 2))).quantize(Decimal("0.01"))


def _transactions_for(account: Account, count: int, rng: random.Random) -> list[Transaction]:
    """Generate `count` transactions for one account, newest first.

    The two most recent ones on a current account are pending, the rest are booked.
    """
    today = clock.now().date()
    patterns = SAVINGS_PATTERNS if account.cash_account_type == "SVGS" else CURRENT_PATTERNS
    # Spread the transactions over roughly the last 60 days, newest first.
    day_offsets = sorted(rng.sample(range(0, 60), count))
    transactions = []
    for index, offset in enumerate(day_offsets):
        name, description, direction, low, high = rng.choice(patterns)
        # A real current account receives a salary now and then. Pure chance left some
        # accounts with no money coming in at all, so every 8th transaction is a salary.
        if account.cash_account_type != "SVGS" and index % 8 == 3:
            name, description, direction, low, high = CURRENT_PATTERNS[0]
        is_pending = account.cash_account_type != "SVGS" and index < 2
        day = today - timedelta(days=offset)
        transactions.append(
            Transaction(
                transaction_id=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                account_id=account.resource_id,
                status="pending" if is_pending else "booked",
                amount=direction * _money(rng, low, high),
                currency=account.currency,
                booking_date=None if is_pending else day,
                value_date=day,
                counterparty_name=name,
                counterparty_iban=make_iban(f"{rng.randrange(10**16):016d}"),
                remittance=description,
            )
        )
    return transactions


def build_state() -> State:
    """Create a fresh bank: 2 customers, 5 accounts (3 GEL, 1 USD, 1 EUR) and 70 transactions."""
    rng = random.Random(2026)  # fixed seed, so the data is the same every start

    customers = {
        # Fake sign-ins for the approval page. Test values only, listed in the README.
        "c1": Customer(id="c1", name="Nino Kezherashvili", username="nino", password="nino"),
        "c2": Customer(id="c2", name="Luka Darsalia", username="luka", password="luka"),
    }

    # (resource_id, customer, currency, account name, product, type, opening balance, transactions)
    definitions = [
        ("a1", "c1", "GEL", "Current account", "Current account", "CACC", "3200.00", 20),
        ("a2", "c1", "GEL", "Savings account", "Savings account", "SVGS", "10500.00", 6),
        ("a3", "c1", "USD", "Dollar account", "Current account", "CACC", "850.00", 12),
        ("a4", "c2", "GEL", "Current account", "Current account", "CACC", "1800.00", 20),
        ("a5", "c2", "EUR", "Euro account", "Current account", "CACC", "2500.00", 12),
    ]

    accounts: dict[str, Account] = {}
    transactions: list[Transaction] = []
    for number, (rid, cust, currency, name, product, kind, opening, count) in enumerate(definitions, start=1):
        account = Account(
            resource_id=rid,
            customer_id=cust,
            iban=make_iban(f"{number:016d}"),
            currency=currency,
            name=name,
            product=product,
            owner_name=customers[cust].name,
            opening_balance=Decimal(opening),
            cash_account_type=kind,
        )
        accounts[rid] = account
        transactions.extend(_transactions_for(account, count, rng))

    return State(customers=customers, accounts=accounts, transactions=transactions)
