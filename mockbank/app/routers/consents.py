"""Consent and approval calls (built in step 3).

Planned here:
  POST   /v1/consents                       create a consent
  GET    /v1/consents/{id}                  read it
  GET    /v1/consents/{id}/status           check its status
  DELETE /v1/consents/{id}                  revoke it
  POST   /v1/consents/{id}/authorisations   start the customer's approval
plus the mock bank approval page and the token step.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["Consents"])
