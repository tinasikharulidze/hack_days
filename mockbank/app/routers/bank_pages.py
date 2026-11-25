"""The mock bank's approval pages (step 3). A person sees these, not an app.

These are not part of the standard's API. The standard only says "send the
customer to the bank's page"; what that page looks like is the bank's own
business. Ours has two screens:

  1. Sign in      the customer picks nothing yet, just proves who they are
                  (fake usernames and passwords, listed in the README)
  2. Decide       the customer chooses which accounts to share, then
                  presses Approve or Reject

Afterwards the customer is sent back to the app's own address (the
TPP-Redirect-URI it gave when creating the consent):
  approved  ->  <redirect uri>?code=<one-time code>&consentId=<id>
  rejected  ->  <redirect uri>?error=access_denied&consentId=<id>

With the setting auto_approve on, there are no screens: opening the link
approves at once as the owner of the requested accounts, and sends the
customer straight back. Handy for automated tests.
"""

import secrets
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import brand, clock, consent_rules, settings, store
from app.brand import esc
from app.formdata import first, read_form
from app.models import Account, Authorisation, Consent, Customer

# include_in_schema=False: these are web pages, not API calls, so they stay out of /docs.
router = APIRouter(prefix="/bank", tags=["Mock bank pages"], include_in_schema=False)


# ---------- Helpers ----------

def _closed_page(message: str) -> HTMLResponse:
    """Shown when the link is unknown, used up or too old."""
    body = f"<h1>This request is closed</h1><p class='muted'>{esc(message)}</p>"
    return HTMLResponse(brand.page("Closed", body), status_code=400)


def _open_request(authorisation_id: str) -> tuple[Authorisation, Consent] | HTMLResponse:
    """Find the approval and its consent, if the customer can still act on them.

    Returns the pair, or a ready-made "closed" page to send back.
    """
    authorisation = store.STATE.authorisations.get(authorisation_id)
    if authorisation is None or authorisation.kind != "consent":
        return _closed_page("This link is not known to the bank.")
    consent = consent_rules.refresh(store.STATE.consents[authorisation.resource_id])
    if consent.status != "received" or authorisation.status not in ("received", "psuAuthenticated"):
        return _closed_page(f"It was already answered or has run out (status: {consent.status}).")
    return authorisation, consent


def _allowed_accounts(consent: Consent, customer: Customer) -> list[Account]:
    """The accounts this customer may share for this consent.

    If the app named IBANs, only the customer's own accounts among them.
    If it asked for "all accounts", all of the customer's accounts.
    """
    mine = [a for a in store.STATE.accounts.values() if a.customer_id == customer.id]
    asked = consent_rules.referenced_ibans(consent)
    return [a for a in mine if a.iban in asked] if asked else mine


def _redirect_back(consent: Consent, **params: str) -> RedirectResponse:
    """Send the customer back to the app, adding our answer to its address."""
    parts = urlparse(consent.tpp_redirect_uri)
    query = parse_qsl(parts.query) + list(params.items())
    target = urlunparse(parts._replace(query=urlencode(query)))
    return RedirectResponse(target, status_code=303)


def _finish_approval(authorisation: Authorisation, consent: Consent, customer: Customer,
                     accounts: list[Account]) -> RedirectResponse:
    """Record the approval, make the one-time code and send the customer back."""
    consent_rules.approve(consent, customer, [a.resource_id for a in accounts])
    authorisation.status = "finalised"
    authorisation.customer_id = customer.id
    authorisation.code = secrets.token_urlsafe(16)
    authorisation.code_expires_at = clock.now() + timedelta(
        minutes=settings.get().authorisation_code_validity_minutes)
    return _redirect_back(consent, code=authorisation.code, consentId=consent.consent_id)


def _permissions_text(consent: Consent) -> str:
    """Plain words for what the app asked to see."""
    access = consent.access
    wanted = []
    if access.get("accounts") or access.get("availableAccounts"):
        wanted.append("your list of accounts")
    if access.get("balances"):
        wanted.append("your balances")
    if access.get("transactions"):
        wanted.append("your transactions")
    return ", ".join(wanted) or "your account information"


# ---------- Screen 1: sign in ----------

def _login_page(authorisation: Authorisation, consent: Consent, error: str = "") -> HTMLResponse:
    if consent.recurring_indicator:
        how_long = (f"until <b>{esc(consent.valid_until)}</b>, up to "
                    f"<b>{esc(consent.frequency_per_day)}</b> reads a day")
    else:
        how_long = "<b>once</b>, for a short time"
    error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
    body = f"""
    <h1>An app wants to see your data</h1>
    <p class="muted">Sign in to your bank to decide.</p>
    <div class="info">The app asks to see <b>{esc(_permissions_text(consent))}</b>, {how_long}.</div>
    {error_html}
    <form method="post" action="/bank/approve/{esc(authorisation.authorisation_id)}/login">
      <label for="u">Username</label><input id="u" name="username" type="text" autocomplete="username" autofocus>
      <label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">
      <div class="buttons"><button class="primary" type="submit">Sign in</button></div>
    </form>"""
    return HTMLResponse(brand.page("Sign in", body))


@router.get("/approve/{authorisation_id}")
async def approval_start(authorisation_id: str):
    """The link the app gives the customer (scaRedirect). Shows the sign-in screen."""
    found = _open_request(authorisation_id)
    if isinstance(found, HTMLResponse):
        return found
    authorisation, consent = found

    if settings.get().auto_approve:
        # No screens: approve as the owner of the requested accounts (or the first customer).
        by_iban = {a.iban: a for a in store.STATE.accounts.values()}
        asked = sorted(consent_rules.referenced_ibans(consent))
        owner_id = by_iban[asked[0]].customer_id if asked else next(iter(store.STATE.customers))
        customer = store.STATE.customers[owner_id]
        return _finish_approval(authorisation, consent, customer, _allowed_accounts(consent, customer))

    return _login_page(authorisation, consent)


@router.post("/approve/{authorisation_id}/login")
async def approval_login(authorisation_id: str, request: Request):
    """Check the fake username and password, then show the decide screen."""
    found = _open_request(authorisation_id)
    if isinstance(found, HTMLResponse):
        return found
    authorisation, consent = found

    form = await read_form(request)
    username, password = first(form, "username"), first(form, "password")
    customer = next((c for c in store.STATE.customers.values()
                     if c.username == username and c.password == password), None)
    if customer is None:
        return _login_page(authorisation, consent, "Wrong username or password.")

    authorisation.status = "psuAuthenticated"
    authorisation.customer_id = customer.id
    return _decide_page(authorisation, consent, customer)


# ---------- Screen 2: choose accounts, approve or reject ----------

def _decide_page(authorisation: Authorisation, consent: Consent, customer: Customer,
                 error: str = "") -> HTMLResponse:
    accounts = _allowed_accounts(consent, customer)
    error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
    if accounts:
        rows = "".join(
            f"""<label class="account"><input type="checkbox" name="account" value="{esc(a.resource_id)}" checked>
                <span><b>{esc(a.name)} · {esc(a.currency)}</b><small>{esc(a.iban)}</small></span></label>"""
            for a in accounts)
        listing = f"<p class='muted'>Choose the accounts to share:</p>{rows}"
        approve_button = '<button class="primary" name="decision" value="approve" type="submit">Approve</button>'
    else:
        # The app asked about accounts that are not this customer's.
        listing = "<div class='error'>None of the accounts the app asked about belong to you.</div>"
        approve_button = ""
    body = f"""
    <h1>Hello, {esc(customer.name.split()[0])}</h1>
    <div class="info">The app wants to see <b>{esc(_permissions_text(consent))}</b>.</div>
    {error_html}
    <form method="post" action="/bank/approve/{esc(authorisation.authorisation_id)}/decide">
      {listing}
      <div class="buttons">
        <button class="secondary" name="decision" value="reject" type="submit">Reject</button>
        {approve_button}
      </div>
    </form>"""
    return HTMLResponse(brand.page("Approve", body))


@router.post("/approve/{authorisation_id}/decide")
async def approval_decide(authorisation_id: str, request: Request):
    """The customer pressed Approve or Reject."""
    found = _open_request(authorisation_id)
    if isinstance(found, HTMLResponse):
        return found
    authorisation, consent = found

    # Only a customer who signed in on screen 1 may decide.
    if authorisation.status != "psuAuthenticated" or authorisation.customer_id is None:
        return _closed_page("Please sign in first.")
    customer = store.STATE.customers[authorisation.customer_id]

    form = await read_form(request)
    if first(form, "decision") == "reject":
        consent_rules.reject(consent)
        authorisation.status = "failed"
        return _redirect_back(consent, error="access_denied", consentId=consent.consent_id)

    # Approve: the chosen accounts must be among the ones this customer may share.
    allowed = {a.resource_id: a for a in _allowed_accounts(consent, customer)}
    chosen_ids = [i for i in form.get("account", []) if i in allowed]
    if not chosen_ids:
        return _decide_page(authorisation, consent, customer, "Choose at least one account to share.")
    return _finish_approval(authorisation, consent, customer, [allowed[i] for i in chosen_ids])
