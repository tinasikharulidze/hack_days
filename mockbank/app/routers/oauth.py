"""The token step (step 3). NOT part of the Berlin Group standard.

The standard leaves "how does the app get an access token" to each bank's own
OAuth description, so this endpoint is our design. It copies the usual OAuth 2.0
shape (RFC 6749), so a real OAuth library works against it.

  POST /oauth/token      (form data, as OAuth requires)

  Swap the one-time code for tokens:
      grant_type=authorization_code & code=<code from the redirect>
  Get a fresh access token when the old one ran out:
      grant_type=refresh_token & refresh_token=<refresh token>

Answer (200):
  access_token   the short-lived key (default 60 minutes) shown with every call
  token_type     always "Bearer"
  expires_in     seconds until the access token stops working
  refresh_token  the longer-lived key; it works as long as the consent does
  consent_id     our extra: which consent these tokens belong to

Errors use OAuth's own format {"error": ..., "error_description": ...}, not the
standard's tppMessages. We do not check the app's identity (no client id or
secret): the real standard uses certificates for that, which we skip.
"""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import clock, consent_rules, faults, settings, store
from app.formdata import first, read_form
from app.models import Consent, RefreshToken, Token

router = APIRouter(prefix="/oauth", tags=["Token (not part of the standard)"])

_NO_CACHE = {"Cache-Control": "no-store", "Pragma": "no-cache"}   # OAuth: never cache tokens


def _oauth_error(error: str, description: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": error, "error_description": description},
                        headers=_NO_CACHE)


def _issue_access_token(consent: Consent, customer_id: str, refresh_token: str) -> JSONResponse:
    """Make a new access token for a consent and build the answer."""
    minutes = settings.get().token_validity_minutes
    token = Token(access_token=secrets.token_urlsafe(24), consent_id=consent.consent_id,
                  customer_id=customer_id, expires_at=clock.now() + timedelta(minutes=minutes))
    store.STATE.tokens[token.access_token] = token
    return JSONResponse(content={
        "access_token": token.access_token,
        "token_type": "Bearer",
        "expires_in": minutes * 60,
        "refresh_token": refresh_token,
        "consent_id": consent.consent_id,
    }, headers=_NO_CACHE)


@router.post("/token", dependencies=[Depends(faults.fault_before("oauth.token"))])
async def token(request: Request):
    """Swap a one-time code or a refresh token for an access token."""
    form = await read_form(request)
    grant_type = first(form, "grant_type")

    if grant_type == "authorization_code":
        code = first(form, "code")
        if not code:
            return _oauth_error("invalid_request", "The code is missing.")
        # Find the approval this code was handed out for.
        authorisation = next((a for a in store.STATE.authorisations.values() if a.code == code), None)
        if authorisation is None or authorisation.code_used:
            return _oauth_error("invalid_grant", "The code is unknown or was already used.")
        if clock.now() >= authorisation.code_expires_at:
            return _oauth_error("invalid_grant", "The code has expired.")
        consent = consent_rules.refresh(store.STATE.consents[authorisation.resource_id])
        if consent.status != "valid":
            return _oauth_error("invalid_grant", f"The consent is not valid (status: {consent.status}).")

        authorisation.code_used = True   # a code works once
        refresh_token = secrets.token_urlsafe(24)
        store.STATE.refresh_tokens[refresh_token] = RefreshToken(
            refresh_token=refresh_token, consent_id=consent.consent_id, customer_id=consent.customer_id)
        response = _issue_access_token(consent, consent.customer_id, refresh_token)

    elif grant_type == "refresh_token":
        refresh_token = first(form, "refresh_token")
        if not refresh_token:
            return _oauth_error("invalid_request", "The refresh_token is missing.")
        stored = store.STATE.refresh_tokens.get(refresh_token)
        if stored is None:
            return _oauth_error("invalid_grant", "The refresh token is unknown.")
        consent = consent_rules.refresh(store.STATE.consents[stored.consent_id])
        if consent.status != "valid":
            # When the consent ends, the refresh token ends with it.
            return _oauth_error("invalid_grant", f"The consent is not valid (status: {consent.status}).")
        response = _issue_access_token(consent, stored.customer_id, refresh_token)

    else:
        return _oauth_error("unsupported_grant_type",
                            "Use grant_type=authorization_code or grant_type=refresh_token.")

    await faults.run("oauth.token", "after")
    return response
