# Тесты: идемпотентность повторов
"""Tests for idempotent retries.

The scenario throughout: a client sends a request, the connection drops
before the response arrives, and the client retries. It cannot know
whether the first attempt was applied, so retrying must be safe.
"""
import asyncio
import uuid

import pytest

from tests.conftest import IS_SQLITE

OP_URL = "/api/v1/wallets/{wid}/operation"
GET_URL = "/api/v1/wallets/{wid}"
HIST_URL = "/api/v1/wallets/{wid}/operations"
BUY_URL = "/api/v1/wallets/{wid}/purchase"
REFUND_URL = "/api/v1/wallets/{wid}/purchases/{pid}/refund"

CART_950 = [
    {"name": "Notebook", "price": 200, "quantity": 2},
    {"name": "Backpack", "price": 300, "quantity": 1},
    {"name": "Pen set", "price": 125, "quantity": 2},
]


def key(name="k"):
    return f"{name}-{uuid.uuid4()}"


async def _deposit(client, wid, amount, idem=None):
    headers = {"Idempotency-Key": idem} if idem else None
    return await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "DEPOSIT", "amount": amount},
        headers=headers,
    )


async def _balance(client, wid):
    resp = await client.get(GET_URL.format(wid=wid))
    return resp.json()["balance"]


async def _count_ops(client, wid):
    resp = await client.get(HIST_URL.format(wid=wid), params={"limit": 200})
    return resp.json()["count"]


async def test_retried_deposit_is_applied_once(client):
    wid = uuid.uuid4()
    k = key()

    first = await _deposit(client, wid, 1000, k)
    second = await _deposit(client, wid, 1000, k)

    assert first.status_code == 200
    assert second.status_code == 200
    # The retry gets the original answer, not a second credit.
    assert first.json() == second.json()
    assert await _balance(client, wid) == 1000
    assert await _count_ops(client, wid) == 1


async def test_without_a_key_a_repeat_is_a_second_operation(client):
    """Idempotency is opt-in: no key means the request is taken at face
    value, which is what a genuinely new deposit needs."""
    wid = uuid.uuid4()

    await _deposit(client, wid, 1000)
    await _deposit(client, wid, 1000)

    assert await _balance(client, wid) == 2000
    assert await _count_ops(client, wid) == 2


async def test_different_keys_are_different_requests(client):
    wid = uuid.uuid4()

    await _deposit(client, wid, 1000, key("a"))
    await _deposit(client, wid, 1000, key("b"))

    assert await _balance(client, wid) == 2000


async def test_same_key_with_a_different_body_is_rejected(client):
    """Reusing a key for another request is a client bug, not a retry."""
    wid = uuid.uuid4()
    k = key()

    assert (await _deposit(client, wid, 1000, k)).status_code == 200

    resp = await _deposit(client, wid, 5000, k)
    assert resp.status_code == 422
    assert "different request" in resp.json()["detail"]

    # Nothing extra was credited.
    assert await _balance(client, wid) == 1000


async def test_retried_purchase_charges_once(client):
    wid = uuid.uuid4()
    await _deposit(client, wid, 1000)
    k = key()
    body = {"items": CART_950, "promo_code": "SALE10"}

    first = await client.post(
        BUY_URL.format(wid=wid),
        json=body,
        headers={"Idempotency-Key": k},
    )
    second = await client.post(
        BUY_URL.format(wid=wid),
        json=body,
        headers={"Idempotency-Key": k},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    # Same purchase, not a duplicate order.
    assert first.json()["purchase_id"] == second.json()["purchase_id"]
    assert await _balance(client, wid) == 145


async def test_retried_purchase_does_not_burn_a_second_coupon_use(client):
    """A retry must not consume the coupon twice either."""
    wid = uuid.uuid4()
    await _deposit(client, wid, 5000)
    k = key()
    body = {"items": CART_950, "promo_code": "ONCE"}

    for _ in range(3):
        resp = await client.post(
            BUY_URL.format(wid=wid),
            json=body,
            headers={"Idempotency-Key": k},
        )
        assert resp.status_code == 200

    assert await _balance(client, wid) == 4145

    # The one-per-wallet allowance is still intact for a genuinely new
    # purchase, because only one use was ever consumed... and now spent.
    resp = await client.post(
        BUY_URL.format(wid=wid),
        json=body,
        headers={"Idempotency-Key": key()},
    )
    assert resp.status_code == 409


async def test_retried_refund_pays_out_once(client):
    wid = uuid.uuid4()
    await _deposit(client, wid, 1000)
    resp = await client.post(BUY_URL.format(wid=wid), json={"items": CART_950})
    pid = resp.json()["purchase_id"]
    k = key()

    first = await client.post(
        REFUND_URL.format(wid=wid, pid=pid),
        json={"amount": 300},
        headers={"Idempotency-Key": k},
    )
    second = await client.post(
        REFUND_URL.format(wid=wid, pid=pid),
        json={"amount": 300},
        headers={"Idempotency-Key": k},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    # 1000 - 950 + 300, refunded once.
    assert await _balance(client, wid) == 350


async def test_a_failed_request_does_not_burn_the_key(client):
    """If the work rolled back, the same key must still be usable.

    The key is claimed inside the transaction, so a failure undoes the
    claim along with everything else - otherwise a client could lose a
    payment slot to a transient error.
    """
    wid = uuid.uuid4()
    await _deposit(client, wid, 100)
    k = key()

    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "WITHDRAW", "amount": 500},
        headers={"Idempotency-Key": k},
    )
    assert resp.status_code == 400

    await _deposit(client, wid, 1000)
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "WITHDRAW", "amount": 500},
        headers={"Idempotency-Key": k},
    )
    assert resp.status_code == 200
    assert await _balance(client, wid) == 600


async def test_keys_are_scoped_to_their_endpoint(client):
    """The same string on a different endpoint is a different request."""
    wid = uuid.uuid4()
    await _deposit(client, wid, 1000, "shared-key-1")

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950},
        headers={"Idempotency-Key": "shared-key-1"},
    )
    assert resp.status_code == 422


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "Needs real concurrent transactions. Set TEST_DATABASE_URL to a "
        "Postgres DSN to run this test."
    ),
)
async def test_simultaneous_retries_apply_once(client):
    """Two retries in flight at the same moment.

    The primary key on the idempotency table makes the second transaction
    wait for the first, so it takes the replay path instead of charging.
    """
    wid = uuid.uuid4()
    k = key()

    responses = await asyncio.gather(
        _deposit(client, wid, 1000, k),
        _deposit(client, wid, 1000, k),
    )
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()
    assert await _balance(client, wid) == 1000
    assert await _count_ops(client, wid) == 1
