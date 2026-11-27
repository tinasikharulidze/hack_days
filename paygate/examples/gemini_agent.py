"""A small chat agent: Gemini talking to the paygate MCP server.

This is the missing piece that makes the infrastructure an AGENT. It is only a
loop around a model:

    you type something
      -> Gemini reads it, plus the list of tools (from the MCP server)
      -> Gemini either answers in words, or asks to call a tool
      -> we run the tool on the MCP server and give the result back to Gemini
      -> repeat until Gemini answers in words

Everything is printed, so you see exactly what the agent does. Every step is also
written to agent_transcript.jsonl.

Run (the mock bank and `python -m paygate.server` must already be running):

    $env:GEMINI_API_KEY = "your key"          # PowerShell
    uv run python examples/gemini_agent.py

Optional: GEMINI_MODEL (default gemini-2.5-flash), PAYGATE_URL (default http://127.0.0.1:8001/mcp).
"""

import asyncio
import json
import os
from datetime import datetime, timezone

from google import genai
from google.genai import types
from mcp.client import Client

SYSTEM_PROMPT = """\
You are a careful payment assistant for a bank customer in Georgia.

- Reply in the language the customer writes in (Georgian if they write Georgian).
- Use the paygate tools to read accounts and make payments. Never invent account numbers, balances or results.
- Every payment must be approved by the customer on their bank's page. Give them the link and ask them to tell you when they have approved.
- Give each payment its own unique intent_id, and reuse the SAME intent_id when you retry that payment.
- If a tool says the outcome is unknown, never start a new payment. Follow the tool's next_step.
- Read the next_step in every tool answer and follow it.
- Keep answers short and plain. Always show amounts with their currency.
"""


def clean_schema(schema):
    """Gemini accepts most of JSON Schema; drop the few decorations it does not need."""
    if isinstance(schema, dict):
        return {k: clean_schema(v) for k, v in schema.items()
                if k not in ("title", "default", "$schema", "additionalProperties")}
    if isinstance(schema, list):
        return [clean_schema(v) for v in schema]
    return schema


def log(path: str, kind: str, **data) -> None:
    with open(path, "a", encoding="utf-8") as file:
        file.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "kind": kind, **data},
                              ensure_ascii=False) + "\n")


async def tools_for_gemini(mcp: Client) -> types.Tool:
    """Turn the MCP server's tool list into Gemini's format. Our docstrings become the descriptions."""
    listed = await mcp.list_tools()
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t.name, description=t.description,
                                  parameters_json_schema=clean_schema(t.input_schema))
        for t in listed.tools])


async def run_turn(llm, model: str, config: types.GenerateContentConfig, mcp: Client,
                   contents: list, transcript: str, max_steps: int = 12) -> str:
    """One customer message in, one answer out. The model may call several tools in between."""
    for _ in range(max_steps):
        response = await llm.aio.models.generate_content(model=model, contents=contents, config=config)
        reply = response.candidates[0].content
        contents.append(reply)                                    # keep it exactly as given (Gemini needs that)
        calls = [part.function_call for part in reply.parts or [] if part.function_call]
        if not calls:
            text = "".join(part.text or "" for part in reply.parts or [] if not getattr(part, "thought", False))
            log(transcript, "agent", text=text)
            return text
        results = []
        for call in calls:
            arguments = dict(call.args or {})
            print(f"   -> {call.name}({json.dumps(arguments, ensure_ascii=False)})")
            answer = await mcp.call_tool(call.name, arguments)
            data = json.loads(answer.content[0].text)
            print(f"   <- {data.get('status', '?')}: {data.get('message', '')}")
            log(transcript, "tool", name=call.name, arguments=arguments, result=data)
            results.append(types.Part.from_function_response(name=call.name, response=data))
        contents.append(types.Content(role="user", parts=results))
    return "(The agent made too many tool calls in a row and stopped.)"


async def main() -> None:
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = os.environ.get("PAYGATE_URL", "http://127.0.0.1:8001/mcp")
    transcript = "agent_transcript.jsonl"
    llm = genai.Client()                                           # reads GEMINI_API_KEY from the environment
    async with Client(url) as mcp:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT, tools=[await tools_for_gemini(mcp)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        print(f"Connected to {url}. Model: {model}. Type 'quit' to stop.\n")
        contents: list = []
        while True:
            message = (await asyncio.to_thread(input, "You: ")).strip()
            if message.lower() in ("quit", "exit"):
                break
            if not message:
                continue
            log(transcript, "customer", text=message)
            contents.append(types.Content(role="user", parts=[types.Part(text=message)]))
            print("\nAgent:", await run_turn(llm, model, config, mcp, contents, transcript), "\n")


if __name__ == "__main__":
    asyncio.run(main())
