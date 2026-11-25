# Mock Georgian open banking API

## 1. What is this?

Open banking lets an app, with a customer's permission, read their bank
accounts and start payments through a standard set of web addresses (an API).
Georgia has its own version of this standard.

This project is a **fake bank** that offers those same addresses. Developers
can build and test apps against it without a real bank, real money or real
customers. It can also be made to fail on purpose (slow answers, errors), so
you can see how an app copes when things go wrong.

Standard followed: Berlin Group NextGenPSD2, Georgian version 0.8.
Everything here is made up. Never use real data.

### Words you will meet

| Word | Meaning |
|---|---|
| **Bank** | The thing we are faking. |
| **TPP** | The app that wants to read accounts or start payments. That is you, the developer. |
| **Customer** | The person who owns the accounts and says yes or no. |
| **Consent** | The customer's permission for the TPP to read something. It has an expiry date. |
| **Access token** | A short-lived key the app shows with every call, so the bank knows who is calling. |
| **Refresh token** | A longer-lived key the app swaps for a new access token. No customer needed. |
| **Booked** | A transaction that is final. |
| **Pending** | A transaction the bank has seen but not finished. It can still change. |
| **IBAN** | The account number. |

## 2. Status: step 3 of 6

| Step | What | State |
|---|---|---|
| 1 | Read the standard | Done |
| 2 | Skeleton: sample data, settings, faults, clock, errors, admin controls | Done |
| 3 | Consent, approval page, token | **Done** |
| 4 | Accounts, balances, transactions | Not built yet |
| 5 | Payments | Not built yet |
| 6 | Check against the standard, full test run | Not built yet |

**Today the mock can give an app a valid access token, but there is nothing to
read with it yet.** The account and payment files are still empty placeholders.

## 3. Run it

From the `hack_days` folder:

```
uv sync
cd mockbank
uv run uvicorn app.main:app --reload
```

- http://127.0.0.1:8000/docs lists every call and lets you try it.
- http://127.0.0.1:8000/health answers `{"status": "ok"}` if the mock is up.
- `uv run pytest` runs the tests (62 today).

The mock keeps everything in memory. Stop it and it forgets everything.

## 4. The consent flow (step 3)

This is how an app gets permission, in five moves.

```
App                                Bank (this mock)                 Customer
 |  1. POST /v1/consents  ------->   |
 |  <---- consentId + link           |   status: received
 |  2. sends the customer to the link ------------------------------>|
 |                                   |  <---- signs in, picks accounts, Approve
 |                                   |   status: valid
 |  <---- customer comes back to the app's address with ?code=...     |
 |  3. POST /oauth/token (code)  --> |
 |  <---- access token + refresh token
 |  4. later: POST /oauth/token (refresh token) --> new access token
```

### 4.1 Create a consent

`POST /v1/consents` with these headers: `X-Request-ID` (any UUID),
`PSU-IP-Address` (any IP), `TPP-Redirect-URI` (where to send the customer back).

```
{
  "access": {"accounts": [{"iban": "GE94MB0000000000000001"}],
             "balances": [{"iban": "GE94MB0000000000000001"}]},
  "recurringIndicator": true,
  "validUntil": "2026-12-20",
  "frequencyPerDay": 4,
  "combinedServiceIndicator": false
}
```

The answer is `201` with a `consentId`, status `received`, and links. The one
that matters is `scaRedirect`: send the customer there.
To ask for all of a customer's accounts, use
`"access": {"availableAccounts": "allAccounts"}`.

The bank refuses a request with `400` when:

| Problem | Code |
|---|---|
| A header is missing, or the body is badly formed | `FORMAT_ERROR` |
| `combinedServiceIndicator` is true (not supported) | `PARAMETER_NOT_SUPPORTED` |
| `frequencyPerDay` is above the daily limit (4) | `PARAMETER_NOT_CONSISTENT` |
| A one-off consent (`recurringIndicator` false) with `frequencyPerDay` not 1 | `PARAMETER_NOT_CONSISTENT` |
| `validUntil` is in the past, or nothing is asked for | `PARAMETER_NOT_CONSISTENT` |
| The accounts belong to two different customers | `PARAMETER_NOT_CONSISTENT` |
| An IBAN does not exist here | `RESOURCE_UNKNOWN` |

A `validUntil` too far ahead is not refused. It is cut back to 90 days, and
`GET /v1/consents/{id}` shows the date you actually got.

**About "4 a day".** The setting `max_consent_requests_per_day` is the most
reads per day an app may ask for (`frequencyPerDay`). Step 3 only checks that
the number is allowed. Step 4 will refuse the 5th read of the day with `429`.

### 4.2 The customer approves

The customer opens the link and sees two screens: **sign in**, then **choose
accounts and press Approve or Reject**. They can share fewer accounts than the
app asked for. An account that is not theirs is never offered.

Fake sign-ins (test values, nothing real):

| Customer | Username | Password |
|---|---|---|
| Nino Beridze | `nino` | `mock-nino-123` |
| Giorgi Kapanadze | `giorgi` | `mock-giorgi-123` |

The same list is at `GET /admin/data`.

Afterwards the customer is sent back to the app's `TPP-Redirect-URI`:

- approved: `...?code=<one-time code>&consentId=<id>`
- rejected: `...?error=access_denied&consentId=<id>`

For automated tests, set `auto_approve` to true (see 5.2). Opening the link then
approves at once, with no screens, and still sends the customer back with a code.

### 4.3 Get a token

`POST /oauth/token` as a normal web form (`application/x-www-form-urlencoded`).

| To do this | Send |
|---|---|
| Swap the code for tokens | `grant_type=authorization_code&code=<code>` |
| Get a new access token | `grant_type=refresh_token&refresh_token=<refresh token>` |

The answer is `access_token`, `token_type` ("Bearer"), `expires_in` (seconds),
`refresh_token`, and `consent_id`.

- The code works **once** and only for 10 minutes.
- The access token lasts 60 minutes. Then the app uses the refresh token.
- The refresh token lasts as long as the consent. When the consent ends, it stops working.
- Errors use OAuth's own format, for example
  `{"error": "invalid_grant", "error_description": "..."}`.

**This step is not in the standard.** The standard leaves it to each bank, so
this part is our own design, copied from common OAuth. Certificates and client
secrets (the bank checking which app is calling) are not included.

### 4.4 Read, check and delete a consent

| Call | What it does |
|---|---|
| `GET /v1/consents/{id}` | What was asked, until when, and the status |
| `GET /v1/consents/{id}/status` | Just the status. Apps poll this while the customer decides. |
| `DELETE /v1/consents/{id}` | The app withdraws it. Status becomes `terminatedByTpp`. |
| `POST /v1/consents/{id}/authorisations` | Get a fresh approval link (`409` if the consent is no longer open) |

These need no token, because the token only exists after approval. An unknown
consent id gives `403 CONSENT_UNKNOWN`.

### 4.5 How long a consent lives

Statuses: `received` → `valid` → `expired`, or `rejected`, or `terminatedByTpp`.

| Case | Rule (setting) |
|---|---|
| Not approved in time | Becomes `expired` after 24 hours (`consent_setup_timeout_hours`) |
| Recurring consent | Valid until `validUntil`, never more than 90 days (`consent_validity_days`) |
| One-off consent | Valid for 60 minutes after approval (`one_off_consent_validity_minutes`) |

The mock has no background timer. A consent's status is brought up to date
each time someone asks. Use the mock clock (5.4) to test expiry.

### 4.6 The access check (ready for steps 4 and 5)

Every account or payment call will need two headers:

```
Authorization: Bearer <access token>
Consent-ID: <consent id>
```

| Problem | Answer |
|---|---|
| `Consent-ID` missing | 400 `FORMAT_ERROR` |
| No token, or not written as `Bearer ...` | 401 `TOKEN_INVALID` |
| Token not known | 401 `TOKEN_UNKNOWN` |
| Token ran out | 401 `TOKEN_EXPIRED` (use the refresh token) |
| Consent not known | 403 `CONSENT_UNKNOWN` |
| Token belongs to a different consent | 401 `CONSENT_INVALID` |
| Consent ended (deleted, rejected, not approved) | 401 `CONSENT_INVALID` |
| Consent ran out | 401 `CONSENT_EXPIRED` |

It is built and tested, but no real call uses it yet.

## 5. What else works

### 5.1 Sample data

Built fresh at every start (and on reset). The numbers are the same every
time, so tests can rely on them.

| | |
|---|---|
| Customers | 2: Nino Beridze, Giorgi Kapanadze |
| Accounts | 5: 3 in GEL (2 current, 1 savings), 1 in USD, 1 in EUR |
| Transactions | 70 in total, 8 of them pending |
| IBANs | Look real: `GE`, two check digits, `MB`, 16 digits. They pass the standard IBAN check. |
| Balances | No account starts overdrawn. |

Each account reports two balances, as the standard does:

- **interimBooked**: the starting amount plus the booked transactions only.
- **interimAvailable**: the same, plus pending transactions.

See all of it at `GET /admin/data`, including the IBANs you will need.

### 5.2 Settings (the bank's rules)

The Georgian rules are not hard-coded. They are settings, so you can change
them and test how your app reacts. Each one is labelled with how far we trust
it. **Several values come from a non-official summary of the Georgian rules
and have not been checked against the official document.**

| Setting | Default | Trust |
|---|---|---|
| `consent_validity_days` | 90 | unverified |
| `one_off_consent_validity_minutes` | 60 | unverified |
| `max_consent_requests_per_day` | 4 | unverified |
| `consent_setup_timeout_hours` | 24 | unverified |
| `payment_approval_timeout_minutes` | 15 | not confirmed |
| `account_identifier` | IBAN | unverified |
| `with_balance_supported` | false | unverified |
| `combined_service_indicator_supported` | false | unverified |
| `supported_payment_products` | domestic | decided |
| `supported_booking_statuses` | booked, pending, both | decided |
| `token_validity_minutes` | 60 | decided |
| `authorisation_code_validity_minutes` | 10 | decided |
| `auto_approve` | false | decided |

"Decided" means we chose it, not that the standard says so.

Two ways to change them:

1. **While running:** `PUT /admin/settings` with only the values to change,
   for example `{"auto_approve": true}`. Reset puts them back.
2. **At start:** copy `settings.example.json` to `settings.json` in the folder
   you start the mock from, edit it, restart. To use another file, set the
   environment variable `MOCKBANK_SETTINGS` to its path.

Unknown setting names and impossible values (such as 0 days) are rejected.

### 5.3 Faults (making the bank misbehave)

Real banks are slow and fail. Switch on a fault and the mock does the same.

| Kind | What it does |
|---|---|
| `slow` | Waits `delay_seconds` (0 to 300), then carries on. |
| `server_error` | Stops with status `429`, `500` or `503`. |
| `consent_expires_early` | The next approved consent dies after 2 minutes instead of its full time. |
| `payment_stays_pending` | A payment never gets past "pending". Used from step 5. |

Options for a fault:

| Option | Meaning |
|---|---|
| `operation` | Which call it hits. One of the names below, `payments.*` for every payment call, or `*` for everything (default). |
| `phase` | `before` (default): fails before the bank does the work. `after`: the bank does the work, then fails. |
| `times` | How many calls it affects, then it switches itself off. Leave out for "until removed". |

Operations: `consents.create`, `consents.get`, `consents.status`,
`consents.delete`, `consents.authorise`, `accounts.list`, `accounts.details`,
`accounts.balances`, `accounts.transactions`, `accounts.transaction`,
`payments.initiate`, `payments.get`, `payments.status`, `payments.cancel`,
`payments.authorise`, `oauth.token`.

The approval pages themselves have no faults, because a person uses them.

**Why `after` matters.** Imagine the bank takes a payment, then answers 503.
Your app sees an error, tries again, and the customer pays twice. Phase
`after` lets you test exactly this.

Example: make the next payment start succeed but report an error.

```
POST /admin/faults
{"kind": "server_error", "operation": "payments.initiate",
 "phase": "after", "times": 1, "status_code": 503}
```

### 5.4 Clock

`POST /admin/time/advance` with `{"minutes": 90}` moves the bank's clock
forward. Use it to see what your app does when a token or consent has
expired, without waiting. Reset puts the clock back to real time.

### 5.5 Errors

Errors on the standard's calls follow the standard:

```
{"tppMessages": [{"category": "ERROR", "code": "CONSENT_UNKNOWN", "text": "..."}]}
```

- Each HTTP status allows only certain codes. The mock refuses to send a
  made-up code.
- **500 and 503 have no body**, because the standard defines no code for them.
  The same applies to a 429 on a payment call or on the token step. Your app
  must cope with an empty error.
- A 429 on an account call carries `ACCESS_EXCEEDED`.
- A badly formed request returns 400 `FORMAT_ERROR`.
- The token step (`/oauth/token`) is the exception: it uses OAuth's format.

Not yet checked: 408 and 415 codes, which we have not seen in the standard.

### 5.6 Reset

`POST /admin/reset` gives you fresh data, starting settings, no faults and the
real clock. It also forgets all consents and tokens. Call it at the start of
every test.

## 6. Admin controls

These are ours, not part of the standard. **They have no password. Never put
this mock on a public server.**

| Call | What it does |
|---|---|
| `GET /admin/settings` | Show the rules with their trust labels |
| `PUT /admin/settings` | Change rules |
| `GET /admin/faults` | List faults that are on |
| `POST /admin/faults` | Switch a fault on |
| `DELETE /admin/faults/{id}` | Switch one off |
| `DELETE /admin/faults` | Switch all off |
| `POST /admin/time/advance` | Move the clock forward |
| `GET /admin/data` | See customers (with fake sign-ins), accounts, IBANs, balances |
| `POST /admin/reset` | Start over |

## 7. Look and feel

The approval pages use the project's brand colours: bubblegum pink `#FD6AD2`,
galactic violet `#553EFE`, sky blue `#7CC4F5` and marshmallow white `#F0F0F0`,
on a soft pink-and-violet glow. They carry a "Test bank" badge and a line that
nothing is real. All colours live in one file, `app/brand.py`, so changing the
look means editing that file only.

## 8. What comes next

| Step | Calls |
|---|---|
| 4 Accounts | List accounts, one account, balances, transactions (booked, pending or both), one transaction. They will use the access check from 4.6 and refuse the 5th read of the day. |
| 5 Payments | Start a domestic payment, read it, check its status, cancel it, approve it. |
| 6 Check | Compare against the standard and run a whole flow from consent to payment. |

## 9. Where things are

| File | What it does |
|---|---|
| `app/main.py` | Starts the app and connects the pieces. Also `/health`. |
| `app/routers/consents.py` | The consent calls (working) |
| `app/routers/bank_pages.py` | The sign-in and approval pages (working) |
| `app/routers/oauth.py` | The token step (working) |
| `app/routers/admin.py` | The admin controls (working) |
| `app/routers/accounts.py` | Account calls (placeholder) |
| `app/routers/payments.py` | Payment calls (placeholder) |
| `app/consent_rules.py` | A consent's life: expiry, approving, rejecting |
| `app/auth.py` | The access check: token plus consent |
| `app/brand.py` | Colours and layout of the pages |
| `app/formdata.py` | Reads web forms (so we need no extra package) |
| `app/models.py` | The shapes of the data: customer, account, transaction, consent, token, payment |
| `app/store.py` | The bank's memory, reset, balance calculation |
| `app/seed.py` | Creates the sample data |
| `app/settings.py` | The bank's rules and their trust labels |
| `app/faults.py` | Makes the bank misbehave on purpose |
| `app/clock.py` | The bank's clock, which can be moved |
| `app/errors.py` | The standard error format and allowed codes |
| `tests/` | Automated tests (`helpers.py` plays the app and the customer) |
| `settings.example.json` | Starting rules to copy and edit |
| `requirements.txt` | Packages, if you do not use `uv` |

## 10. Not planned

Foreign, bulk and recurring payments, card accounts, real certificates and
request signing, and any screen beyond the approval pages.
