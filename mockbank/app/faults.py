"""Fault injection: making the mock bank misbehave on purpose.

A developer (or a test script) turns a fault on through the admin API.
Endpoints then call into this module at two moments:

  before = before the bank does the work
  after  = after the bank did the work, just before it answers

The "after" moment matters most. A slow or failing answer AFTER the bank
already took the payment is exactly how duplicate payments happen: the app
sees an error or a timeout and tries again.

Fault kinds:
  slow                   wait delay_seconds, then carry on
  server_error           stop with an error: status_code 500, 503 or 429
  payment_stays_pending  a payment never moves past "pending"   (used in step 5)
  consent_expires_early  a consent expires before its time      (used in step 3)

The first two act right here. The last two are only switches: the code that
handles payments and consents asks for them with take().
"""

import asyncio
from dataclasses import asdict, dataclass

from app.errors import BankError

KINDS = ("slow", "server_error", "payment_stays_pending", "consent_expires_early")

# Every call a fault can target. "*" means all calls, and "payments.*"
# means every payments call.
OPERATIONS = (
    "consents.create", "consents.get", "consents.status", "consents.delete", "consents.authorise",
    "accounts.list", "accounts.details", "accounts.balances",
    "accounts.transactions", "accounts.transaction",
    "payments.initiate", "payments.get", "payments.status", "payments.cancel", "payments.authorise",
    "oauth.token",   # the token step (not part of the standard, see routers/oauth.py)
)


@dataclass
class Fault:
    id: int
    kind: str
    operation: str = "*"       # which call it targets
    phase: str = "before"      # "before" or "after" the work (slow and server_error only)
    times: int | None = None   # how many calls it affects; None = until removed
    delay_seconds: float = 0   # for "slow"
    status_code: int = 503     # for "server_error"


_faults: list[Fault] = []
_next_id = 1


def add(**fields) -> Fault:
    """Turn a fault on. Returns it, including its new id."""
    global _next_id
    fault = Fault(id=_next_id, **fields)
    _next_id += 1
    _faults.append(fault)
    return fault


def remove(fault_id: int) -> bool:
    """Turn one fault off. Returns False if there was no such fault."""
    for fault in _faults:
        if fault.id == fault_id:
            _faults.remove(fault)
            return True
    return False


def clear() -> None:
    """Turn every fault off."""
    global _next_id
    _faults.clear()
    _next_id = 1


def listing() -> list[dict]:
    """All faults currently on, as plain dicts."""
    return [asdict(f) for f in _faults]


def _matches(fault: Fault, operation: str) -> bool:
    """Does this fault target this call?"""
    if fault.operation == "*" or fault.operation == operation:
        return True
    # "payments.*" matches "payments.initiate", "payments.status", ...
    return fault.operation.endswith(".*") and operation.startswith(fault.operation[:-1])


def _use_up_one(fault: Fault) -> None:
    """Count one use of a fault, and remove it when its uses run out."""
    if fault.times is not None:
        fault.times -= 1
        if fault.times <= 0 and fault in _faults:
            _faults.remove(fault)


async def run(operation: str, phase: str) -> None:
    """The hook the endpoints call. Applies any slow / server_error fault that matches.

    Call run("payments.initiate", "before") at the start of a call and
    run("payments.initiate", "after") once the work is done.
    """
    for fault in list(_faults):
        if fault.kind not in ("slow", "server_error"):
            continue
        if fault.phase != phase or not _matches(fault, operation):
            continue
        _use_up_one(fault)
        if fault.kind == "slow":
            await asyncio.sleep(fault.delay_seconds)
        else:
            raise BankError(fault.status_code, _code_for(fault.status_code, operation),
                            "Injected fault (set through the admin API).")


def take(kind: str, operation: str = "*") -> Fault | None:
    """For switch-style faults: is this fault on? If so, use up one of its uses and return it."""
    for fault in _faults:
        if fault.kind == kind and _matches(fault, operation):
            _use_up_one(fault)
            return fault
    return None


def fault_before(operation: str):
    """A FastAPI dependency that runs the "before" hook for one call.

    Use it in a route as:  dependencies=[Depends(fault_before("payments.initiate"))]
    """

    async def dependency() -> None:
        await run(operation, "before")

    return dependency


def _code_for(status_code: int, operation: str) -> str | None:
    """The standard code a server_error fault sends, or None for "no body".

    The standard defines no codes for 500 and 503, so those answers have no
    body. A 429 has the code ACCESS_EXCEEDED on account calls only; on
    payment calls it has no body either. The token step is not part of the
    standard, so it gets no body for 429 as well.
    """
    if status_code == 429 and not operation.startswith("payments.") and operation != "oauth.token":
        return "ACCESS_EXCEEDED"
    return None
