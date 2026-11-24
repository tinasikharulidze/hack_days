"""The mock bank's entry point.

Run it from the mockbank folder with:
    uvicorn app.main:app --reload
Then open http://127.0.0.1:8000/docs to see and try every call.
"""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.errors import BankError, bank_error_handler, validation_error_handler
from app.routers import accounts, admin, consents, payments


def create_app() -> FastAPI:
    """Build the app. A function, so tests can make a fresh copy."""
    app = FastAPI(
        title="Mock Georgian open banking API",
        description=(
            "A mock bank that follows the Georgian open banking standard "
            "(Berlin Group NextGenPSD2). For testing only: no real money, no real customers."
        ),
        version="0.1.0",
    )

    # Every error leaves the mock in the standard's error format.
    app.add_exception_handler(BankError, bank_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    app.include_router(consents.router)
    app.include_router(accounts.router)
    app.include_router(payments.router)
    app.include_router(admin.router)

    @app.get("/health", tags=["Admin (not part of the standard)"])
    def health():
        """Is the mock running?"""
        return {"status": "ok"}

    return app


app = create_app()
