"""Payment calls (built in step 5).

Planned here:
  POST   /v1/payments/{product}                       start a payment
  GET    /v1/payments/{product}/{id}                  read it
  GET    /v1/payments/{product}/{id}/status           check its status
  DELETE /v1/payments/{product}/{id}                  cancel it
  POST   /v1/payments/{product}/{id}/authorisations   start the customer's approval
Only single domestic payments are supported.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["Payments"])
