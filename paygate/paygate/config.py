"""Settings for the layer. Every value has a default, so nothing is required.

To change one, put it in a file called paygate_settings.json next to where you
start the server (see paygate_settings.example.json), or set an environment
variable PAYGATE_SETTINGS=/path/to/file.json.
"""

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Config:
    # Where things are
    bank_url: str = "http://127.0.0.1:8000"                       # the mock bank
    callback_url: str = "http://127.0.0.1:8001/callback"          # where the bank sends the customer back to
    state_file: str = "paygate_state.json"                        # connection + payment records (survives restarts)
    audit_file: str = "paygate_audit.jsonl"                       # one line per tool call

    # Payment rules (all amounts in GEL; the mock only supports domestic GEL payments)
    max_payment_gel: str = "200.00"          # one payment may not be bigger than this
    max_per_day_gel: str = "500.00"          # all payments started in one day (UTC) together
    duplicate_window_minutes: int = 60       # an identical payment inside this time counts as the same payment

    # Talking to the bank
    bank_timeout_seconds: float = 10.0       # no answer after this long = "outcome unknown"
    consent_days: int = 30                   # how long we ask the customer to let us read their accounts
    reads_per_day: int = 4                   # the most the mock allows by default (frequencyPerDay)
    psu_ip_address: str = "192.0.2.1"        # the standard requires one; the mock ignores it
    max_transactions: int = 50               # the most transactions one tool answer returns (keeps answers small)


def load() -> Config:
    """Read the settings file if there is one; unknown names are refused so typos show up."""
    path = Path(os.environ.get("PAYGATE_SETTINGS", "paygate_settings.json"))
    values = json.loads(path.read_text()) if path.exists() else {}
    known = {f.name for f in fields(Config)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"Unknown setting(s) in {path}: {sorted(unknown)}")
    return Config(**values)
