"""Reading HTML form data without an extra package.

Browsers send forms as "a=1&b=2" text. FastAPI's own Form() needs the
python-multipart package; this tiny helper avoids adding a dependency.
"""

from urllib.parse import parse_qs

from fastapi import Request


async def read_form(request: Request) -> dict[str, list[str]]:
    """Return the submitted form as {name: [values]}. A name can repeat (checkboxes)."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return parse_qs(raw, keep_blank_values=True)


def first(form: dict[str, list[str]], name: str, default: str = "") -> str:
    """The first value of a field, or a default if the field is missing."""
    values = form.get(name)
    return values[0] if values else default
