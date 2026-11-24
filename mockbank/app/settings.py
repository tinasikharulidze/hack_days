"""The rules of the mock bank, in one place.

Anything the Georgian standard fixes (consent validity, daily limits,
timeouts, what is supported) is a setting here.
A developer can change these while the mock is running
(PUT /admin/settings) or start the mock with their own settings file.

Other modules must call get() every time they need a value. Never copy a
value into a variable that lives longer than one request, or later changes
to the settings will be ignored.
"""

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    # A misspelled setting name is an error instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")

    # --- Consent rules ---
    consent_validity_days: int = Field(
        90, ge=1
    )  # how long a recurring consent stays valid
    one_off_consent_validity_minutes: int = Field(
        60, ge=1
    )  # how long a one-off consent stays valid
    max_consent_requests_per_day: int = Field(
        4, ge=1
    )  # consent requests allowed per day
    consent_setup_timeout_hours: int = Field(
        24, ge=1
    )  # time allowed to approve a new consent

    # --- Payment rules ---
    payment_approval_timeout_minutes: int = Field(
        15, ge=1
    )  # time allowed to approve a payment
    supported_payment_products: list[str] = Field(default_factory=lambda: ["domestic"])

    # --- Access rules ---
    token_validity_minutes: int = Field(60, ge=1)  # how long an access token works
    account_identifier: Literal["IBAN"] = "IBAN"  # only IBANs identify accounts
    supported_booking_statuses: list[str] = Field(
        default_factory=lambda: [
            "booked",
            "pending",
            "both",
        ]  # "booked" must always be supported
    )
    with_balance_supported: bool = False  # the withBalance query parameter
    combined_service_indicator_supported: bool = (
        False  # the combinedServiceIndicator field
    )

    # --- Test convenience ---
    auto_approve: bool = False  # skip the Approve/Reject page and approve straight away


# How much we trust each value. Shown by GET /admin/settings
#   unverified    = taken from a summary of the Georgian rules, not the official document
#   not confirmed = we have not found this value anywhere yet
#   decided       = our own choice for this project
SETTING_STATUS = {
    "consent_validity_days": "unverified",
    "one_off_consent_validity_minutes": "unverified",
    "max_consent_requests_per_day": "unverified",
    "consent_setup_timeout_hours": "unverified",
    "payment_approval_timeout_minutes": "not confirmed",
    "supported_payment_products": "decided",
    "token_validity_minutes": "decided",
    "account_identifier": "unverified",
    "supported_booking_statuses": "decided",
    "with_balance_supported": "unverified",
    "combined_service_indicator_supported": "unverified",
    "auto_approve": "decided",
}


def _load_starting_settings() -> Settings:
    """Defaults, overlaid with a settings file if one exists.

    The file is "settings.json" in the folder the mock is started from, or
    the path in the MOCKBANK_SETTINGS environment variable.
    """
    path = Path(os.environ.get("MOCKBANK_SETTINGS", "settings.json"))
    if path.exists():
        return Settings(**json.loads(path.read_text(encoding="utf-8")))
    return Settings()


_current = _load_starting_settings()


def get() -> Settings:
    """The settings in force right now."""
    return _current


def update(changes: dict) -> Settings:
    """Change some settings. Raises pydantic.ValidationError if a name or value is wrong."""
    global _current
    # Build the new settings first, so a bad change leaves the old ones untouched.
    _current = Settings(**{**_current.model_dump(), **changes})
    return _current


def reset() -> None:
    """Go back to the starting settings (defaults plus the settings file)."""
    global _current
    _current = _load_starting_settings()
