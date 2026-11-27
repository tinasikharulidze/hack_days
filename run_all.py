"""Starts the whole project with one command:  uv run python run_all.py

It starts three programs and opens the chat page:

    1. the mock bank          http://127.0.0.1:8000   (a practice bank, no real money)
    2. paygate (MCP server)   http://127.0.0.1:8001   (the tools and payment rules for the agent)
    3. the chat page          http://127.0.0.1:8002   (where you talk to the agent)

Press Ctrl+C to stop all three. Their messages go to the logs/ folder, to keep this window quiet.

Every run starts with an EMPTY bank (the mock keeps its data in memory), so this script also deletes
paygate's saved connection from the last run. Otherwise paygate would remember a connection that the
bank no longer knows.
"""

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
BANK, PAYGATE, CHAT = 8000, 8001, 8002


def load_env_file() -> None:
    """Reads KEY=value lines from .env (here, or in paygate/) into the environment. Existing values win."""
    for path in (ROOT / ".env", ROOT / "paygate" / ".env"):
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            print(f"Read settings from {path.relative_to(ROOT)}")
            return


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    load_env_file()
    if not os.environ.get("GEMINI_API_KEY"):
        print("No GEMINI_API_KEY found. Create a file called .env next to run_all.py containing one line:\n"
              "    GEMINI_API_KEY=your-key-here\nThen run this again. (See README.md, step 3.)")
        return 1
    for port in (BANK, PAYGATE, CHAT):
        if port_open(port):
            print(f"Port {port} is already in use. Is the project already running in another window? "
                  f"Close that one first.")
            return 1

    stale = ROOT / "paygate" / "paygate_state.json"
    if stale.exists():
        stale.unlink()
        print("Cleared paygate's saved connection from the last run (the bank starts empty every time).")

    LOGS.mkdir(exist_ok=True)
    (LOGS / ".gitignore").write_text("*\n")              # keep log files out of git
    python = sys.executable
    parts = [
        ("bank", BANK, ROOT / "mockbank",
         [python, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(BANK), "--log-level", "warning"], {}),
        ("paygate", PAYGATE, ROOT / "paygate", [python, "-m", "paygate.server"], {}),
        ("chat", CHAT, ROOT / "paygate", [python, "examples/chat_ui.py"], {"NO_BROWSER": "1"}),
    ]
    running: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, port, folder, command, extra in parts:
            log = open(LOGS / f"{name}.log", "w", encoding="utf-8")
            process = subprocess.Popen(command, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, **extra, "PYTHONUNBUFFERED": "1"})
            running.append((name, process))
            for _ in range(100):                          # wait up to 20 seconds for it to start listening
                if process.poll() is not None:
                    print(f"The {name} program stopped while starting. See logs/{name}.log")
                    return 1
                if port_open(port):
                    break
                time.sleep(0.2)
            else:
                print(f"The {name} program did not start in time. See logs/{name}.log")
                return 1
            print(f"  started {name:8} http://127.0.0.1:{port}")

        print("\nEverything is running.\n"
              f"  Chat page:       http://127.0.0.1:{CHAT}\n"
              f"  Bank sample data: http://127.0.0.1:{BANK}/admin/data   (sign-in: nino / nino)\n"
              "  Press Ctrl+C here to stop everything.\n")
        webbrowser.open(f"http://127.0.0.1:{CHAT}")
        while all(process.poll() is None for _, process in running):
            time.sleep(0.5)
        for name, process in running:
            if process.poll() is not None:
                print(f"The {name} program stopped. See logs/{name}.log")
        return 1
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        for _, process in running:
            if process.poll() is None:
                process.terminate()
        for _, process in running:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    sys.exit(main())
