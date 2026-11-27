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
