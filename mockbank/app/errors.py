"""Error responses.

Accoding to standard's OpenAPI file (Georgian version 0.8):

  * Errors come as {"tppMessages": [{"category", "code", "path", "text"}]}.
    The standard also allows a second style (application/problem+json), but
    we only use this one.
  * Each HTTP status has its own list of allowed codes (STANDARD_CODES below,
    copied from the file).
  * The standard says a 500 or 503 has NO body, because it defines no codes
    for them. The same goes for a 429 on a payment call. The mock follows
    that: those answers carry only the status.

BankError refuses a code that is not allowed for its status, so a typo or a
made-up code fails in our own tests instead of leaking out.
"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

# Codes the standard allows, per HTTP status (account and payment calls combined).
STANDARD_CODES: dict[int, set[str]] = {
    400: {
        "FORMAT_ERROR",
        "PARAMETER_NOT_CONSISTENT",
        "PARAMETER_NOT_SUPPORTED",
        "SERVICE_INVALID",
        "RESOURCE_UNKNOWN",
        "RESOURCE_EXPIRED",
        "RESOURCE_BLOCKED",
        "TIMESTAMP_INVALID",
        "PERIOD_INVALID",
        "SCA_METHOD_UNKNOWN",
        "SCA_INVALID",
        "CONSENT_UNKNOWN",
        "SESSIONS_NOT_SUPPORTED",  # account calls only
        "PAYMENT_FAILED",
        "EXECUTION_DATE_INVALID",  # payment calls only
    },
    401: {
        "CERTIFICATE_INVALID",
        "ROLE_INVALID",
        "CERTIFICATE_EXPIRED",
        "CERTIFICATE_BLOCKED",
        "CERTIFICATE_REVOKE",
        "CERTIFICATE_MISSING",
        "SIGNATURE_INVALID",
        "SIGNATURE_MISSING",
        "CORPORATE_ID_INVALID",
        "PSU_CREDENTIALS_INVALID",
        "CONSENT_INVALID",
        "CONSENT_EXPIRED",
        "TOKEN_UNKNOWN",
        "TOKEN_INVALID",
        "TOKEN_EXPIRED",
        "KID_MISSING",  # payment calls only
    },
    403: {
        "CONSENT_UNKNOWN",
        "SERVICE_BLOCKED",
        "RESOURCE_UNKNOWN",
        "RESOURCE_EXPIRED",
        "PRODUCT_INVALID",  # payment calls only
    },
    404: {"RESOURCE_UNKNOWN", "PRODUCT_UNKNOWN"},  # PRODUCT_UNKNOWN: payment calls only
    405: {"SERVICE_INVALID", "CANCELLATION_INVALID"},
    406: {"REQUESTED_FORMATS_INVALID"},
    409: {"STATUS_INVALID"},
    429: {"ACCESS_EXCEEDED"},  # account calls only
}

# Statuses where the standard defines no codes, so the answer has no body.
NO_BODY_STATUSES = {500, 503}


class BankError(Exception):
    """Raise this anywhere to send a standard error response.

    Give a code for statuses that have codes. Leave it out (None) when the
    answer should have no body: always for 500 and 503, and for a 429 on a
    payment call.
    """

    def __init__(
        self,
        status_code: int,
        code: str | None = None,
        text: str = "",
        category: str = "ERROR",
    ):
        if status_code in NO_BODY_STATUSES and code is not None:
            raise ValueError(
                f"The standard defines no codes for {status_code}; leave code out."
            )
        if code is not None and code not in STANDARD_CODES.get(status_code, set()):
            raise ValueError(f"{code!r} is not a standard code for HTTP {status_code}.")
        self.status_code = status_code
        self.code = code
        self.text = text
        self.category = category


def _body(category: str, code: str, text: str) -> dict:
    # The standard limits the text to 500 characters.
    return {"tppMessages": [{"category": category, "code": code, "text": text[:500]}]}


async def bank_error_handler(request: Request, exc: BankError) -> Response:
    """Turns a BankError into the standard error answer."""
    if exc.code is None:
        return Response(status_code=exc.status_code)  # no body, as the standard says
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(exc.category, exc.code, exc.text),
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """A badly formed request becomes a 400 with the standard code FORMAT_ERROR."""
    # Keep the message short and readable: "field: problem; field: problem".
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
    )
    return JSONResponse(
        status_code=400, content=_body("ERROR", "FORMAT_ERROR", problems)
    )
