"""Talks to the bank. Everything about tokens and bank error codes stays in here.

The rest of the layer calls simple methods (create_consent, get_json, start_payment...)
and gets either plain data or a BankProblem. A BankProblem says what KIND of
trouble it was, because the kind decides what the agent should be told:

  unreachable   no answer, a timeout, or a 500/503. We do NOT know if the bank did the work.
  rate_limited  the bank says too many reads today (429). Retrying now will not help.
  connection    the customer's permission is gone (expired, withdrawn, tokens dead).
                Only a new connect_bank helps.
  refused       the bank said no to this request (wrong input, unknown account, no money...).
                The bank's own words are kept in .text.

Tokens: the access token is short-lived. When the bank answers 401 with a token
code, we swap the refresh token for a new access token and repeat the call once.
The agent never notices.
"""

import uuid
from datetime import date, timedelta

import httpx

from paygate.config import Config
from paygate.state import State

_TOKEN_CODES = {"TOKEN_EXPIRED", "TOKEN_INVALID", "TOKEN_UNKNOWN"}
_CONSENT_CODES = {"CONSENT_INVALID", "CONSENT_EXPIRED", "CONSENT_UNKNOWN"}


class BankProblem(Exception):
    def __init__(self, kind: str, status: int | None = None, code: str = "", text: str = ""):
        super().__init__(f"{kind}: HTTP {status} {code} {text}".strip())
        self.kind = kind
        self.status = status
        self.code = code
        self.text = text


def new_request_id() -> str:
    return str(uuid.uuid4())


class BankClient:
    def __init__(self, config: Config, state: State, http: httpx.AsyncClient | None = None):
        self.config = config
        self.state = state
        # An outside client can be passed in (the tests pass one wired straight to the mock).
        self.http = http or httpx.AsyncClient(
            base_url=config.bank_url, timeout=config.bank_timeout_seconds, follow_redirects=False
        )

    # ---------- low level ----------
    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One HTTP call. Network trouble and 5xx both become 'unreachable'."""
        try:
            response = await self.http.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise BankProblem("unreachable", text=f"{type(error).__name__}") from error
        if response.status_code >= 500:
            raise BankProblem("unreachable", status=response.status_code)
        return response

    @staticmethod
    def _error_of(response: httpx.Response) -> tuple[str, str]:
        """The code and text from a standard error answer (or from an OAuth one)."""
        try:
            body = response.json()
        except ValueError:
            return "", ""
        if "tppMessages" in body and body["tppMessages"]:
            first = body["tppMessages"][0]
            return first.get("code", ""), first.get("text", "")
        return body.get("error", ""), body.get("error_description", "")

    def _raise_for(self, response: httpx.Response) -> None:
        """Turn a 4xx answer into the right kind of BankProblem."""
        code, text = self._error_of(response)
        if response.status_code == 429:
            raise BankProblem("rate_limited", 429, code, text)
        if code in _CONSENT_CODES or code in _TOKEN_CODES:
            self.state.connection["status"] = "expired"
            self.state.save()
            raise BankProblem("connection", response.status_code, code, text)
        raise BankProblem("refused", response.status_code, code, text)

    def _auth_headers(self, request_id: str | None = None) -> dict:
        connection = self.state.connection
        if connection.get("status") != "connected":
            raise BankProblem("connection", text="not connected")
        return {
            "Authorization": f"Bearer {connection['access_token']}",
            "Consent-ID": connection["consent_id"],
            "X-Request-ID": request_id or new_request_id(),
        }

    async def _authorised(self, method: str, path: str, request_id: str | None = None,
                          extra_headers: dict | None = None, **kwargs) -> httpx.Response:
        """A call that needs the customer's permission. Renews the access token once if needed."""
        for attempt in (1, 2):
            headers = {**self._auth_headers(request_id), **(extra_headers or {})}
            response = await self._send(method, path, headers=headers, **kwargs)
            code, _ = self._error_of(response) if response.status_code == 401 else ("", "")
            if code in _TOKEN_CODES and attempt == 1:
                await self.refresh()          # then loop and repeat the call with the new token
                continue
            if response.status_code >= 400:
                self._raise_for(response)
            return response
        raise AssertionError("unreachable")

    # ---------- the permission (consent + tokens) ----------
    async def create_consent(self, ibans: list[str]) -> dict:
        """Ask the bank for permission to read these accounts. Returns the consent id and the customer's link."""
        access = [{"iban": iban} for iban in ibans]
        response = await self._send("POST", "/v1/consents", headers={
            "X-Request-ID": new_request_id(),
            "PSU-IP-Address": self.config.psu_ip_address,
            "TPP-Redirect-URI": self.config.callback_url,
        }, json={
            "access": {"accounts": access, "balances": access, "transactions": access},
            "recurringIndicator": True,
            "validUntil": (date.today() + timedelta(days=self.config.consent_days)).isoformat(),
            "frequencyPerDay": self.config.reads_per_day,
            "combinedServiceIndicator": False,
        })
        if response.status_code >= 400:
            self._raise_for(response)
        body = response.json()
        return {"consent_id": body["consentId"], "link": body["_links"]["scaRedirect"]["href"]}

    async def exchange_code(self, code: str) -> dict:
        """Swap the one-time code from the redirect for tokens."""
        response = await self._send("POST", "/oauth/token",
                                    data={"grant_type": "authorization_code", "code": code})
        if response.status_code >= 400:
            raise BankProblem("refused", response.status_code, *self._error_of(response))
        return response.json()

    async def refresh(self) -> None:
        """Get a new access token with the refresh token. If that fails the permission is gone."""
        response = await self._send("POST", "/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": self.state.connection.get("refresh_token", "")})
        if response.status_code >= 400:
            self.state.connection["status"] = "expired"
            self.state.save()
            raise BankProblem("connection", response.status_code, *self._error_of(response))
        self.state.connection["access_token"] = response.json()["access_token"]
        self.state.save()

    # ---------- reading ----------
    async def get_json(self, path: str, params: dict | None = None) -> dict:
        return (await self._authorised("GET", path, params=params)).json()

    # ---------- payments ----------
    async def start_payment(self, body: dict, request_id: str) -> dict:
        """Start a domestic payment. The same request_id is used again on every retry of one intent."""
        response = await self._authorised("POST", "/v1/payments/domestic", request_id=request_id, extra_headers={
            "PSU-IP-Address": self.config.psu_ip_address,
            "TPP-Redirect-URI": self.config.callback_url,
        }, json=body)
        return response.json()

    async def payment_status(self, bank_payment_id: str) -> str:
        response = await self._authorised("GET", f"/v1/payments/domestic/{bank_payment_id}/status")
        return response.json()["transactionStatus"]

    async def cancel_payment(self, bank_payment_id: str) -> None:
        await self._authorised("DELETE", f"/v1/payments/domestic/{bank_payment_id}")
