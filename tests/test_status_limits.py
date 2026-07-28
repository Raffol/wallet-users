# Тесты: статус кошелька и потолок баланса
"""Tests for wallet status and the balance ceiling."""
import uuid

import pytest

OP_URL = "/api/v1/wallets/{wid}/operation"
GET_URL = "/api/v1/wallets/{wid}"
STATUS_URL = "/api/v1/wallets/{wid}/status"
BUY_URL = "/api/v1/wallets/{wid}/purchase"
REFUND_URL = "/api/v1/wallets/{wid}/purchases/{pid}/refund"

CART_950 = [
    {"name": "Notebook", "price": 200, "quantity": 2},
    {"name": "Backpack", "price": 300, "quantity": 1},
    {"name": "Pen set", "price": 125, "quantity": 2},
]

# The largest value NUMERIC(20, 2) holds. Sent as a string so the exact
# decimal survives: as a JSON float it would be rounded on the way in.
MAX_BALANCE = "999999999999999999.99"


async def _op(client, wid, kind, amount):
    return await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": kind, "amount": amount},
    )


async def _set_status(client, wid, status):
    return await client.post(
        STATUS_URL.format(wid=wid),
        json={"status": status},
    )


async def _balance(client, wid):
    resp = await client.get(GET_URL.format(wid=wid))
    return resp.json()["balance"]


# --- freezing ----------------------------------------------------------


async def test_frozen_wallet_refuses_withdrawal(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)

    resp = await _set_status(client, wid, "FROZEN")
    assert resp.status_code == 200
    assert resp.json()["status"] == "FROZEN"

    resp = await _op(client, wid, "WITHDRAW", 100)
    assert resp.status_code == 409
    assert "frozen" in resp.json()["detail"]
    assert await _balance(client, wid) == 1000


async def test_frozen_wallet_refuses_purchase(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)
    await _set_status(client, wid, "FROZEN")

    resp = await client.post(BUY_URL.format(wid=wid), json={"items": CART_950})
    assert resp.status_code == 409
    assert await _balance(client, wid) == 1000


async def test_frozen_wallet_still_accepts_money(client):
    """Freezing stops spending, not receiving.

    A freeze exists to keep money from leaving an account under dispute.
    Blocking incoming refunds would trap the customer's money rather than
    protect it, so credits are deliberately still allowed.
    """
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)
    resp = await client.post(BUY_URL.format(wid=wid), json={"items": CART_950})
    pid = resp.json()["purchase_id"]

    await _set_status(client, wid, "FROZEN")

    assert (await _op(client, wid, "DEPOSIT", 500)).status_code == 200
    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 200

    # 1000 - 950 + 500 + 950
    assert await _balance(client, wid) == 1500


async def test_unfreezing_restores_spending(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)
    await _set_status(client, wid, "FROZEN")
    assert (await _op(client, wid, "WITHDRAW", 100)).status_code == 409

    resp = await _set_status(client, wid, "ACTIVE")
    assert resp.json()["status"] == "ACTIVE"

    assert (await _op(client, wid, "WITHDRAW", 100)).status_code == 200
    assert await _balance(client, wid) == 900


async def test_new_wallets_are_active(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)
    assert (await _op(client, wid, "WITHDRAW", 1)).status_code == 200


async def test_status_of_unknown_wallet_404(client):
    resp = await _set_status(client, uuid.uuid4(), "FROZEN")
    assert resp.status_code == 404


@pytest.mark.parametrize("status", ["BLOCKED", "frozen", "", 1])
async def test_invalid_status_422(client, status):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 100)
    resp = await _set_status(client, wid, status)
    assert resp.status_code == 422


# --- balance ceiling ---------------------------------------------------


async def test_deposit_past_the_ceiling_is_rejected(client):
    """Two maximum deposits used to overflow NUMERIC(20, 2).

    The database would raise a numeric overflow, which reaches the client
    as an opaque 500. The ceiling is checked instead, so the answer is a
    400 that says what went wrong.
    """
    wid = uuid.uuid4()
    assert (await _op(client, wid, "DEPOSIT", MAX_BALANCE)).status_code == 200

    resp = await _op(client, wid, "DEPOSIT", "0.01")
    assert resp.status_code == 400
    assert "Balance limit exceeded" in resp.json()["detail"]


async def test_ceiling_leaves_the_balance_untouched(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", "1000.00")
    await _op(client, wid, "DEPOSIT", MAX_BALANCE)

    # The rejected deposit changed nothing, so the wallet can still spend.
    resp = await _op(client, wid, "WITHDRAW", "1000.00")
    assert resp.status_code == 200


async def test_amount_above_the_column_width_is_rejected_by_schema(client):
    """Beyond the ceiling the request never reaches the database at all."""
    wid = uuid.uuid4()
    resp = await _op(client, wid, "DEPOSIT", "9999999999999999999999.00")
    assert resp.status_code == 422


async def test_amount_with_three_decimals_is_rejected(client):
    wid = uuid.uuid4()
    resp = await _op(client, wid, "DEPOSIT", "10.001")
    assert resp.status_code == 422
