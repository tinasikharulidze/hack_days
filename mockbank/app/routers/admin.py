"""Admin API: controls for the person running the mock. Not part of the bank standard.

Use it to change the bank's rules, switch faults on and off, jump forward
in time, look at the sample data, and reset everything. There is no password on purpose
"""

import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from app import clock, faults, settings, store

router = APIRouter(prefix="/admin", tags=["Admin (not part of the standard)"])


# ---------- Settings ----------


@router.get("/settings")
def get_settings():
    """The bank's current rules, and how much we trust each value."""
    return {
        "settings": settings.get().model_dump(),
        "status": settings.SETTING_STATUS,
    }


@router.put("/settings")
def change_settings(changes: dict):
    """Change one or more rules. Send only the ones you want to change.

    Example body: {"max_consent_requests_per_day": 2, "auto_approve": true}
    """
    try:
        updated = settings.update(changes)
    except ValidationError as error:
        # Unknown setting name or an invalid value: nothing was changed.
        raise HTTPException(status_code=422, detail=json.loads(error.json()))
    return {"settings": updated.model_dump()}


# ---------- Faults ----------


class FaultIn(BaseModel):
    """What to send to switch a fault on."""

    kind: Literal[*faults.KINDS]  # one of the fault kinds listed in faults.py
    operation: str = "*"
    phase: Literal["before", "after"] = "before"
    times: int | None = Field(None, ge=1)
    delay_seconds: float = Field(0, ge=0, le=300)
    # The only error statuses that make sense as a bank fault. 500 and 503 come
    # with no body; 429 means "too many requests".
    status_code: Literal[429, 500, 503] = 503


@router.get("/faults")
def list_faults():
    """Faults that are on right now."""
    return {
        "faults": faults.listing(),
        "kinds": faults.KINDS,
        "operations": faults.OPERATIONS,
    }


@router.post("/faults", status_code=201)
def add_fault(fault: FaultIn):
    """Switch a fault on."""
    allowed = (
        fault.operation == "*"
        or fault.operation in faults.OPERATIONS
        or (
            fault.operation.endswith(".*")
            and any(op.startswith(fault.operation[:-1]) for op in faults.OPERATIONS)
        )
    )
    if not allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown operation. Use one of: {faults.OPERATIONS}",
        )
    return faults.add(**fault.model_dump()).__dict__


@router.delete("/faults/{fault_id}")
def remove_fault(fault_id: int):
    """Switch one fault off."""
    if not faults.remove(fault_id):
        raise HTTPException(status_code=404, detail="No fault with that id.")
    return {"removed": fault_id}


@router.delete("/faults")
def remove_all_faults():
    """Switch every fault off."""
    faults.clear()
    return {"faults": []}


# ---------- Time ----------


class AdvanceIn(BaseModel):
    minutes: float = Field(gt=0, le=60 * 24 * 365)


@router.post("/time/advance")
def advance_time(body: AdvanceIn):
    """Jump the bank's clock forward, for example to make a consent expire."""
    clock.advance(minutes=body.minutes)
    return {"now": clock.now().isoformat()}


# ---------- Data and reset ----------


@router.get("/data")
def sample_data():
    """The customers and accounts in the mock, so you know which IBANs to use."""
    state = store.STATE
    return {
        "customers": [{"id": c.id, "name": c.name} for c in state.customers.values()],
        "accounts": [
            {
                "resource_id": a.resource_id,
                "customer_id": a.customer_id,
                "iban": a.iban,
                "currency": a.currency,
                "name": a.name,
                "balances": {
                    k: str(v) for k, v in store.balances(a.resource_id).items()
                },
            }
            for a in state.accounts.values()
        ],
        "transaction_count": len(state.transactions),
    }


@router.post("/reset")
def reset():
    """Start over: fresh data, starting settings, no faults, real clock."""
    store.reset_all()
    return {"reset": True}
