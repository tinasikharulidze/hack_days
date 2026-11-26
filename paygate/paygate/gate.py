"""The payment gate: the rules that stand between the agent and the bank.

The agent can be wrong, impatient or forgetful. It can send the same payment
twice because an answer got lost. The gate does not trust it. Every rule here
is plain code that the agent cannot argue with.

THE IDEA: every payment belongs to an "intent" (one thing the agent wants to
pay, such as "pay for order 1234"). The gate keeps one record per intent, in the
state file. Before it ever sends anything to the bank, it looks at that ledger:

  1. Same intent_id again          -> never a second payment. The gate returns the first one.
  2. Same money, same payee, same reference, a different intent_id (an agent that
     invents a new id on each retry) -> treated as the same payment too.
  3. Too big, or too much for one day -> refused before the bank hears about it.
  4. No answer from the bank when starting a payment -> the gate says "outcome
     unknown", never "failed". Trying again with the same intent_id is safe,
     because the same X-Request-ID goes out and the customer only ever gets one
     link to approve.
  5. The customer approves every payment on the bank's own page, so the agent can
     never move money alone.

Payment states in a record (what we tell the agent in "status"):

  starting            we sent the request, got no answer. The bank may or may not have it.
  awaiting_customer   the bank has it; the customer has not approved yet
  completed           the customer approved and the money moved (bank status ACSC)
  pending             approved but the bank is still processing (bank status PDNG)
  rejected            the bank refused it, the customer said no, or the 15 minutes ran out
  cancelled           cancelled before the customer approved
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from paygate.bank import BankClient, BankProblem
from paygate.config import Config
from paygate.state import State

_IBAN = re.compile(r"^GE\d{2}[A-Z]{2}\d{16}$")
_BANK_STATUS = {
    "RCVD": "awaiting_customer",
    "ACSC": "completed",
    "PDNG": "pending",
    "RJCT": "rejected",
    "CANC": "cancelled",
}
_DEAD = {
    "rejected",
    "cancelled",
}  # these never moved money and do not count against limits
_NAMESPACE = uuid.UUID(
    "6f1c1c4e-2a53-4f0e-9a43-0c2f7f6a9d11"
)  # fixed, so one intent always gives one X-Request-ID


def explain_problem(problem: BankProblem) -> dict:
    """What to tell an agent about a bank problem, in words it can act on."""
    if problem.kind == "unreachable":
        return {
            "status": "unknown",
            "message": "The bank did not answer properly (timeout or server error).",
            "next_step": "Wait a little and try the same call again. Do not tell the customer it failed.",
        }
    if problem.kind == "rate_limited":
        return {
            "status": "rate_limited",
            "message": "The bank allows only a few reads per day on this connection and today's are used up.",
            "next_step": "Do not retry now. Tell the customer, and try again tomorrow.",
        }
    if problem.kind == "connection":
        return {
            "status": "not_connected",
            "message": "The customer's permission to access the bank is missing, expired or withdrawn.",
            "next_step": "Call connect_bank to ask the customer for permission again.",
        }
    return {
        "status": "refused",
        "message": f"The bank refused the request: {problem.text or problem.code}",
        "next_step": "Check the inputs. Repeating the same request will give the same answer.",
    }


class PayGate:
    def __init__(self, config: Config, state: State, bank: BankClient, now=None):
        self.config = config
        self.state = state
        self.bank = bank
        self.now = now or (lambda: datetime.now(UTC))  # tests can pass a fake clock

    # =====================================================================
    # The connection (permission to read accounts)
    # =====================================================================
    async def connect(self, ibans: list[str]) -> dict:
        """Start (or report on) the customer's permission. Safe to call again and again."""
        ibans = [i.replace(" ", "").upper() for i in ibans]
        if not ibans or any(not _IBAN.match(i) for i in ibans):
            return {
                "status": "refused",
                "message": "Give the customer's account numbers as Georgian IBANs, "
                "for example GE00XX0000000000000000.",
                "next_step": "Ask the customer for their IBANs.",
            }
        connection = self.state.connection
        if connection.get("status") == "connected":
            return {
                "status": "connected",
                "accounts_requested": connection["ibans"],
                "message": "The bank connection is active.",
                "next_step": "Use list_accounts to see which accounts the customer actually shared.",
            }
        if connection.get("status") == "pending" and connection.get("ibans") == ibans:
            return self._waiting(connection)
        try:
            created = await self.bank.create_consent(ibans)
        except BankProblem as problem:
            return explain_problem(problem)
        self.state.connection = {
            "status": "pending",
            "ibans": ibans,
            **created,
            "created_at": self.now().isoformat(),
        }
        self.state.save()
        return self._waiting(self.state.connection)

    @staticmethod
    def _waiting(connection: dict) -> dict:
        return {
            "status": "waiting_for_customer",
            "customer_link": connection["link"],
            "message": "The customer must open this link, sign in to their bank, and approve.",
            "next_step": "Show the link to the customer. After they approve, call connect_bank again "
            "with the same ibans: it will say 'connected'.",
        }

    # =====================================================================
    # The bank sends the customer back here (after consent or payment)
    # =====================================================================
    async def handle_callback(self, params: dict) -> str:
        """Called when the customer's browser returns from the bank. Returns a sentence for the web page."""
        consent_id = params.get("consentId")
        connection = self.state.connection
        if consent_id:  # back from the permission page
            if connection.get("consent_id") != consent_id:
                return "This permission request is not known here."
            if params.get("error"):
                self.state.connection = {"status": "denied"}
                self.state.save()
                return "You did not give permission. Nothing was shared."
            try:
                tokens = await self.bank.exchange_code(params.get("code", ""))
            except BankProblem:
                return "The bank could not confirm your permission. Please ask for it again in the chat."
            self.state.connection = {
                **connection,
                "status": "connected",
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
            }
            self.state.save()
            return "Permission given. You can go back to the chat."
        bank_id = params.get("paymentId")
        record = next(
            (
                r
                for r in self.state.payments.values()
                if r.get("bank_payment_id") == bank_id
            ),
            None,
        )
        if record is None:
            return "You can go back to the chat."
        await self._refresh(record)  # the redirect is only a nudge; ask the bank
        words = {
            "completed": "Payment approved and sent.",
            "rejected": "The payment did not go through.",
            "pending": "Payment approved. The bank is still processing it.",
        }
        return words.get(record["state"], "You can go back to the chat.")

    # =====================================================================
    # Payments
    # =====================================================================
    async def make_payment(
        self,
        intent_id: str,
        from_iban: str,
        to_iban: str,
        to_name: str,
        amount: str,
        reference: str,
        currency: str = "GEL",
    ) -> dict:
        problem = self._check_input(
            intent_id, from_iban, to_iban, to_name, amount, reference, currency
        )
        if problem:
            return {
                "status": "refused",
                "message": problem,
                "next_step": "Fix the input and call again.",
            }
        from_iban, to_iban = (
            from_iban.replace(" ", "").upper(),
            to_iban.replace(" ", "").upper(),
        )
        amount = f"{Decimal(amount):.2f}"
        if self.state.connection.get("status") != "connected":
            return explain_problem(BankProblem("connection"))
        if from_iban not in self.state.connection["ibans"]:
            return {
                "status": "refused",
                "message": "The customer has not given access to that paying account.",
                "next_step": "Use one of the accounts from list_accounts.",
            }

        fingerprint = "|".join([from_iban, to_iban, amount, currency, reference])
        record = self.state.payments.get(intent_id)

        # Rule 1: this intent already exists. Never a second payment.
        if record is not None:
            if record["fingerprint"] != fingerprint:
                return {
                    "status": "refused",
                    "intent_id": intent_id,
                    "message": "This intent_id was already used for a different payment.",
                    "next_step": "Use a new intent_id for a different payment.",
                }
            if (
                record["state"] == "starting"
            ):  # the first try got no answer: try again, same request id
                return await self._send(record)
            await self._refresh(record)
            return self._answer(
                record,
                "A payment for this intent_id already exists. Nothing new was sent.",
            )

        # Rule 2: same payment under another intent_id (an agent that makes up a new id on each retry).
        window = self.now() - timedelta(minutes=self.config.duplicate_window_minutes)
        for other in self.state.payments.values():
            if (
                other["fingerprint"] == fingerprint
                and other["state"] not in _DEAD
                and datetime.fromisoformat(other["created_at"]) >= window
            ):
                await self._refresh(other)
                if other["state"] not in _DEAD:
                    return self._answer(
                        other,
                        f"The same payment already exists under intent_id "
                        f"'{other['intent_id']}'. Nothing new was sent. If you really want a "
                        f"second identical payment, change the reference.",
                    )

        # Rule 3: limits.
        if Decimal(amount) > Decimal(self.config.max_payment_gel):
            return {
                "status": "refused",
                "message": f"A single payment may not exceed "
                f"{self.config.max_payment_gel} GEL.",
                "next_step": "Ask the customer or use a smaller amount.",
            }
        if self._today_total() + Decimal(amount) > Decimal(self.config.max_per_day_gel):
            return {
                "status": "refused",
                "message": f"This would take today's payments over the daily limit of "
                f"{self.config.max_per_day_gel} GEL.",
                "next_step": "Try again tomorrow or ask the customer.",
            }

        # All clear: write the record FIRST, then talk to the bank.
        record = {
            "intent_id": intent_id,
            "fingerprint": fingerprint,
            "from_iban": from_iban,
            "to_iban": to_iban,
            "to_name": to_name,
            "amount": amount,
            "currency": currency,
            "reference": reference,
            "state": "starting",
            "bank_payment_id": None,
            "link": None,
            "created_at": self.now().isoformat(),
        }
        self.state.payments[intent_id] = record
        self.state.save()
        return await self._send(record)

    async def payment_status(self, intent_id: str) -> dict:
        record = self.state.payments.get(intent_id)
        if record is None:
            return {
                "status": "refused",
                "message": "No payment with this intent_id.",
                "next_step": "Check the intent_id.",
            }
        if record["state"] == "starting":
            return self._answer(
                record,
                "We sent the payment but never got an answer, so we do not know "
                "whether the bank has it.",
            )
        if not await self._refresh(record):
            return {
                **self._answer(record, "The bank did not answer the status check."),
                "status": "unknown",
                "last_known_status": record["state"],
                "next_step": "Check again in a moment. Do NOT create a new payment: this one may have gone through.",
            }
        return self._answer(record)

    async def cancel_payment(self, intent_id: str) -> dict:
        record = self.state.payments.get(intent_id)
        if record is None:
            return {
                "status": "refused",
                "message": "No payment with this intent_id.",
                "next_step": "Check the intent_id.",
            }
        if record["bank_payment_id"] is None:
            return self._answer(
                record,
                "There is nothing to cancel: the bank never confirmed this payment. "
                "It cannot be approved without a link, so it will time out by itself.",
            )
        await self._refresh(record)
        if record["state"] != "awaiting_customer":
            return self._answer(
                record,
                "Too late to cancel: the payment is no longer waiting for the customer.",
            )
        try:
            await self.bank.cancel_payment(record["bank_payment_id"])
        except BankProblem as problem:
            return explain_problem(problem)
        record["state"] = "cancelled"
        self.state.save()
        return self._answer(record, "The payment was cancelled.")

    # ---------- helpers ----------
    @staticmethod
    def _check_input(
        intent_id, from_iban, to_iban, to_name, amount, reference, currency
    ) -> str:
        """Returns a sentence describing the first problem, or an empty string."""
        if not intent_id or len(intent_id) > 64:
            return "intent_id is required (up to 64 characters)."
        for name, iban in (("from_iban", from_iban), ("to_iban", to_iban)):
            if not _IBAN.match(iban.replace(" ", "").upper()):
                return f"{name} is not a valid Georgian IBAN."
        if currency != "GEL":
            return "Only GEL payments are supported."
        try:
            value = Decimal(amount)
        except InvalidOperation:
            return "amount must be a number such as 25.50."
        if value <= 0 or value != value.quantize(Decimal("0.01")):
            return "amount must be positive with at most two decimals."
        if not to_name.strip():
            return "to_name (the receiver's name) is required."
        if not reference.strip() or len(reference) > 35:
            return "reference is required (up to 35 characters)."
        return ""

    def _today_total(self) -> Decimal:
        today = self.now().date().isoformat()
        return sum(
            (
                Decimal(r["amount"])
                for r in self.state.payments.values()
                if r["created_at"][:10] == today and r["state"] not in _DEAD
            ),
            Decimal(0),
        )

    async def _send(self, record: dict) -> dict:
        """Send a payment to the bank and record the outcome. Used for the first try and for retries."""
        body = {
            "debtorAccount": {"iban": record["from_iban"]},
            "instructedAmount": {
                "currency": record["currency"],
                "amount": record["amount"],
            },
            "creditorAccount": {"iban": record["to_iban"]},
            "creditorName": record["to_name"],
            "endToEndIdentification": record["reference"],
            "remittanceInformationUnstructured": record["reference"],
        }
        request_id = str(
            uuid.uuid5(_NAMESPACE, record["intent_id"])
        )  # the same for every retry of this intent
        try:
            answer = await self.bank.start_payment(body, request_id)
        except BankProblem as problem:
            if problem.kind == "unreachable":  # we do not know. Keep 'starting'.
                return self._answer(
                    record,
                    "No proper answer from the bank, so we do NOT know if it has the "
                    "payment. It is safe to call make_payment again with the same intent_id.",
                )
            record["state"] = "rejected"  # the bank clearly said no
            self.state.save()
            return {**explain_problem(problem), "intent_id": record["intent_id"]}
        record["bank_payment_id"] = answer["paymentId"]
        record["link"] = answer["_links"]["scaRedirect"]["href"]
        record["state"] = _BANK_STATUS.get(answer.get("transactionStatus"), "unknown")
        self.state.save()
        return self._answer(record)

    async def _refresh(self, record: dict) -> bool:
        """Ask the bank for the payment's status and store it. False if the bank could not be asked."""
        if record["bank_payment_id"] is None or record["state"] in _DEAD | {
            "completed"
        }:
            return True
        try:
            status = await self.bank.payment_status(record["bank_payment_id"])
        except BankProblem:
            return False
        record["state"] = _BANK_STATUS.get(status, record["state"])
        self.state.save()
        return True

    @staticmethod
    def _answer(record: dict, message: str = "") -> dict:
        """What the agent sees for a payment. Always says what to do next."""
        state = record["state"]
        next_step = {
            "starting": "Call make_payment again with the same intent_id to try again (safe).",
            "awaiting_customer": "Show the customer_link to the customer. They must approve on the bank's page. "
            "Check payment_status afterwards.",
            "completed": "Done. Do not send again.",
            "pending": "The money is on its way. Check payment_status later. Do not send again.",
            "rejected": "The payment did not happen. To try again, use a NEW intent_id.",
            "cancelled": "The payment did not happen. To try again, use a NEW intent_id.",
        }.get(state, "Check payment_status.")
        meaning = {
            "starting": "unknown",
            "awaiting_customer": "Waiting for the customer to approve.",
            "completed": "Paid.",
            "pending": "Approved, the bank is still processing.",
            "rejected": "Not paid (refused, declined or timed out).",
            "cancelled": "Cancelled.",
        }
        answer = {
            "intent_id": record["intent_id"],
            "status": "unknown" if state == "starting" else state,
            "amount": record["amount"],
            "currency": record["currency"],
            "to": record["to_name"],
            "message": message or meaning.get(state, state),
            "next_step": next_step,
        }
        if state == "awaiting_customer":
            answer["customer_link"] = record["link"]
        return answer
