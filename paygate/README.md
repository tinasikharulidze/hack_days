# paygate

The layer between an AI agent and a bank: **MCP tools** an agent can call, and a **payment gate** that
stops the agent's mistakes (above all, paying twice).

```
Agent (Claude)  <--MCP-->  paygate  <--HTTP-->  a bank (today: our mock, ../mockbank)
```

The agent never sees tokens, consent ids or bank error codes. It sees seven simple tools, and every
answer says what to do next.

**Status:** works against the mock bank. Not yet adapted to a real bank (see 7).

## 1. For a developer: how to use it

1. Have a bank to talk to. Today that is the mock: from the repository root,
   `uv run uvicorn app.main:create_app --factory --port 8000` (run inside `mockbank/`).
2. Install the one extra package: `uv add "mcp>=2"` (from the repository root).
3. Copy `paygate_settings.example.json` to `paygate_settings.json` if you want to change anything
   (bank address, payment limits). Every setting has a default, so this is optional.
4. Start the server (from this folder): `uv run python -m paygate.server`
   It listens on `http://127.0.0.1:8001/mcp`.
5. Connect your agent. For Claude Code:
   `claude mcp add --transport http paygate http://127.0.0.1:8001/mcp`

That is all. You write no code against the bank. The agent gets the tools and the rules.

The same address also serves `/callback`, the page the bank sends the customer's browser back to
after they approve something.

### Try it with a chat agent (Gemini)

`examples/gemini_agent.py` is a small agent (a loop around Gemini) that uses the tools. It comes in two forms:

- **Chat page** (easiest): `uv run --env-file .env python examples/chat_ui.py` opens http://127.0.0.1:8002 with the
  the chat on the whole left side, and on the right the mock bank's own pages (one frame that stays open) with
  **developer tools** below it: payments by `intent_id`, buttons that make the bank misbehave (lose the next answer, slow, down, stuck pending),
  and a timeline per customer message (every model call and tool call with its time; the gate's verdict in
  colour and the rule that fired, for example "duplicate under another intent_id: blocked").
  Links for the customer appear as buttons that load the bank frame. The right half asks the chat server, which
  passes on to the bank's admin API (`BANK_URL`, default `http://127.0.0.1:8000`).
  The repository's `run_all.py` starts the bank, paygate and this page together.
- **Terminal**: `uv run --env-file .env python examples/gemini_agent.py`

Both need `GEMINI_API_KEY` in a `.env` file (or in the environment). The page and the agent use Georgian by default; set `LANGUAGE=en` for English. Set `GEMINI_MODEL` to use another model (default
`gemini-2.5-flash`). Any model that can call tools works the same way, because MCP does not belong to one model maker.
The agent is only a loop: read the customer, let the model ask for tools, run them on the server, give the results back
(see the comments in the file). Everything is written to `agent_transcript.jsonl`.

The full step-by-step guide for a newcomer is the README in the repository root.

## 2. The seven tools

| Tool | What it does |
|---|---|
| `connect_bank` | Asks the customer for permission. Returns a link for the customer. Safe to repeat. |
| `list_accounts` | The accounts the customer shared. |
| `get_balances` | Balances of one account. |
| `get_transactions` | Transactions of one account (cut at 50; the bank requires `date_from`). |
| `make_payment` | Starts a payment. The customer still approves on the bank's page. |
| `payment_status` | What happened to a payment. |
| `cancel_payment` | Cancels a payment the customer has not approved yet. |

The agent sees only each tool's name, its docstring and its inputs. So the docstrings in
`paygate/server.py` are written to the agent: when to use the tool, and what each `status` means.

## 3. The gate's rules (`paygate/gate.py`)

1. One payment per **intent**. The agent picks an `intent_id`; the same id again returns the first payment, never a second.
2. The same accounts, amount and reference under a different `intent_id` is the same payment (catches an agent that invents a new id on each retry).
3. Limits: 200 GEL per payment and 500 GEL per day by default.
4. No answer from the bank is **unknown**, never "failed". Retrying with the same `intent_id` is safe: the same `X-Request-ID` is sent, and the customer only ever receives one link to approve.
5. The customer approves every payment on the bank's page. The agent can never move money alone.
6. Every tool call is one line in `paygate_audit.jsonl`.

Records are kept in `paygate_state.json`, so a restart forgets nothing.

## 4. A payment, step by step

1. Agent calls `make_payment` with an `intent_id`. The gate checks input, connection, duplicates and limits.
2. The gate writes a record (state `starting`), then calls the bank.
3. The bank answers: the record becomes `awaiting_customer`, and the agent gets `customer_link`.
4. The customer opens the link, signs in, approves. The bank sends them to `/callback`, which asks the bank for the status.
5. The agent calls `payment_status`: `completed`.

If step 3 loses its answer, the record stays `starting` and the agent is told "unknown, call again with the same `intent_id`".
If the status check fails later, the agent is told "do NOT create a new payment", and if it does, the gate sends nothing new.

Payment states: `starting` (no answer yet), `awaiting_customer`, `completed`, `pending` (approved, the bank is
still processing), `rejected` (refused, declined or timed out), `cancelled`.

## 5. Settings (`paygate_settings.json`)

| Setting | Default | Meaning |
|---|---|---|
| `bank_url` | `http://127.0.0.1:8000` | The bank to talk to |
| `callback_url` | `http://127.0.0.1:8001/callback` | Where the bank sends the customer back |
| `max_payment_gel` | `200.00` | Largest single payment |
| `max_per_day_gel` | `500.00` | All payments started in one day (UTC) |
| `duplicate_window_minutes` | `60` | An identical payment inside this time is the same payment |
| `bank_timeout_seconds` | `10` | No answer after this long means "unknown" |
| `consent_days` | `30` | How long we ask to read the accounts |
| `reads_per_day` | `4` | The reads per day we ask for (the mock's default maximum) |
| `max_transactions` | `50` | Most transactions in one answer |
| `state_file`, `audit_file` | `paygate_state.json`, `paygate_audit.jsonl` | Where the layer keeps its memory and its log |

## 6. Good to know

- The mock allows only 4 reads a day per connection by default. An agent reaches that quickly; the layer says `rate_limited` and tells it not to retry. Raise the limit in the mock's settings for experiments.
- The customer's IBANs are asked for in `connect_bank`: the agent must ask the customer for them.
- Tokens are hidden. An expired access token is renewed silently; an expired consent becomes `not_connected` with the advice to call `connect_bank` again.

## 7. What it does not do yet

- **A real bank.** The token step (`/oauth/token`) is our own design, because the standard leaves it to each bank. A real bank has its own login, plus certificates and signatures we skip. The plan is an adapter between paygate and the bank, so only the adapter changes per bank. Today `paygate/bank.py` plays that role for the mock.
- Only domestic GEL payments, one customer at a time, and no scorecard yet.

## 8. Tests

`uv run pytest` (from this folder). 39 tests play three parts at once: the agent (a real MCP client), the
customer (signing in on the bank's pages) and the bank (the mock, in the same process, with its faults:
lost answers, errors, expiry). The tests find the mock in `../mockbank`; set `MOCKBANK_PATH` to use another place.

## 9. Where things are

| File | What it does |
|---|---|
| `paygate/server.py` | The MCP server: the seven tools and the `/callback` page |
| `paygate/gate.py` | The payment gate: the rules and the payment records |
| `paygate/bank.py` | Talks to the bank: tokens, renewing, error kinds |
| `paygate/state.py` | The memory file (connection and payments) |
| `paygate/config.py` | Settings and their defaults |
| `paygate_settings.example.json` | Starting settings to copy and edit |
| `examples/gemini_agent.py` | The example agent: the loop around Gemini (also runs in the terminal) |
| `examples/chat_ui.py`, `examples/chat_ui.html` | The chat web page for that agent |
| `tests/test_paygate.py` | The tests |
| `tests/test_example_agent.py` | Tests the agent's loop with a pretend model (no key needed) |
| `tests/conftest.py` | Finds the mock bank and gives each test a clean one |
