"""Tests for the layer between an agent and the bank (paygate/).

Each test plays three parts at once, all inside this process:
  - the AGENT: calls MCP tools exactly as an agent would (through a real MCP client),
  - the CUSTOMER: signs in and approves on the mock bank's pages,
  - the BANK: the mock itself, with its faults (lost answers, errors, expiry).
"""

import asyncio
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client import Client
from starlette.testclient import TestClient

from app.seed import make_iban
from paygate.bank import BankClient
from paygate.config import Config
from paygate.gate import PayGate
from paygate.server import build_server
from paygate.state import State

OUTSIDE_IBAN = make_iban("0000111122223333")      # a valid IBAN that is not one of the bank's accounts


class AnswerLostTransport(httpx.ASGITransport):
    """The bank does the work, then the answer never arrives (like a real network timeout).

    (httpx only enforces timeouts on real sockets, so in-process tests have to raise one themselves.)
    """

    def __init__(self, app, lose_next_payment_answers=1):
        super().__init__(app=app)
        self.to_lose = lose_next_payment_answers

    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        if request.method == "POST" and request.url.path == "/v1/payments/domestic" and self.to_lose > 0:
            self.to_lose -= 1
            raise httpx.ReadTimeout("answer lost", request=request)
        return response


class World:
    """One agent + one customer + the mock bank. Use as:  async with World(app) as w: ..."""

    def __init__(self, app, tmp_path, state_file=None, transport=None, **config_changes):
        self.app = app
        self.config = Config(audit_file=str(tmp_path / "audit.jsonl"), **config_changes)
        self.state = State(state_file)
        self.bank_http = httpx.AsyncClient(transport=transport or httpx.ASGITransport(app=app), base_url="http://mock",
                                           follow_redirects=False)
        self.bank = BankClient(self.config, self.state, self.bank_http)
        self.gate = PayGate(self.config, self.state, self.bank)
        self.server = build_server(self.gate, self.bank, self.config)

    async def __aenter__(self):
        self.mcp = await Client(self.server).__aenter__()
        data = (await self.bank_http.get("/admin/data")).json()
        self.person = data["customers"][0]
        self.accounts = [a for a in data["accounts"] if a["customer_id"] == self.person["id"]]
        self.gel = next(a for a in self.accounts if a["currency"] == "GEL")
        return self

    async def __aexit__(self, *exc):
        await self.mcp.__aexit__(*exc)
        await self.bank_http.aclose()

    # ----- the agent -----
    async def tool(self, name, **arguments) -> dict:
        """Call a tool the way an agent does; return the JSON answer it sees."""
        result = await self.mcp.call_tool(name, arguments)
        return json.loads(result.content[0].text)

    # ----- the customer -----
    async def customer_decides(self, link, decision="approve") -> dict:
        """Sign in and press a button on the bank's page. Returns what the bank sends back to the app."""
        path = urlparse(link).path
        await self.bank_http.post(path + "/login", data={"username": self.person["username"],
                                                         "password": self.person["password"]})
        form = {"decision": decision}
        if "/approve/" in path:
            form["account"] = [a["resource_id"] for a in self.accounts]
        back = await self.bank_http.post(path + "/decide", data=form)
        assert back.status_code == 303
        return {k: v[0] for k, v in parse_qs(urlparse(back.headers["location"]).query).items()}

    async def connect(self):
        """The whole permission journey; leaves the layer connected."""
        ibans = [a["iban"] for a in self.accounts]
        waiting = await self.tool("connect_bank", ibans=ibans)
        assert waiting["status"] == "waiting_for_customer"
        await self.gate.handle_callback(await self.customer_decides(waiting["customer_link"]))
        assert (await self.tool("connect_bank", ibans=ibans))["status"] == "connected"

    async def pay(self, intent="order-1", amount="25.50", reference="Order 1", **more):
        return await self.tool("make_payment", intent_id=intent, from_iban=self.gel["iban"], to_iban=OUTSIDE_IBAN,
                               to_name="Example Shop", amount=amount, reference=reference, **more)

    async def bank_payments(self) -> list:
        return (await self.bank_http.get("/admin/payments")).json()["payments"]

    async def balance(self) -> Decimal:
        data = (await self.bank_http.get("/admin/data")).json()
        for account in data["accounts"]:
            if account["resource_id"] == self.gel["resource_id"]:
                return Decimal(str(account["balance"])) if "balance" in account else Decimal("0")


def run(coroutine):
    return asyncio.run(coroutine)


# ---------------------------------------------------------------- what the agent sees

def test_the_agent_sees_seven_tools_each_with_a_description(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            tools = (await w.mcp.list_tools()).tools
            assert {t.name for t in tools} == {"connect_bank", "list_accounts", "get_balances", "get_transactions",
                                               "make_payment", "payment_status", "cancel_payment"}
            for tool in tools:
                assert len(tool.description) > 80                      # a real explanation, not a stub
                for name, spec in tool.input_schema.get("properties", {}).items():
                    assert spec.get("description"), f"{tool.name}.{name} has no description"
    run(go())


def test_the_payment_tool_description_tells_the_agent_what_to_do_when_unsure(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            tool = next(t for t in (await w.mcp.list_tools()).tools if t.name == "make_payment")
            assert "SAME intent_id" in tool.description and "unknown" in tool.description
    run(go())


# ---------------------------------------------------------------- connecting and reading

def test_connect_then_read_accounts_balances_and_transactions(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            listed = await w.tool("list_accounts")
            assert listed["status"] == "ok" and len(listed["accounts"]) == len(w.accounts)
            balances = await w.tool("get_balances", account_id=w.gel["resource_id"])
            assert balances["status"] == "ok" and balances["balances"][0]["currency"] == "GEL"
            transactions = await w.tool("get_transactions", account_id=w.gel["resource_id"], date_from="2020-01-01", booking_status="both")
            assert transactions["status"] == "ok" and transactions["transactions"], transactions
            assert {"date", "amount", "currency", "other_party", "reference"} <= set(transactions["transactions"][0])
    run(go())


def test_connect_twice_before_approval_gives_the_same_link(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            ibans = [a["iban"] for a in w.accounts]
            first = await w.tool("connect_bank", ibans=ibans)
            second = await w.tool("connect_bank", ibans=ibans)
            assert first["customer_link"] == second["customer_link"]
    run(go())


def test_reading_before_connecting_says_to_connect(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            answer = await w.tool("list_accounts")
            assert answer["status"] == "not_connected" and "connect_bank" in answer["next_step"]
    run(go())


def test_customer_refusing_the_permission_is_reported(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            ibans = [a["iban"] for a in w.accounts]
            waiting = await w.tool("connect_bank", ibans=ibans)
            params = await w.customer_decides(waiting["customer_link"], "reject")
            assert "did not give permission" in await w.gate.handle_callback(params)
            assert (await w.tool("list_accounts"))["status"] == "not_connected"
    run(go())


def test_an_expired_access_token_is_renewed_without_the_agent_noticing(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.bank_http.post("/admin/time/advance", json={"minutes": 61})       # access token lasts 60
            assert (await w.tool("list_accounts"))["status"] == "ok"
    run(go())


def test_an_expired_consent_tells_the_agent_to_connect_again(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.bank_http.post("/admin/time/advance", json={"minutes": 60 * 24 * 40})   # past the 30 days
            answer = await w.tool("list_accounts")
            assert answer["status"] == "not_connected" and "connect_bank" in answer["next_step"]
            assert w.state.connection["status"] == "expired"
    run(go())


def test_the_daily_read_limit_is_explained_not_retried(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            for _ in range(4):
                assert (await w.tool("list_accounts"))["status"] == "ok"
            answer = await w.tool("list_accounts")
            assert answer["status"] == "rate_limited" and "Do not retry" in answer["next_step"]
    run(go())


def test_long_transaction_lists_are_cut_and_flagged(app, tmp_path):
    async def go():
        async with World(app, tmp_path, max_transactions=3) as w:
            await w.connect()
            answer = await w.tool("get_transactions", account_id=w.gel["resource_id"], date_from="2020-01-01", booking_status="both")
            assert len(answer["transactions"]) == 3 and answer["truncated"] is True
    run(go())


# ---------------------------------------------------------------- paying

def test_a_payment_waits_for_the_customer_then_completes(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            started = await w.pay()
            assert started["status"] == "awaiting_customer" and started["customer_link"]
            assert (await w.tool("payment_status", intent_id="order-1"))["status"] == "awaiting_customer"
            params = await w.customer_decides(started["customer_link"])
            await w.gate.handle_callback(params)
            done = await w.tool("payment_status", intent_id="order-1")
            assert done["status"] == "completed" and "Do not send again" in done["next_step"]
            assert [p["status"] for p in await w.bank_payments()] == ["ACSC"]
    run(go())


def test_the_same_intent_never_makes_a_second_payment(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            first = await w.pay()
            again = await w.pay()
            assert again["customer_link"] == first["customer_link"] and "Nothing new was sent" in again["message"]
            assert len(await w.bank_payments()) == 1
    run(go())


def test_a_made_up_new_intent_id_for_the_same_payment_is_still_caught(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.pay(intent="order-1")
            again = await w.pay(intent="order-1-retry")           # an agent that invents a new id on each retry
            assert "already exists under intent_id 'order-1'" in again["message"]
            assert again["rule"] == "same_payment_other_intent"          # the developer tools show which rule fired
            assert len(await w.bank_payments()) == 1
    run(go())


def test_one_intent_id_cannot_be_reused_for_a_different_payment(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.pay(amount="10.00")
            clash = await w.pay(amount="11.00")
            assert clash["status"] == "refused" and "different payment" in clash["message"]
    run(go())


def test_a_lost_answer_after_the_bank_did_the_work_still_pays_only_once(app, tmp_path):
    """The classic failure. The bank has the payment, the agent hears 503, and tries again."""
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.bank_http.post("/admin/faults", json={"kind": "server_error", "operation": "payments.initiate",
                                                          "phase": "after", "status_code": 503, "times": 1})
            lost = await w.pay()
            assert lost["status"] == "unknown" and "do NOT know" in lost["message"]   # never "failed"
            retry = await w.pay()                                  # the agent does what next_step says
            assert retry["status"] == "awaiting_customer"
            await w.customer_decides(retry["customer_link"])       # the customer only ever got ONE link
            await w.gate.handle_callback({"paymentId": (await w.bank_payments())[-1]["payment_id"]})
            statuses = sorted(p["status"] for p in await w.bank_payments())
            assert statuses.count("ACSC") == 1                     # money moved exactly once
    run(go())


def test_with_a_bank_that_dedupes_the_retry_is_the_same_payment(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.bank_http.put("/admin/settings", json={"deduplicate_by_request_id": True})
            await w.bank_http.post("/admin/faults", json={"kind": "server_error", "operation": "payments.initiate",
                                                          "phase": "after", "status_code": 503, "times": 1})
            await w.pay()
            retry = await w.pay()
            assert retry["status"] == "awaiting_customer"
            assert len(await w.bank_payments()) == 1               # the bank saw one payment only
    run(go())


def test_a_fault_before_creates_nothing_and_the_retry_works(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.bank_http.post("/admin/faults", json={"kind": "server_error", "operation": "payments.initiate",
                                                          "status_code": 503, "times": 1})
            assert (await w.pay())["status"] == "unknown"
            assert (await w.pay())["status"] == "awaiting_customer"
            assert len(await w.bank_payments()) == 1
    run(go())


def test_a_timeout_after_the_bank_did_the_work_counts_as_unknown_and_the_retry_pays_once(app, tmp_path):
    async def go():
        async with World(app, tmp_path, transport=AnswerLostTransport(app)) as w:
            await w.connect()
            lost = await w.pay()
            assert lost["status"] == "unknown"                     # a timeout is not a failure
            assert len(await w.bank_payments()) == 1               # ...and the bank did create the payment
            retry = await w.pay()
            assert retry["status"] == "awaiting_customer"
            await w.customer_decides(retry["customer_link"])
            await w.gate.handle_callback({"paymentId": (await w.bank_payments())[-1]["payment_id"]})
            assert sorted(p["status"] for p in await w.bank_payments()).count("ACSC") == 1
    run(go())


def test_a_failed_status_check_after_approval_does_not_invite_a_second_payment(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            started = await w.pay()
            await w.customer_decides(started["customer_link"])
            await w.bank_http.post("/admin/faults", json={"kind": "server_error", "operation": "payments.status",
                                                          "status_code": 503, "times": 2})
            unsure = await w.tool("payment_status", intent_id="order-1")
            assert unsure["status"] == "unknown" and "Do NOT create a new payment" in unsure["next_step"]
            again = await w.pay()                                  # the agent panics and pays again
            assert "Nothing new was sent" in again["message"]
            assert len(await w.bank_payments()) == 1
            assert (await w.tool("payment_status", intent_id="order-1"))["status"] == "completed"
    run(go())


def test_a_payment_the_bank_keeps_pending_is_reported_as_pending(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.bank_http.post("/admin/faults", json={"kind": "payment_stays_pending", "times": 1})
            started = await w.pay()
            await w.customer_decides(started["customer_link"])
            answer = await w.tool("payment_status", intent_id="order-1")
            assert answer["status"] == "pending" and "Do not send again" in answer["next_step"]
    run(go())


def test_a_customer_saying_no_ends_it_and_a_new_intent_may_try_again(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            started = await w.pay()
            await w.customer_decides(started["customer_link"], "reject")
            assert (await w.tool("payment_status", intent_id="order-1"))["status"] == "rejected"
            assert "NEW intent_id" in (await w.tool("payment_status", intent_id="order-1"))["next_step"]
            fresh = await w.pay(intent="order-1-second-try")      # same details are fine: the first one is dead
            assert fresh["status"] == "awaiting_customer"
    run(go())


def test_a_payment_the_customer_never_approves_times_out_as_rejected(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.pay()
            await w.bank_http.post("/admin/time/advance", json={"minutes": 16})      # approval window is 15
            assert (await w.tool("payment_status", intent_id="order-1"))["status"] == "rejected"
    run(go())


def test_cancel_works_before_approval_and_not_after(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.pay(intent="a", reference="A")
            assert (await w.tool("cancel_payment", intent_id="a"))["status"] == "cancelled"
            started = await w.pay(intent="b", reference="B")
            await w.customer_decides(started["customer_link"])
            late = await w.tool("cancel_payment", intent_id="b")
            assert late["status"] == "completed" and "Too late" in late["message"]
    run(go())


# ---------------------------------------------------------------- the gate's own rules

def test_limits_per_payment_and_per_day(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            big = await w.pay(intent="big", amount="250.00", reference="big")
            assert big["status"] == "refused" and "200.00" in big["message"]
            assert (await w.pay(intent="p1", amount="190.00", reference="p1"))["status"] == "awaiting_customer"
            assert (await w.pay(intent="p2", amount="190.00", reference="p2"))["status"] == "awaiting_customer"
            third = await w.pay(intent="p3", amount="190.00", reference="p3")            # 570 > 500
            assert third["status"] == "refused" and "daily limit" in third["message"]
            assert len(await w.bank_payments()) == 2                                      # the bank never heard of p3
    run(go())


def test_a_cancelled_payment_frees_its_part_of_the_daily_limit(app, tmp_path):
    async def go():
        async with World(app, tmp_path, max_per_day_gel="200.00") as w:
            await w.connect()
            await w.pay(intent="p1", amount="150.00", reference="p1")
            await w.tool("cancel_payment", intent_id="p1")
            assert (await w.pay(intent="p2", amount="150.00", reference="p2"))["status"] == "awaiting_customer"
    run(go())


def test_bad_input_is_refused_before_the_bank_is_called(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            assert "two decimals" in (await w.pay(amount="1.234"))["message"]
            assert "number" in (await w.pay(intent="x", amount="abc"))["message"]
            assert "Only GEL" in (await w.pay(intent="y", currency="USD"))["message"]
            assert "valid Georgian IBAN" in (await w.tool("make_payment", intent_id="z", from_iban="nope",
                to_iban=OUTSIDE_IBAN, to_name="Shop", amount="5.00", reference="r"))["message"]
            other = make_iban("9999888877776666")
            answer = await w.tool("make_payment", intent_id="w", from_iban=other, to_iban=OUTSIDE_IBAN,
                                  to_name="Shop", amount="5.00", reference="r")
            assert answer["status"] == "refused" and "not given access" in answer["message"]
            assert await w.bank_payments() == []
    run(go())


def test_the_bank_refusing_a_payment_is_passed_on_in_plain_words(app, tmp_path):
    async def go():
        async with World(app, tmp_path, max_payment_gel="100000.00", max_per_day_gel="100000.00") as w:
            await w.connect()
            answer = await w.pay(amount="99999.00", reference="too much")     # more money than the account has
            assert answer["status"] == "refused" and "bank refused" in answer["message"]
    run(go())


# ---------------------------------------------------------------- memory and records

def test_payment_records_survive_a_restart(app, tmp_path):
    state_file = str(tmp_path / "state.json")

    async def first():
        async with World(app, tmp_path, state_file=state_file) as w:
            await w.connect()
            await w.pay()

    async def second():
        async with World(app, tmp_path, state_file=state_file) as w:           # a "restarted" layer
            again = await w.pay()
            assert "Nothing new was sent" in again["message"]
            assert len(await w.bank_payments()) == 1
    run(first())
    run(second())


def test_every_tool_call_is_written_to_the_audit_file(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            await w.connect()
            await w.pay()
            lines = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
            assert [line["tool"] for line in lines].count("make_payment") == 1
            assert all("time" in line and "status" in line for line in lines)
    run(go())


def test_the_callback_page_works_over_http(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            waiting = await w.tool("connect_bank", ibans=[a["iban"] for a in w.accounts])
            params = await w.customer_decides(waiting["customer_link"])
            web = TestClient(w.server.streamable_http_app())
            return web.get("/callback", params=params)
    page = run(go())
    assert page.status_code == 200 and "Permission given" in page.text
