"""A chat web page for the agent, so you do not have to use the terminal.

It is the same agent as gemini_agent.py (same loop, same tools), with a web page in front:

    browser  <-->  this file (a tiny web server, port 8002)  <-->  Gemini
                                   |
                                   +--> paygate MCP server (port 8001) --> bank (port 8000)

The page has two halves. Left: the customer's side (the chat on top, the bank's own pages below).
Right: developer tools (a timeline of every model and tool call with timings, the gate's decisions,
what the bank really did, and buttons that make the bank misbehave). When a tool returns a link for
the customer, the page shows a button that loads the bank's page in the lower left, so you never copy links.

The page also asks THIS server for the bank's real books and for fault switches (/dev/bank, /dev/fault).
We pass those on to the mock bank's admin API, because a browser page may not talk to another port directly.

Run (the bank and paygate must already be running; or use run_all.py, which starts everything):

    uv run --env-file .env python examples/chat_ui.py

Then open http://127.0.0.1:8002 (it opens by itself unless NO_BROWSER is set).
Optional: GEMINI_MODEL, PAYGATE_URL, BANK_URL, CHAT_UI_PORT, LANGUAGE (ka = Georgian, the default, or en = English).
"""

import asyncio
import os
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                       # so "import gemini_agent" finds the file next to this one

import gemini_agent as agent                        # noqa: E402  (the loop and the tool conversion live there)
import httpx                                        # noqa: E402  (to reach the mock bank's admin API)
import uvicorn                                      # noqa: E402
from google import genai                            # noqa: E402
from google.genai import types                      # noqa: E402
from mcp.client import Client                       # noqa: E402
from starlette.applications import Starlette        # noqa: E402
from starlette.requests import Request              # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from starlette.routing import Route                 # noqa: E402


# Ready-made ways to make the mock bank misbehave. Each one is a request to the bank's admin API (POST /admin/faults).
FAULT_PRESETS = {
    # The bank takes the payment, then answers too late (the gate waits 10 s). This is how duplicates are born.
    "lose_answer": {"kind": "slow", "operation": "payments.initiate", "phase": "after", "times": 1, "delay_seconds": 15},
    "slow": {"kind": "slow", "operation": "*", "phase": "before", "delay_seconds": 5},
    "bank_down": {"kind": "server_error", "operation": "*", "phase": "before", "status_code": 503},
    "stuck_pending": {"kind": "payment_stays_pending", "operation": "*"},
}


def build_app(llm, model: str, config: types.GenerateContentConfig, mcp: Client,
              transcript: str = "agent_transcript.jsonl", bank_url: str | None = None,
              bank_transport=None) -> Starlette:
    """The web app. It keeps ONE conversation in memory (this is a demo for one person at a time).

    bank_url is where the mock bank runs (for the developer panel). bank_transport is only for tests.
    """
    bank_url = (bank_url or os.environ.get("BANK_URL", "http://127.0.0.1:8000")).rstrip("/")
    contents: list = []                              # the conversation so far: the agent's only memory
    one_at_a_time = asyncio.Lock()                   # two messages at once would mix up the conversation

    async def page(request: Request) -> HTMLResponse:
        html = (HERE / "chat_ui.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__LANG__", agent.language()))     # the page picks its words from this

    async def chat(request: Request) -> JSONResponse:
        """The page sends one customer message; we answer with the agent's words and the tool calls it made."""
        message = ((await request.json()).get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "Empty message."}, status_code=400)
        tools: list = []                             # tool calls, in order
        timeline: list = []                          # model calls and tool calls, in order, with milliseconds
        last_ms = {"tool": 0}

        def on_step(kind: str, name: str, ms: int) -> None:
            if kind == "model":
                timeline.append({"kind": "model", "name": name, "ms": ms})
            else:
                last_ms["tool"] = ms                 # on_tool follows straight away and picks this up

        def on_tool(name: str, arguments: dict, result: dict) -> None:
            call = {"name": name, "arguments": arguments, "result": result}
            tools.append(call)
            timeline.append({"kind": "tool", "ms": last_ms["tool"], **call})

        started = time.perf_counter()
        async with one_at_a_time:
            agent.log(transcript, "customer", text=message)
            contents.append(types.Content(role="user", parts=[types.Part(text=message)]))
            reply = await agent.run_turn(llm, model, config, mcp, contents, transcript,
                                         on_tool=on_tool, on_step=on_step)
        return JSONResponse({"reply": reply, "tools": tools, "timeline": timeline,
                             "total_ms": round((time.perf_counter() - started) * 1000)})

    def bank_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=bank_url, timeout=3, transport=bank_transport)

    async def dev_bank(request: Request) -> JSONResponse:
        """The bank's real books, for the developer panel: payments, duplicates, balances, faults that are on."""
        try:
            async with bank_client() as bank:
                payments = (await bank.get("/admin/payments")).json()
                data = (await bank.get("/admin/data")).json()
                faults = (await bank.get("/admin/faults")).json()
        except Exception as error:                   # bank stopped or not reachable: the page shows a notice
            return JSONResponse({"ok": False, "error": f"{type(error).__name__}"})
        return JSONResponse({"ok": True, "payments": payments["payments"],
                             "completed_count": payments["completed_count"],
                             "duplicates": payments["duplicates"],
                             "accounts": data["accounts"], "faults": faults["faults"]})

    async def dev_fault(request: Request) -> JSONResponse:
        """Switch a ready-made fault on ({"preset": "lose_answer"}) or every fault off ({"preset": "clear"})."""
        preset = (await request.json()).get("preset")
        try:
            async with bank_client() as bank:
                if preset == "clear":
                    await bank.delete("/admin/faults")
                elif preset in FAULT_PRESETS:
                    await bank.post("/admin/faults", json=FAULT_PRESETS[preset])
                else:
                    return JSONResponse({"error": "Unknown preset."}, status_code=400)
        except Exception as error:
            return JSONResponse({"ok": False, "error": f"{type(error).__name__}"}, status_code=502)
        return JSONResponse({"ok": True})

    async def reset(request: Request) -> JSONResponse:
        """Start a new conversation. (The bank connection and the payments are NOT reset.)"""
        contents.clear()
        return JSONResponse({"ok": True})

    async def no_icon(request: Request) -> Response:
        return Response(status_code=204)             # browsers ask for a tab icon; we have none

    return Starlette(routes=[Route("/", page), Route("/favicon.ico", no_icon), Route("/chat", chat, methods=["POST"]),
                             Route("/reset", reset, methods=["POST"]),
                             Route("/dev/bank", dev_bank), Route("/dev/fault", dev_fault, methods=["POST"])])


async def main() -> None:
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = os.environ.get("PAYGATE_URL", "http://127.0.0.1:8001/mcp")
    port = int(os.environ.get("CHAT_UI_PORT", "8002"))
    llm = genai.Client(http_options=types.HttpOptions(timeout=60_000))
    async with Client(url) as mcp:
        config = types.GenerateContentConfig(
            system_instruction=agent.system_prompt(), tools=[await agent.tools_for_gemini(mcp)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        app = build_app(llm, model, config, mcp)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        print(f"Chat page: http://127.0.0.1:{port}   (model: {model}, tools from {url})   Ctrl+C to stop")
        if not os.environ.get("NO_BROWSER"):
            asyncio.get_running_loop().call_later(1.5, webbrowser.open, f"http://127.0.0.1:{port}")
        await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
