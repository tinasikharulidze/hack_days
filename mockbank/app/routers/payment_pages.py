"""The mock bank's payment approval pages (step 5). A person sees these, not an app.

Same idea as the consent pages (routers/bank_pages.py), and the same look:

  1. Sign in     the customer proves who they are. They must be the owner of the
                 account the money leaves, otherwise they are turned away.
  2. Decide      they see the payment (amount, to whom, from which account) and
                 press Approve or Reject.

Every payment needs this, even if the customer signed in for the consent a minute
ago. That is the "ask every time" trust level.

Afterwards the customer is sent back to the app's TPP-Redirect-URI:
  approved or rejected or failed  ->  <redirect uri>?paymentId=<id>   (+ error=... if it did not go through)
The app must still ask the status call what happened. The redirect is only a nudge.

With the setting auto_approve on there are no screens: opening the link approves
at once as the owner of the paying account.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import brand, payment_rules, settings, store
from app.brand import esc
from app.formdata import first, read_form
from app.models import Authorisation, Customer, Payment
from app.routers.bank_pages import _closed_page, redirect_to

router = APIRouter(prefix="/bank", tags=["Mock bank pages"], include_in_schema=False)


# ---------- Helpers ----------

def _open_request(authorisation_id: str) -> tuple[Authorisation, Payment] | HTMLResponse:
    """Find the approval and its payment, if the customer can still act on them."""
    authorisation = store.STATE.authorisations.get(authorisation_id)
    if authorisation is None or authorisation.kind != "payment":
        return _closed_page("This link is not known to the bank.")
    payment = payment_rules.refresh(store.STATE.payments[authorisation.resource_id])
    if payment.status != "RCVD" or authorisation.status not in ("received", "psuAuthenticated"):
        return _closed_page(f"It was already answered or has run out (status: {payment.status}).")
    return authorisation, payment


def _summary(payment: Payment) -> str:
    """The payment in plain words, for the customer to check."""
    debtor = payment_rules.account_by_iban(payment.debtor_iban)
    note = f"<br>Reference: {esc(payment.remittance)}" if payment.remittance else ""
    return (f"<div class='info'>Pay <b>{esc(f'{payment.amount:.2f}')} {esc(payment.currency)}</b> "
            f"to <b>{esc(payment.creditor_name)}</b><br>"
            f"<small>{esc(payment.creditor_iban)}</small><br>"
            f"From your {esc(debtor.name)} <small>({esc(debtor.iban)})</small>{note}</div>")


def _back_to_app(payment: Payment, **extra: str) -> RedirectResponse:
    return redirect_to(payment.tpp_redirect_uri, paymentId=payment.payment_id, **extra)


def _finish(payment: Payment) -> RedirectResponse:
    """The customer approved: try to complete the payment and send them back."""
    outcome = payment_rules.approve(payment)
    if outcome == "failed":
        return _back_to_app(payment, error="payment_failed")
    return _back_to_app(payment)


# ---------- Screen 1: sign in ----------

def _login_page(authorisation: Authorisation, payment: Payment, error: str = "") -> HTMLResponse:
    error_html = f"<div class='error'>{esc(error)}</div>" if error else ""
    body = f"""
    <h1>Approve a payment</h1>
    <p class="muted">An app wants to make this payment from your account. Sign in to decide.</p>
    {_summary(payment)}
    {error_html}
    <form method="post" action="/bank/pay/{esc(authorisation.authorisation_id)}/login">
      <label for="u">Username</label><input id="u" name="username" type="text" autocomplete="username" autofocus>
      <label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">
      <div class="buttons"><button class="primary" type="submit">Sign in</button></div>
    </form>"""
    return HTMLResponse(brand.page("Sign in", body))


@router.get("/pay/{authorisation_id}")
async def payment_start(authorisation_id: str):
    """The link the app gives the customer (scaRedirect). Shows the sign-in screen."""
    found = _open_request(authorisation_id)
    if isinstance(found, HTMLResponse):
        return found
    authorisation, payment = found

    if settings.get().auto_approve:
        authorisation.status = "finalised"
        authorisation.customer_id = payment.customer_id
        return _finish(payment)
    return _login_page(authorisation, payment)


@router.post("/pay/{authorisation_id}/login")
async def payment_login(authorisation_id: str, request: Request):
    """Check the fake username and password, and that this customer owns the paying account."""
    found = _open_request(authorisation_id)
    if isinstance(found, HTMLResponse):
        return found
    authorisation, payment = found

    form = await read_form(request)
    username, password = first(form, "username"), first(form, "password")
    customer = next((c for c in store.STATE.customers.values()
                     if c.username == username and c.password == password), None)
    if customer is None:
        return _login_page(authorisation, payment, "Wrong username or password.")
    if customer.id != payment.customer_id:
        return _login_page(authorisation, payment, "This payment is not from one of your accounts.")

    authorisation.status = "psuAuthenticated"
    authorisation.customer_id = customer.id
    return _decide_page(authorisation, payment, customer)


# ---------- Screen 2: approve or reject ----------

def _decide_page(authorisation: Authorisation, payment: Payment, customer: Customer) -> HTMLResponse:
    body = f"""
    <h1>Hello, {esc(customer.name.split()[0])}</h1>
    <p class="muted">Please check the payment:</p>
    {_summary(payment)}
    <form method="post" action="/bank/pay/{esc(authorisation.authorisation_id)}/decide">
      <div class="buttons">
        <button class="secondary" name="decision" value="reject" type="submit">Reject</button>
        <button class="primary" name="decision" value="approve" type="submit">Pay {esc(f'{payment.amount:.2f}')} {esc(payment.currency)}</button>
      </div>
    </form>"""
    return HTMLResponse(brand.page("Approve payment", body))


@router.post("/pay/{authorisation_id}/decide")
async def payment_decide(authorisation_id: str, request: Request):
    """The customer pressed Approve or Reject."""
    found = _open_request(authorisation_id)
    if isinstance(found, HTMLResponse):
        return found
    authorisation, payment = found

    # Only the owner, signed in on screen 1, may decide.
    if authorisation.status != "psuAuthenticated" or authorisation.customer_id != payment.customer_id:
        return _closed_page("Please sign in first.")

    form = await read_form(request)
    if first(form, "decision") == "reject":
        payment_rules.reject(payment)
        authorisation.status = "failed"
        return _back_to_app(payment, error="access_denied")

    authorisation.status = "finalised"
    return _finish(payment)
