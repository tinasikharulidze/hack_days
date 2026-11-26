"""Checking that an IBAN is well formed.

The check is the standard one: move the first four characters to the end,
turn letters into numbers (A=10 ... Z=35), and the whole number must leave
remainder 1 when divided by 97. It proves the IBAN is typed correctly, not
that the account exists.
"""

import re

_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def is_valid_iban(iban: str) -> bool:
    """True if the IBAN has the right shape and correct check digits."""
    if not _SHAPE.match(iban):
        return False
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1
