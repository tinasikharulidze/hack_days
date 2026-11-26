"""What the layer remembers, kept in one small JSON file so a restart loses nothing.

  connection   the bank connection: consent id, tokens, link waiting for the customer
  payments     one record per payment intent (the heart of the duplicate protection)

Only one process should use the file at a time. Writes are atomic (write a
temporary file, then rename), so a crash never leaves half a file.
"""

import json
import os
from pathlib import Path


class State:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None        # None = keep in memory only (used by tests)
        self.connection: dict = {"status": "none"}
        self.payments: dict[str, dict] = {}
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text())
            self.connection = data.get("connection", self.connection)
            self.payments = data.get("payments", {})

    def save(self) -> None:
        if not self.path:
            return
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"connection": self.connection, "payments": self.payments}, indent=2))
        os.replace(temporary, self.path)
