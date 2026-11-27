"""Tests the example Gemini agent's loop with a pretend Gemini, so no key or network is needed."""

import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("google.genai")
from google.genai import types            # noqa: E402
from mcp.client import Client             # noqa: E402

from tests.test_paygate import World       # noqa: E402

spec = importlib.util.spec_from_file_location("gemini_agent", Path(__file__).parents[1] / "examples/gemini_agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def model_says(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))])


class PretendGemini:
    """Plays a fixed script of replies, and records what it was shown."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []
        self.aio = self
        self.models = self

    async def generate_content(self, model, contents, config):
        self.seen.append(len(contents))
        return self.replies.pop(0)


def test_the_tool_list_reaches_gemini_with_descriptions(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            async with Client(w.server) as mcp:
                tool = await agent.tools_for_gemini(mcp)
                names = {d.name for d in tool.function_declarations}
                assert "make_payment" in names and len(names) == 7
                make = next(d for d in tool.function_declarations if d.name == "make_payment")
                assert "SAME intent_id" in make.description
                assert "title" not in str(make.parameters_json_schema)
    asyncio.run(go())


def test_the_loop_runs_a_tool_and_returns_the_final_words(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            async with Client(w.server) as mcp:
                llm = PretendGemini([
                    model_says(types.Part(function_call=types.FunctionCall(name="list_accounts", args={}))),
                    model_says(types.Part(text="Please connect your bank first."))])
                contents = [types.Content(role="user", parts=[types.Part(text="show my accounts")])]
                text = await agent.run_turn(llm, "x", types.GenerateContentConfig(), mcp, contents,
                                            str(tmp_path / "t.jsonl"))
                assert text == "Please connect your bank first."
                assert llm.seen == [1, 3]                       # second call saw: question, tool request, tool result
                lines = (tmp_path / "t.jsonl").read_text()
                assert '"list_accounts"' in lines and "not_connected" in lines
    asyncio.run(go())


def test_the_loop_stops_if_the_agent_never_stops_calling_tools(app, tmp_path):
    async def go():
        async with World(app, tmp_path) as w:
            async with Client(w.server) as mcp:
                call = types.Part(function_call=types.FunctionCall(name="list_accounts", args={}))
                llm = PretendGemini([model_says(call) for _ in range(5)])
                contents = [types.Content(role="user", parts=[types.Part(text="x")])]
                text = await agent.run_turn(llm, "x", types.GenerateContentConfig(), mcp, contents,
                                            str(tmp_path / "t.jsonl"), max_steps=3)
                assert "too many tool calls" in text
    asyncio.run(go())


# ---------------------------------------------------------------- the chat web page

def load_chat_ui():
    ui_spec = importlib.util.spec_from_file_location("chat_ui", Path(__file__).parents[1] / "examples/chat_ui.py")
    module = importlib.util.module_from_spec(ui_spec)
    ui_spec.loader.exec_module(module)
    return module


def test_the_web_page_runs_a_turn_and_reports_the_tool_calls(app, tmp_path):
    import httpx
    chat_ui = load_chat_ui()

    async def go():
        async with World(app, tmp_path) as w:
            async with Client(w.server) as mcp:
                llm = PretendGemini([
                    model_says(types.Part(function_call=types.FunctionCall(
                        name="connect_bank", args={"ibans": [a["iban"] for a in w.accounts]}))),
                    model_says(types.Part(text="Open the link to approve."))])
                web = chat_ui.build_app(llm, "x", types.GenerateContentConfig(), mcp, str(tmp_path / "t.jsonl"))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://ui") as browser:
                    page = await browser.get("/")
                    assert page.status_code == 200 and "Payment assistant" in page.text
                    answer = (await browser.post("/chat", json={"message": "connect me"})).json()
                    assert answer["reply"] == "Open the link to approve."
                    call = answer["tools"][0]
                    assert call["name"] == "connect_bank" and call["result"]["customer_link"]   # the page shows a button for it
                    kinds = [step["kind"] for step in answer["timeline"]]
                    assert kinds == ["model", "tool", "model"]                 # the timeline: model, tool, model
                    assert all(step["ms"] >= 0 for step in answer["timeline"]) and answer["total_ms"] >= 0
                    assert (await browser.post("/chat", json={"message": "  "})).status_code == 400
                    assert (await browser.post("/reset")).json() == {"ok": True}
    asyncio.run(go())


def test_the_developer_panel_shows_the_banks_books_and_switches_faults(app, tmp_path):
    import httpx
    chat_ui = load_chat_ui()

    async def go():
        async with World(app, tmp_path) as w:
            async with Client(w.server) as mcp:
                web = chat_ui.build_app(PretendGemini([]), "x", types.GenerateContentConfig(), mcp,
                                        str(tmp_path / "t.jsonl"), bank_url="http://mock",
                                        bank_transport=httpx.ASGITransport(app=app))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://ui") as browser:
                    await w.connect()
                    await w.pay(intent="order-1")
                    books = (await browser.get("/dev/bank")).json()
                    assert books["ok"] and books["duplicates"] == [] and books["accounts"] and books["faults"] == []
                    for preset in chat_ui.FAULT_PRESETS:                       # every ready-made fault can be switched on
                        assert (await browser.post("/dev/fault", json={"preset": preset})).json() == {"ok": True}
                    assert len((await browser.get("/dev/bank")).json()["faults"]) == len(chat_ui.FAULT_PRESETS)
                    await browser.post("/dev/fault", json={"preset": "clear"})
                    assert (await browser.get("/dev/bank")).json()["faults"] == []
                    assert (await browser.post("/dev/fault", json={"preset": "nope"})).status_code == 400
    asyncio.run(go())


def test_the_developer_panel_says_so_when_the_bank_is_not_reachable(app, tmp_path):
    import httpx
    chat_ui = load_chat_ui()

    def broken(request):
        raise httpx.ConnectError("bank is off")

    async def go():
        async with World(app, tmp_path) as w:
            async with Client(w.server) as mcp:
                web = chat_ui.build_app(PretendGemini([]), "x", types.GenerateContentConfig(), mcp,
                                        str(tmp_path / "t.jsonl"), bank_url="http://mock",
                                        bank_transport=httpx.MockTransport(broken))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://ui") as browser:
                    assert (await browser.get("/dev/bank")).json()["ok"] is False
    asyncio.run(go())


# ---------------------------------------------------------------- language (Georgian by default)

def test_the_agent_is_told_to_reply_in_georgian_by_default_and_english_on_request(monkeypatch):
    monkeypatch.delenv("LANGUAGE", raising=False)
    assert "Always reply in Georgian" in agent.system_prompt()
    monkeypatch.setenv("LANGUAGE", "en")
    prompt = agent.system_prompt()
    assert "Always reply in Georgian" not in prompt and "language the customer writes in" in prompt
    monkeypatch.setenv("LANGUAGE", "fr")                       # something we do not support falls back to Georgian
    assert agent.language() == "ka"


def test_the_prompt_always_keeps_numbers_and_links_exact():
    for lang in ("ka", "en"):
        assert "copy amounts, currencies, IBANs" in agent.system_prompt(lang)


def test_the_chat_page_speaks_georgian_by_default_and_english_on_request(app, tmp_path, monkeypatch):
    import httpx
    chat_ui = load_chat_ui()

    async def page_text():
        async with World(app, tmp_path) as w:
            async with Client(w.server) as mcp:
                web = chat_ui.build_app(PretendGemini([]), "x", types.GenerateContentConfig(), mcp)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://ui") as browser:
                    return (await browser.get("/")).text
    monkeypatch.delenv("LANGUAGE", raising=False)
    georgian = asyncio.run(page_text())
    assert 'const LANG = ("ka" === "en")' in georgian and "გადახდების ასისტენტი" in georgian
    assert 'id="bankframe"' in georgian                        # the bank panel inside the page
    monkeypatch.setenv("LANGUAGE", "en")
    assert 'const LANG = ("en" === "en")' in asyncio.run(page_text())
