# Тесты: покупки
"""Tests for the purchase endpoints."""
import asyncio
import uuid

import pytest

from tests.conftest import IS_SQLITE

OP_URL = "/api/v1/wallets/{wid}/operation"
GET_URL = "/api/v1/wallets/{wid}"
BUY_URL = "/api/v1/wallets/{wid}/purchase"
CHECK_URL = "/api/v1/wallets/{wid}/purchase/check"

# Five units for a total of 950: 2x200 + 1x300 + 2x125 = 950
CART_950 = {
    "items": [
        {"name": "Notebook", "price": 200, "quantity": 2},
        {"name": "Backpack", "price": 300, "quantity": 1},
        {"name": "Pen set", "price": 125, "quantity": 2},
    ]
}


async def _topup(client, wid, amount):
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "DEPOSIT", "amount": amount},
    )
    assert resp.status_code == 200
    return resp


async def test_purchase_five_items_for_950(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(BUY_URL.format(wid=wid), json=CART_950)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 950
    assert body["items_count"] == 5
    assert body["positions"] == 3
    assert body["balance"] == 50

    # The debit is durable, not just reported.
    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.json()["balance"] == 50


async def test_purchase_rejected_when_balance_too_low(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 900)

    resp = await client.post(BUY_URL.format(wid=wid), json=CART_950)
    assert resp.status_code == 400
    assert "short by 50" in resp.json()["detail"]

    # Nothing was charged.
    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.json()["balance"] == 900


async def test_purchase_exact_balance_succeeds(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 950)

    resp = await client.post(BUY_URL.format(wid=wid), json=CART_950)
    assert resp.status_code == 200
    assert resp.json()["balance"] == 0


async def test_client_total_is_not_trusted(client):
    """A client claiming a cheaper total must not be charged less."""
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    payload = dict(CART_950, expected_total=10)
    resp = await client.post(BUY_URL.format(wid=wid), json=payload)
    assert resp.status_code == 409

    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.json()["balance"] == 1000


async def test_matching_expected_total_is_accepted(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    payload = dict(CART_950, expected_total=950)
    resp = await client.post(BUY_URL.format(wid=wid), json=payload)
    assert resp.status_code == 200
    assert resp.json()["balance"] == 50


async def test_fractional_prices_are_summed_exactly(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 100)

    payload = {
        "items": [
            {"name": "Coffee", "price": 3.33, "quantity": 3},
            {"name": "Muffin", "price": 0.01, "quantity": 1},
        ]
    }
    resp = await client.post(BUY_URL.format(wid=wid), json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 10.0
    assert body["balance"] == 90.0


async def test_purchase_missing_wallet_404(client):
    resp = await client.post(
        BUY_URL.format(wid=uuid.uuid4()),
        json=CART_950,
    )
    assert resp.status_code == 404


async def test_empty_cart_422(client):
    wid = uuid.uuid4()
    resp = await client.post(BUY_URL.format(wid=wid), json={"items": []})
    assert resp.status_code == 422


@pytest.mark.parametrize("quantity", [0, -1])
async def test_non_positive_quantity_422(client, quantity):
    wid = uuid.uuid4()
    payload = {"items": [{"name": "X", "price": 10, "quantity": quantity}]}
    resp = await client.post(BUY_URL.format(wid=wid), json=payload)
    assert resp.status_code == 422


async def test_check_reports_affordable(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(CHECK_URL.format(wid=wid), json=CART_950)
    assert resp.status_code == 200
    body = resp.json()
    assert body["affordable"] is True
    assert body["total"] == 950
    assert body["shortfall"] == 0

    # A check must not move money.
    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.json()["balance"] == 1000


async def test_check_reports_shortfall(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 700)

    resp = await client.post(CHECK_URL.format(wid=wid), json=CART_950)
    assert resp.status_code == 200
    body = resp.json()
    assert body["affordable"] is False
    assert body["shortfall"] == 250


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "True row-level locking needs Postgres. "
        "Set TEST_DATABASE_URL to a Postgres DSN to run this test."
    ),
)
async def test_concurrent_purchases_cannot_overspend(client):
    """Two simultaneous 950 purchases against 1000: exactly one wins.

    This is the case a check-then-charge design gets wrong: both requests
    would see 1000, both would pass the check, and the balance would end
    up negative.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    responses = await asyncio.gather(
        client.post(BUY_URL.format(wid=wid), json=CART_950),
        client.post(BUY_URL.format(wid=wid), json=CART_950),
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 400]

    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.json()["balance"] == 50


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "True row-level locking needs Postgres. "
        "Set TEST_DATABASE_URL to a Postgres DSN to run this test."
    ),
)
async def test_many_concurrent_purchases_never_go_negative(client):
    """20 parallel purchases of 950 against 5000: exactly 5 succeed."""
    wid = uuid.uuid4()
    await _topup(client, wid, 5000)

    responses = await asyncio.gather(
        *[
            client.post(BUY_URL.format(wid=wid), json=CART_950)
            for _ in range(20)
        ]
    )
    ok = [r for r in responses if r.status_code == 200]
    rejected = [r for r in responses if r.status_code == 400]
    assert len(ok) == 5
    assert len(rejected) == 15

    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.json()["balance"] == 250
