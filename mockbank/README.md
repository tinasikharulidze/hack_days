# Mock Georgian open banking API

## What is this?

Open banking lets an app, with a customer's permission, read their bank
accounts and start payments through a standard set of web addresses (an API).
Georgia has its own version of this standard.

This project is a **fake bank** that offers those same addresses, so
developers can build and test apps against it without a real bank, real
money or real customers. It can also be made to fail on purpose (slow
answers, errors), so you can see how an app copes when things go wrong.

The standard it follows: Berlin Group NextGenPSD2, Georgian version 0.8.

## Status: step 2 of 6

| Step | What | State |
|---|---|---|
| 1 | Read the standard | Done |
| 2 | Skeleton: sample data, settings, faults, admin controls | **Done** |
| 3 | Consent and approval | Not built yet |
| 4 | Accounts, balances, transactions | Not built yet |
| 5 | Payments | Not built yet |
| 6 | Check against the standard, full test run | Not built yet |

**So today the mock does not act like a bank yet.** The bank calls are empty
placeholders. What works is everything around them, listed below.

## What works now

- **Sample data.** Two customers and five accounts (3 GEL, 1 USD, 1 EUR) with
  70 transactions, some booked (final) and some pending (not final yet).
  Everything is made up, and the IBANs are valid in form. See them at
  `GET /admin/data`.
- **Settings.** The Georgian rules (consent validity, daily request limit,
  timeouts, what is supported) are settings you can change. Each is labelled
  "unverified", "not confirmed" or "decided", so a guess is never passed off
  as a rule.
- **Faults.** Switch on slow answers or errors for any call, before or after
  the bank does its work. "After" is how duplicate payments happen: the bank
  takes the payment, the app sees an error and tries again.
- **Clock.** Jump the bank's time forward to test things like an expiring
  consent.
- **Standard errors.** Errors use the standard's format and only its codes. A
  500 or 503 has no body, as the standard says.
- **Reset.** One call puts everything back to the start.

## Run it

From the `hack_days` folder:

```
uv sync
cd mockbank
uv run uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs to see every call and try it. Run the tests
with `uv run pytest`.

## Admin controls

These are ours, not part of the standard. They have no password, so never put
them on a public server.

| Call | What it does |
|---|---|
| `GET /admin/settings` | Show the rules and how much we trust each |
| `PUT /admin/settings` | Change rules, e.g. `{"max_consent_requests_per_day": 2}` |
| `POST /admin/faults` | Switch a fault on, e.g. `{"kind": "server_error", "operation": "payments.initiate", "phase": "after", "times": 1}` |
| `GET /admin/faults` | List faults that are on |
| `DELETE /admin/faults/{id}` | Switch one off (`DELETE /admin/faults` switches all off) |
| `POST /admin/time/advance` | Jump the clock forward, e.g. `{"minutes": 90}` |
| `GET /admin/data` | See the customers, accounts and IBANs |
| `POST /admin/reset` | Start over |

To start with your own rules, copy `settings.example.json` to `settings.json`
and edit it. The mock reads it at startup.

## Where things are

| File | What it does |
|---|---|
| `app/main.py` | Starts the app and connects the pieces |
| `app/routers/` | The web addresses: consents, accounts, payments (placeholders) and admin (working) |
| `app/models.py` | The shapes of the data: customer, account, transaction, consent, payment |
| `app/store.py` | The bank's memory, plus reset and balance calculation |
| `app/seed.py` | Creates the sample data |
| `app/settings.py` | The bank's rules |
| `app/faults.py` | Makes the bank misbehave on purpose |
| `app/clock.py` | The bank's clock, which tests can move forward |
| `app/errors.py` | The standard error format and codes |
| `tests/` | Automated tests |

## Not planned

Foreign, bulk and recurring payments, card accounts, real certificates and
request signing, and any screen beyond the approval page.
