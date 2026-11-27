# Payment agents for Georgian open banking

A project from Hack Days Georgia 2026 (track: Agentic Infrastructure).

**The problem.** An AI agent that can pay is dangerous in one particular way: it sends a payment, the answer
gets lost, it thinks the payment failed, and it sends the payment again. The customer pays twice.

**What this project is.** The infrastructure that makes this safe and easy to build:

```
 You (browser)
     |
 Chat page + agent (Gemini)        <- a small example agent, so you can try everything
     |   MCP (the standard way to give an agent tools)
 paygate                           <- 7 tools + a payment gate that stops duplicates and mistakes
     |   HTTP
 Mock bank                         <- a practice bank following the Georgian open banking standard
```

You can run all of it on your own computer. No real bank, no real money.

## What is in this folder

| Folder or file | What it is | Read more |
|---|---|---|
| `mockbank/` | A practice bank (Georgian open banking, Berlin Group NextGenPSD2). It can also misbehave on purpose. | `mockbank/README.md` |
| `paygate/` | The part developers would use: the MCP tools and the payment gate. Also the example agent and chat page. | `paygate/README.md` |
| `run_all.py` | Starts everything with one command | below |
| `pyproject.toml` | The list of Python packages the project needs | |

## 1. What you need

- A computer with Windows, macOS or Linux, and an internet connection.
- **uv**, a tool that installs Python and the packages for you. You do not need to install Python yourself.
  - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
  - macOS or Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Close and reopen the terminal afterwards, then check it works: `uv --version`
- A **Gemini API key** (free to create) from Google AI Studio: https://aistudio.google.com/apikey

## 2. Get the project and install it (once)

Open a terminal in the folder that contains this README, then:

```
uv sync
```

This creates a private Python environment in `.venv` and installs everything. It may take a minute.

## 3. Put your Gemini key in a `.env` file (once)

Create a file called `.env` in this folder, with exactly one line (no quotes, no spaces around `=`):

```
GEMINI_API_KEY=your-key-here
```

The easiest way is from the terminal.
Windows PowerShell: `Set-Content -Path .env -Value "GEMINI_API_KEY=your-key-here"`
macOS or Linux: `echo "GEMINI_API_KEY=your-key-here" > .env`

`.env` is already ignored by git, so the key will not be uploaded. Never paste the key into chat, an email or a file that is not `.env`.
Optional extra lines in the same file:
- `GEMINI_MODEL=model-name` to use a different Gemini model (default: `gemini-2.5-flash`).
- `LANGUAGE=en` for English. The default is Georgian (`LANGUAGE=ka`): the chat page is in Georgian and the agent answers in Georgian.

## 4. Start everything

```
uv run python run_all.py
```

After a few seconds your browser opens the **chat page** (http://127.0.0.1:8002). The terminal shows:

```
  started bank     http://127.0.0.1:8000
  started paygate  http://127.0.0.1:8001
  started chat     http://127.0.0.1:8002
```

Leave that terminal open. Press **Ctrl+C** in it to stop everything. Each start gives you an empty, fresh bank.
The programs' messages are in the `logs/` folder.

## 5. Your first conversation

The chat page has suggestion buttons under the message box. Click one to fill the box, then press Send.
Do them in this order:

1. **"Connect to my bank..."** The agent answers with a pink button, "Approve access at the bank". This is the
   customer's part, and the agent can never do it for you. Click the button. The bank's page opens in the
   **bank frame** (upper right; it stays open, no new windows).
   Sign in with username `nino`, password `nino`, tick the accounts, press Approve.
   The panel says "Permission given". Carry on in the chat.
2. **"I approved. Show my accounts and the balance..."** The agent reads the account and tells you the balance.
3. **"Pay 25.50 GEL to Example Shop..."** The agent starts a payment and shows another pink button. Click it,
   sign in again, approve. Back in the chat, press **"Did it go through?"**.
4. **"The shop says Order 1 never arrived, pay it again."** The agent must NOT pay twice. The gate returns the
   first payment and says nothing new was sent.
5. **"Pay 300 GEL..."** Refused: more than the 200 GEL limit for one payment.

The page has two halves. The **left half** is the chat. The **right half** has the bank's online banking at the top
(every approval opens there; it stays open) and the tools for you as a developer below it:

- **Payments by intent_id**: what the agent's tool says about each payment, and how far it got.
- **Make the bank misbehave**: buttons for faults. "Lose the next payment answer" makes the bank take the payment
  and then answer too late. This is how duplicates are born, so try it and then press "The shop says Order 1 never arrived...".
- **Timeline**: for each message you sent, every model call and tool call with its time. The colour is the gate's
  verdict (green fine, amber unsure, red refused), and a black label names the rule that fired. Click an entry for the exact answer.
The page and the agent speak Georgian by default (set `LANGUAGE=en` for English). You can write in either language.
The bank's own sign-in and approval pages, and the raw tool answers in the timeline, are still in English.

## 6. Make the bank misbehave (the important test)

This is the failure the project is about: the bank does the work, but the answer is lost.
Open a second terminal in this folder and run (it breaks the bank's next payment answer, once):

Windows PowerShell:
```
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/admin/faults -ContentType application/json -Body '{"kind":"server_error","operation":"payments.initiate","phase":"after","status_code":503,"times":1}'
```
macOS or Linux:
```
curl -X POST http://127.0.0.1:8000/admin/faults -H "Content-Type: application/json" -d '{"kind":"server_error","operation":"payments.initiate","phase":"after","status_code":503,"times":1}'
```

Then in the chat: `Pay 30.00 GEL to Example Shop, IBAN GE98MB0000111122223333, reference Order 3.`

What should happen: the agent is told "outcome unknown" (never "failed"), retries the same payment calmly,
and gives you ONE button. Approve it. Then check what the bank saw:

- Windows PowerShell: `Invoke-RestMethod http://127.0.0.1:8000/admin/payments`
- macOS or Linux: `curl http://127.0.0.1:8000/admin/payments`

Exactly one payment is completed (`ACSC`). The other may sit unapproved, and the bank drops it after 15 minutes.
The bank can do other things on purpose too: slow answers, a payment stuck in "pending", an expiring permission.
See section 7.3 of `mockbank/README.md`.

## 7. See what happened afterwards

| Where | What it shows |
|---|---|
| The right half of the chat page (Timeline) | Every tool call, its time and the answer |
| `paygate/agent_transcript.jsonl` | The whole conversation and every tool call, one line each |
| `paygate/paygate_audit.jsonl` | Every tool call as paygate saw it |
| http://127.0.0.1:8000/admin/payments | What the bank itself recorded, with a list of duplicate payments |
| http://127.0.0.1:8000/admin/data | Sample customers, accounts, sign-in details (Nino Kezherashvili: `nino` / `nino`, Luka Darsalia: `luka` / `luka`) |
| `logs/` | What each of the three programs printed |

## 8. Run the tests

From this folder (each takes a few seconds):

```
cd mockbank
uv run pytest
cd ../paygate
uv run pytest
```

The tests need no Gemini key. They play the agent, the customer and the bank all at once, including lost answers.

## 9. Starting the parts by hand (for debugging)

Use three terminals, each starting in this folder:

```
cd mockbank
uv run uvicorn app.main:create_app --factory --port 8000
```
```
cd paygate
uv run python -m paygate.server
```
```
cd paygate
uv run --env-file .env python examples/chat_ui.py
```

If you restart the bank, delete `paygate/paygate_state.json` before trying again (the bank forgets everything on
restart, but paygate would still remember the old connection). `run_all.py` does this for you.

## 10. Using another agent instead of our example

paygate speaks MCP, so any agent that can use MCP tools works. The address is `http://127.0.0.1:8001/mcp`.
For Claude Code, once installed: `claude mcp add --transport http paygate http://127.0.0.1:8001/mcp`.
To build your own agent, copy `paygate/examples/gemini_agent.py`: it is about 100 lines and explains itself.

## 11. When something goes wrong

| What you see | What to do |
|---|---|
| `uv` is not recognised | Close and reopen the terminal after installing it. |
| `Failed to hardlink files... os error 396` (Windows) | Run `$env:UV_LINK_MODE = "copy"` and repeat the command. Once and for all: `setx UV_LINK_MODE copy`. Folders synced by OneDrive can cause this. |
| `No GEMINI_API_KEY found` | The `.env` file is missing, has the wrong name (check it is not `.env.txt`), or is in another folder. It belongs next to `run_all.py`. On Windows, `dir -Force` shows it. |
| `Port 8000 (or 8001, 8002) is already in use` | The project is already running in another window, or a leftover program is running. Close it, then try again. |
| The chat says "Gemini call failed" | Read the message. Common causes: wrong key, quota used up, or a model name your key cannot use. Set `GEMINI_MODEL` in `.env`. |
| The agent says it is "not connected" | The permission was never approved, or the bank was restarted. Start again from step 1 of section 5. |
| `rate_limited` | The mock bank allows only 4 reads (accounts, balances, transactions) per connection per day. Restart with `run_all.py` for a fresh bank. |
| The chat page does not open | Open http://127.0.0.1:8002 yourself. If it stays blank, read `logs/chat.log`. |
| Anything else | Read the files in `logs/`. They show what each program said. |

## 12. What it does not do yet (honest list)

- It works with our mock bank only. A real bank needs an adapter and the bank's own login; the token step in the mock is our own design, because the standard leaves it open.
- One customer at a time. Only domestic payments in GEL.
- Georgian covers the chat page and the agent's answers. The bank's pages, the tool messages inside paygate, and these READMEs are in English; the agent translates the tool messages for the customer. The Georgian wording should be reviewed by a native speaker.
- The mock's rule values (consent length, read limits, approval timeout) still need to be checked against the official manual.

## 13. Files created while you use it

`.env` (your key), `logs/`, `paygate/paygate_state.json`, `paygate/paygate_audit.jsonl`, `paygate/agent_transcript.jsonl`.
None of them should be committed. If git shows them, add these lines to `.gitignore`:

```
logs/
paygate/paygate_state.json
paygate/paygate_audit.jsonl
paygate/agent_transcript.jsonl
```
