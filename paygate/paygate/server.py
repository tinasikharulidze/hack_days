"""The MCP server: the tools an agent sees.

HOW TO READ THIS FILE. Each function below with @server.tool() is one tool.
The agent never sees our code. It sees, for each tool:
   - the NAME (the function name),
   - the DESCRIPTION (the docstring),
   - the INPUTS (the parameters, with their descriptions).
That is all the agent has to decide which tool to call and how.

Run it (the mock bank must be running):

    uv run python -m paygate.server          # listens on http://127.0.0.1:8001/mcp

Two things live on that one address:
   /mcp        the MCP endpoint the agent connects to
   /callback   the page the bank sends the customer's browser back to
"""

import functools
import json
from datetime import UTC, datetime
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse

from paygate import config as config_module
from paygate.bank import BankClient, BankProblem
from paygate.config import Config
from paygate.gate import PayGate, explain_problem
from paygate.state import State

INSTRUCTIONS = """\
Tools for reading a customer's Georgian bank accounts and paying from them.

Rules to follow:
1. Call connect_bank first. The customer must approve on their bank's own page.
2. Every payment is approved by the customer on the bank's page. You can never move money alone.
3. Give each payment a unique intent_id and REUSE THE SAME intent_id when you retry that payment.
4. If a tool says the outcome is unknown, never start a new payment. Check payment_status instead.
5. Reads are limited by the bank (a few per day). Do not call read tools in a loop.
6. Always read "next_step" in an answer: it says what to do.
"""


def build_server(gate: PayGate, bank: BankClient, config: Config) -> MCPServer:
    server = MCPServer("paygate", instructions=INSTRUCTIONS)

    def audited(function):
        """Write one line to the audit file for every tool call: what was asked and what was answered."""

        @functools.wraps(function)
        async def wrapper(*args, **kwargs):
            result = await function(*args, **kwargs)
            with open(config.audit_file, "a", encoding="utf-8") as file:
                file.write(
                    json.dumps(
                        {
                            "time": datetime.now(UTC).isoformat(),
                            "tool": function.__name__,
                            "arguments": kwargs,
                            "status": result.get("status"),
                        }
                    )
                    + "\n"
                )
            return result

        return wrapper

    # ---------------------------------------------------------------- connection
    @server.tool()
    @audited
    async def connect_bank(
        ibans: Annotated[
            list[str],
            Field(
                description="IBANs of the customer's accounts you want access to, "
                "for example ['GE00XX0000000000000000']. Ask the customer for them."
            ),
        ],
    ) -> dict:
        """Ask the customer for permission to read their bank accounts and to pay from them.

        Call this first. It is safe to call again: if the connection is already active it says
        'connected', and if the customer has not approved yet it returns the same link.

        The answer's status is one of:
          waiting_for_customer  show customer_link to the customer; they sign in and approve on the bank's page.
                                Afterwards call connect_bank again with the same ibans.
          connected             you can now use the other tools.
          refused               the input was wrong; read message.
        """
        return await gate.connect(ibans)

    # ---------------------------------------------------------------- reading
    @server.tool()
    @audited
    async def list_accounts() -> dict:
        """List the bank accounts the customer shared with you: account_id, IBAN, name, currency.

        Use the account_id from here in get_balances and get_transactions, and the IBAN as from_iban
        in make_payment. The customer may have shared fewer accounts than you asked for.
        The bank allows only a few reads per day, so call this once and remember the answer.
        """
        try:
            body = await bank.get_json("/v1/accounts")
        except BankProblem as problem:
            return explain_problem(problem)
        return {
            "status": "ok",
            "accounts": [
                {
                    "account_id": a["resourceId"],
                    "iban": a["iban"],
                    "name": a.get("name"),
                    "currency": a["currency"],
                    "type": a.get("product"),
                }
                for a in body["accounts"]
            ],
        }

    @server.tool()
    @audited
    async def get_balances(
        account_id: Annotated[
            str, Field(description="An account_id from list_accounts.")
        ],
    ) -> dict:
        """Get the current balances of one account. 'interimAvailable' is the money the customer can spend now.

        Counts as one of the bank's limited daily reads.
        """
        try:
            body = await bank.get_json(f"/v1/accounts/{account_id}/balances")
        except BankProblem as problem:
            return explain_problem(problem)
        return {
            "status": "ok",
            "balances": [
                {
                    "type": b["balanceType"],
                    "amount": b["balanceAmount"]["amount"],
                    "currency": b["balanceAmount"]["currency"],
                }
                for b in body["balances"]
            ],
        }

    @server.tool()
    @audited
    async def get_transactions(
        account_id: Annotated[
            str, Field(description="An account_id from list_accounts.")
        ],
        date_from: Annotated[
            str,
            Field(
                description="First day to include, YYYY-MM-DD. Required by the bank."
            ),
        ],
        date_to: Annotated[
            str,
            Field(
                description="Last day to include, YYYY-MM-DD. Leave empty for today."
            ),
        ] = "",
        booking_status: Annotated[
            str,
            Field(
                description="'booked' (settled), 'pending' (not settled yet) or 'both'."
            ),
        ] = "booked",
    ) -> dict:
        """Get the transactions of one account, newest information the bank has. Negative amounts are money out.

        At most 50 transactions are returned; if there are more, 'truncated' is true, so narrow the dates.
        Counts as one of the bank's limited daily reads.
        """
        params = {"bookingStatus": booking_status, "dateFrom": date_from}
        if date_to:
            params["dateTo"] = date_to
        try:
            body = await bank.get_json(
                f"/v1/accounts/{account_id}/transactions", params
            )
        except BankProblem as problem:
            return explain_problem(problem)
        rows = []
        for kind in ("booked", "pending"):
            for t in body["transactions"].get(kind, []):
                other = t.get("creditorName") or t.get("debtorName")
                rows.append(
                    {
                        "status": kind,
                        "date": t.get("bookingDate") or t["valueDate"],
                        "amount": t["transactionAmount"]["amount"],
                        "currency": t["transactionAmount"]["currency"],
                        "other_party": other,
                        "reference": t.get("remittanceInformationUnstructured", ""),
                    }
                )
        limit = config.max_transactions
        return {
            "status": "ok",
            "transactions": rows[:limit],
            "truncated": len(rows) > limit,
        }

    # ---------------------------------------------------------------- paying
    @server.tool()
    @audited
    async def make_payment(
        intent_id: Annotated[
            str,
            Field(
                description="A unique id YOU choose for this one payment, such as "
                "'order-1234'. When you retry this same payment, use the SAME intent_id. "
                "Never reuse an id for a different payment."
            ),
        ],
        from_iban: Annotated[
            str,
            Field(
                description="The customer's paying account IBAN (from list_accounts)."
            ),
        ],
        to_iban: Annotated[str, Field(description="The receiver's Georgian IBAN.")],
        to_name: Annotated[str, Field(description="The receiver's name.")],
        amount: Annotated[
            str,
            Field(
                description="Amount as text with up to two decimals, for example '25.50'."
            ),
        ],
        reference: Annotated[
            str,
            Field(
                description="What the payment is for, up to 35 characters, "
                "for example 'Order 1234'."
            ),
        ],
        currency: Annotated[str, Field(description="Only 'GEL' is supported.")] = "GEL",
    ) -> dict:
        """Start a payment. The customer must still approve it on their bank's page.

        This does NOT move money by itself. The answer's status is one of:
          awaiting_customer  show customer_link to the customer. After they approve, check payment_status.
          unknown            we got no answer from the bank. Do NOT start a new payment and do NOT tell the
                             customer it failed. Call make_payment again with the SAME intent_id, or check
                             payment_status.
          completed / pending / rejected / cancelled   the payment already exists in that state.
          refused            not sent: the input was wrong, or it breaks a limit. Read message.

        Safe to repeat: calling again with the same intent_id never creates a second payment.
        A payment that matches an existing one (same accounts, amount and reference) is treated as the same payment.
        """
        return await gate.make_payment(
            intent_id, from_iban, to_iban, to_name, amount, reference, currency
        )

    @server.tool()
    @audited
    async def payment_status(
        intent_id: Annotated[
            str, Field(description="The intent_id you used in make_payment.")
        ],
    ) -> dict:
        """Check what happened to a payment. Use this after the customer says they approved, and whenever unsure.

        Status values: awaiting_customer, completed (paid), pending (approved, processing), rejected,
        cancelled, unknown. If the bank cannot be reached the answer is 'unknown' with last_known_status:
        in that case never create a new payment, just check again later.
        """
        return await gate.payment_status(intent_id)

    @server.tool()
    @audited
    async def cancel_payment(
        intent_id: Annotated[
            str, Field(description="The intent_id you used in make_payment.")
        ],
    ) -> dict:
        """Cancel a payment the customer has not approved yet. Once approved it can no longer be cancelled."""
        return await gate.cancel_payment(intent_id)

    # ---------------------------------------------------------------- the customer's return trip
    @server.custom_route("/callback", ["GET"])
    async def callback(request: Request) -> HTMLResponse:
        """The bank sends the customer's browser here after they approve (or refuse). Not an agent tool."""
        sentence = await gate.handle_callback(dict(request.query_params))
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><title>Done</title>"
            f"<body style='font-family:sans-serif;max-width:30em;margin:4em auto'>"
            f"<h2>{sentence}</h2></body>"
        )

    return server


def main() -> None:
    config = config_module.load()
    state = State(config.state_file)
    bank = BankClient(config, state)
    server = build_server(PayGate(config, state, bank), bank, config)
    server.run("streamable-http", host="127.0.0.1", port=8001)


if __name__ == "__main__":
    main()
