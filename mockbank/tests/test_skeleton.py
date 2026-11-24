"""Tests for the step 2 skeleton: sample data, settings, faults, clock, errors."""

from decimal import Decimal

from fastapi import Depends

from app import clock, faults, store
from app.faults import fault_before


# ---------- Sample data ----------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_two_customers_and_five_accounts(client):
    data = client.get("/admin/data").json()
    assert len(data["customers"]) == 2
    assert len(data["accounts"]) == 5
    assert {a["currency"] for a in data["accounts"]} == {"GEL", "USD", "EUR"}


def test_ibans_are_valid_georgian_ibans(client):
    for account in client.get("/admin/data").json()["accounts"]:
        iban = account["iban"]
        assert iban.startswith("GE") and len(iban) == 22
        # A valid IBAN leaves remainder 1 when moved-around and divided by 97.
        moved = iban[4:] + iban[:4]
        assert int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def test_available_balance_includes_pending(client):
    for account_id in store.STATE.accounts:
        result = store.balances(account_id)
        pending = sum(
            t.amount for t in store.STATE.transactions
            if t.account_id == account_id and t.status == "pending"
        )
        assert result["interimAvailable"] == result["interimBooked"] + pending


def test_no_account_starts_overdrawn():
    # Sample accounts should look healthy, so an app under test sees normal balances.
    for account_id in store.STATE.accounts:
        assert store.balances(account_id)["interimAvailable"] > Decimal("0")


def test_pending_transactions_have_no_booking_date():
    for t in store.STATE.transactions:
        assert (t.booking_date is None) == (t.status == "pending")


def test_sample_data_is_the_same_every_time():
    first = [(t.amount, t.remittance) for t in store.STATE.transactions]
    store.reset_all()
    second = [(t.amount, t.remittance) for t in store.STATE.transactions]
    assert first == second


# ---------- Settings ----------

def test_default_settings(client):
    body = client.get("/admin/settings").json()
    assert body["settings"]["consent_validity_days"] == 90
    assert body["settings"]["max_consent_requests_per_day"] == 4
    assert body["status"]["payment_approval_timeout_minutes"] == "not confirmed"


def test_change_settings_and_reset(client):
    r = client.put("/admin/settings", json={"max_consent_requests_per_day": 2, "auto_approve": True})
    assert r.status_code == 200
    assert client.get("/admin/settings").json()["settings"]["max_consent_requests_per_day"] == 2
    client.post("/admin/reset")
    assert client.get("/admin/settings").json()["settings"]["max_consent_requests_per_day"] == 4


def test_unknown_setting_is_rejected_and_nothing_changes(client):
    r = client.put("/admin/settings", json={"max_consent_request_per_day": 2})  # typo
    assert r.status_code == 422
    assert client.get("/admin/settings").json()["settings"]["max_consent_requests_per_day"] == 4


# ---------- Faults ----------

def _add_test_routes(app):
    """Two throwaway routes that use the fault hooks, since real endpoints come later."""
    work_done = []

    @app.get("/test/before", dependencies=[Depends(fault_before("accounts.list"))])
    async def before():
        work_done.append("before")
        return {"ok": True}

    @app.get("/test/after")
    async def after():
        work_done.append("after")  # the bank does the work first...
        await faults.run("payments.initiate", "after")  # ...then maybe fails
        return {"ok": True}

    return work_done


def test_server_error_before_stops_the_work(app, client):
    work_done = _add_test_routes(app)
    client.post("/admin/faults", json={"kind": "server_error", "operation": "accounts.list"})
    r = client.get("/test/before")
    assert r.status_code == 503
    assert r.content == b""  # the standard defines no body for a 503
    assert work_done == []  # nothing happened


def test_server_error_after_happens_after_the_work(app, client):
    work_done = _add_test_routes(app)
    client.post("/admin/faults", json={
        "kind": "server_error", "operation": "payments.initiate", "phase": "after"})
    r = client.get("/test/after")
    assert r.status_code == 503
    assert work_done == ["after"]  # the work was done, yet the caller saw an error


def test_fault_only_hits_its_own_operation(app, client):
    _add_test_routes(app)
    client.post("/admin/faults", json={"kind": "server_error", "operation": "payments.*"})
    assert client.get("/test/before").status_code == 200  # accounts.list is untouched


def test_fault_with_times_runs_out(app, client):
    _add_test_routes(app)
    client.post("/admin/faults", json={"kind": "server_error", "operation": "accounts.list", "times": 1})
    assert client.get("/test/before").status_code == 503
    assert client.get("/test/before").status_code == 200  # used up
    assert client.get("/admin/faults").json()["faults"] == []


def test_wildcard_operation_matches_a_group(app, client):
    _add_test_routes(app)
    client.post("/admin/faults", json={"kind": "server_error", "operation": "accounts.*"})
    assert client.get("/test/before").status_code == 503


def test_unknown_operation_is_rejected(client):
    r = client.post("/admin/faults", json={"kind": "slow", "operation": "accounts.nothing"})
    assert r.status_code == 422


def test_switch_fault_is_taken_once(client):
    client.post("/admin/faults", json={
        "kind": "payment_stays_pending", "operation": "payments.status", "times": 1})
    assert faults.take("payment_stays_pending", "payments.status") is not None
    assert faults.take("payment_stays_pending", "payments.status") is None


def test_remove_fault(client):
    fault_id = client.post("/admin/faults", json={"kind": "slow", "delay_seconds": 1}).json()["id"]
    assert client.delete(f"/admin/faults/{fault_id}").status_code == 200
    assert client.delete(f"/admin/faults/{fault_id}").status_code == 404


# ---------- Clock and errors ----------

def test_clock_advance_and_reset(client):
    before = clock.now()
    client.post("/admin/time/advance", json={"minutes": 90})
    assert (clock.now() - before).total_seconds() >= 90 * 60
    client.post("/admin/reset")
    assert (clock.now() - before).total_seconds() < 60


def test_429_on_account_calls_has_the_standard_code(app, client):
    _add_test_routes(app)
    client.post("/admin/faults", json={"kind": "server_error", "operation": "accounts.list", "status_code": 429})
    r = client.get("/test/before")
    assert r.status_code == 429
    assert r.json()["tppMessages"][0]["code"] == "ACCESS_EXCEEDED"


def test_429_on_payment_calls_has_no_body(app, client):
    _add_test_routes(app)
    client.post("/admin/faults", json={
        "kind": "server_error", "operation": "payments.initiate", "phase": "after", "status_code": 429})
    r = client.get("/test/after")
    assert r.status_code == 429
    assert r.content == b""  # the standard defines no body for a payment 429


def test_fault_status_must_be_429_500_or_503(client):
    r = client.post("/admin/faults", json={"kind": "server_error", "status_code": 404})
    assert r.status_code == 400


def test_bank_error_refuses_codes_the_standard_does_not_have():
    import pytest
    from app.errors import BankError

    BankError(400, "CONSENT_UNKNOWN")  # allowed
    with pytest.raises(ValueError):
        BankError(400, "CONSENT_NOT_FOUND")  # made up
    with pytest.raises(ValueError):
        BankError(404, "CONSENT_UNKNOWN")  # real code, but not allowed for 404
    with pytest.raises(ValueError):
        BankError(503, "SERVICE_UNAVAILABLE")  # the standard has no codes for 503
    BankError(503)  # fine: no code


def test_bad_request_uses_the_standard_error_shape(client):
    r = client.post("/admin/time/advance", json={"minutes": "soon"})
    assert r.status_code == 400
    message = r.json()["tppMessages"][0]
    assert message["category"] == "ERROR" and message["code"] == "FORMAT_ERROR"
