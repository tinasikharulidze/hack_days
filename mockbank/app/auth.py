"""Who is calling, and for which consent? (used by the account and payment calls)

Every call that reads accounts or starts payments must show two things:

  Authorization: Bearer <access token>   who is calling   (from the token step)
  Consent-ID: <consent id>               what they may see (from the consent step)

The two must belong together. require_access() checks both and gives back
the consent, so the route can then check which accounts it covers.

Step 3 builds and tests this check. Steps 4 and 5 put it on their routes with
    consent: Consent = Depends(require_access)
"""

from fastapi import Header

from app import clock, store
from app.consent_rules import refresh
from app.errors import BankError
from app.models import Consent


def require_access(
    authorization: str | None = Header(default=None),
    consent_id: str = Header(alias="Consent-ID"),   # missing header -> 400 FORMAT_ERROR
) -> Consent:
    """Check the token and the consent. Raises the standard error if anything is wrong."""
    # 1. The token must be there and look like "Bearer <token>".
    if not authorization or not authorization.lower().startswith("bearer "):
        raise BankError(401, "TOKEN_INVALID", "Send the access token as 'Authorization: Bearer <token>'.")
    token = store.STATE.tokens.get(authorization[7:].strip())
    if token is None:
        raise BankError(401, "TOKEN_UNKNOWN", "This access token is not known.")
    if clock.now() >= token.expires_at:
        raise BankError(401, "TOKEN_EXPIRED", "The access token has expired. Ask for a new one with the refresh token.")

    # 2. The consent must exist.
    consent = store.STATE.consents.get(consent_id)
    if consent is None:
        raise BankError(403, "CONSENT_UNKNOWN", "This consent does not exist.")
    refresh(consent)

    # 3. The token must belong to this consent (a token for consent A cannot open consent B).
    if token.consent_id != consent.consent_id:
        raise BankError(401, "CONSENT_INVALID", "This token was not issued for this consent.")

    # 4. The consent must be valid right now.
    if consent.status == "expired":
        raise BankError(401, "CONSENT_EXPIRED", "The consent has expired.")
    if consent.status != "valid":
        raise BankError(401, "CONSENT_INVALID", f"The consent is not valid (status: {consent.status}).")
    return consent
