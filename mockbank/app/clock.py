"""The mock bank's clock.

Everything in the mock that depends on time (consent expiry, payment
approval deadlines) asks this module for "now" instead of using the real
clock. That lets a test jump forward in time, for example to check what
happens when a consent expires, without waiting for it.
"""

from datetime import datetime, timedelta, timezone

# How far the mock's clock is ahead of the real clock.
_offset = timedelta(0)


def now() -> datetime:
    """Current time in the mock bank, in UTC."""
    return datetime.now(timezone.utc) + _offset


def advance(**kwargs) -> None:
    """Move the mock clock forward, e.g. advance(minutes=30)."""
    global _offset
    _offset += timedelta(**kwargs)


def reset() -> None:
    """Put the mock clock back in sync with the real clock."""
    global _offset
    _offset = timedelta(0)
