"""Account information calls (built in step 4).

Planned here:
  GET /v1/accounts                                   list accounts
  GET /v1/accounts/{id}                              account details
  GET /v1/accounts/{id}/balances                     balances
  GET /v1/accounts/{id}/transactions                 transactions
  GET /v1/accounts/{id}/transactions/{transactionId} one transaction
Each one needs a valid token and consent.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["Accounts"])
