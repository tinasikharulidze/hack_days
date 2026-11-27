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

Optional: GEMINI_MODEL (default gemini-2.5-flash), PAYGATE_URL (default http://127.0.0.1:8001/mcp),
LANGUAGE (ka = Georgian, the default, or en = English).
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from google import genai
from google.genai import types
from mcp.client import Client

PROMPT_BODY = """\
You are a careful payment assistant for a bank customer in Georgia.

{language_rule}
- The tool answers are in English. Translate their meaning for the customer, but copy amounts, currencies, IBANs,
  account ids, names, references and links exactly as they are.
- Use the paygate tools to read accounts and make payments. Never invent account numbers, balances or results.
- Every payment must be approved by the customer on their bank's page. Give them the link and ask them to tell you when they have approved.
- Give each payment its own unique intent_id, and reuse the SAME intent_id when you retry that payment.
- If a tool says the outcome is unknown, never start a new payment. Follow the tool's next_step.
- Read the next_step in every tool answer and follow it.
- Keep answers short and plain. Always show amounts with their currency.
"""

LANGUAGE_RULES = {
    "ka": "- Always reply in Georgian (ქართულად), in natural, polite Georgian, even if the customer writes in English, "
          "unless they ask for another language.",
    "en": "- Reply in the language the customer writes in (Georgian if they write Georgian).",
}


def language() -> str:
    """The language setting: LANGUAGE=ka (the default) or LANGUAGE=en, from the environment or the .env file."""
    value = os.environ.get("LANGUAGE", "ka").strip().lower()
    return value if value in LANGUAGE_RULES else "ka"


def system_prompt(lang: str | None = None) -> str:
    """The instructions given to the model at the start of every request."""
    return PROMPT_BODY.format(language_rule=LANGUAGE_RULES[lang or language()])


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


def print_tool(name: str, arguments: dict, data: dict) -> None:
    """The default way to show a tool call: two lines in the terminal."""
    print(f"   -> {name}({json.dumps(arguments, ensure_ascii=False)})")
    print(f"   <- {data.get('status', '?')}: {data.get('message', '')}")


async def run_turn(llm, model: str, config: types.GenerateContentConfig, mcp: Client,
                   contents: list, transcript: str, max_steps: int = 12, on_tool=print_tool,
                   on_step=None) -> str:
    """One customer message in, one answer out. The model may call several tools in between.

    on_tool(name, arguments, result) is called after every tool call, so a caller (the terminal, or the
    web page in chat_ui.py) can show what the agent is doing.
    on_step(kind, name, milliseconds) is optional: it reports how long each model call ("model") and
    each tool call ("tool") took, right before on_tool, so the web page can draw a timeline.
    """
    for _ in range(max_steps):
        if on_tool is print_tool:
            print("   (asking Gemini...)")
        started = time.perf_counter()
        try:
            response = await llm.aio.models.generate_content(model=model, contents=contents, config=config)
        except Exception as error:                                # show the real problem instead of hanging
            if on_step:
                on_step("model", "error", round((time.perf_counter() - started) * 1000))
            log(transcript, "error", text=repr(error))
            return f"(Gemini call failed: {type(error).__name__}: {str(error)[:500]})"
        if on_step:
            on_step("model", "gemini", round((time.perf_counter() - started) * 1000))
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
            started = time.perf_counter()
            answer = await mcp.call_tool(call.name, arguments)
            data = json.loads(answer.content[0].text)
            if on_step:
                on_step("tool", call.name, round((time.perf_counter() - started) * 1000))
            on_tool(call.name, arguments, data)
            log(transcript, "tool", name=call.name, arguments=arguments, result=data)
            results.append(types.Part.from_function_response(name=call.name, response=data))
        contents.append(types.Content(role="user", parts=results))
    return "(The agent made too many tool calls in a row and stopped.)"


async def main() -> None:
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = os.environ.get("PAYGATE_URL", "http://127.0.0.1:8001/mcp")
    transcript = "agent_transcript.jsonl"
    # Reads GEMINI_API_KEY from the environment. The timeout (milliseconds) stops a stuck call from hanging for ever.
    llm = genai.Client(http_options=types.HttpOptions(timeout=60_000))
    async with Client(url) as mcp:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt(), tools=[await tools_for_gemini(mcp)],
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
