# Тесты: возвраты
"""Tests for refunds."""
import asyncio
import uuid

import pytest

from tests.conftest import IS_SQLITE

OP_URL = "/api/v1/wallets/{wid}/operation"
GET_URL = "/api/v1/wallets/{wid}"
BUY_URL = "/api/v1/wallets/{wid}/purchase"
REFUND_URL = "/api/v1/wallets/{wid}/purchases/{pid}/refund"

# Five units for a subtotal of 950: 2x200 + 1x300 + 2x125
CART_950 = [
    {"name": "Notebook", "price": 200, "quantity": 2},
    {"name": "Backpack", "price": 300, "quantity": 1},
    {"name": "Pen set", "price": 125, "quantity": 2},
]


async def _topup(client, wid, amount):
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "DEPOSIT", "amount": amount},
    )
    assert resp.status_code == 200


async def _balance(client, wid):
    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.status_code == 200
    return resp.json()["balance"]


async def _buy(client, wid, promo_code=None):
    """Make a purchase and return its id and the charged total."""
    payload = {"items": CART_950}
    if promo_code is not None:
        payload["promo_code"] = promo_code
    resp = await client.post(BUY_URL.format(wid=wid), json=payload)
    assert resp.status_code == 200
    body = resp.json()
    return body["purchase_id"], body["total"]


async def test_purchase_returns_a_refundable_id(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    pid, total = await _buy(client, wid)
    assert uuid.UUID(pid)
    assert total == 950


async def test_full_refund_restores_balance(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid)
    assert await _balance(client, wid) == 50

    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["refunded"] == 950
    assert body["refunded_total"] == 950
    assert body["refundable_remaining"] == 0
    assert body["balance"] == 1000

    assert await _balance(client, wid) == 1000


async def test_refund_returns_charged_amount_not_subtotal(client):
    """The classic refund bug: paying back the pre-discount price.

    SALE10 charges 855 for a 950 cart. Refunding the subtotal would put
    1095 back into a wallet that started with 1000 - a 95 profit created
    out of nothing by buying and returning.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, total = await _buy(client, wid, promo_code="SALE10")
    assert total == 855

    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 200
    assert resp.json()["refunded"] == 855
    # Back to exactly the starting balance - not a cent more.
    assert await _balance(client, wid) == 1000


async def test_double_refund_rejected(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid)

    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 200
    assert await _balance(client, wid) == 1000

    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 409
    assert "nothing is left to refund" in resp.json()["detail"]
    # The second attempt paid out nothing.
    assert await _balance(client, wid) == 1000


async def test_partial_refunds_accumulate_to_the_charged_total(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid, promo_code="SALE10")

    resp = await client.post(
        REFUND_URL.format(wid=wid, pid=pid),
        json={"amount": 300},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["refunded"] == 300
    assert body["refunded_total"] == 300
    assert body["refundable_remaining"] == 555
    assert body["balance"] == 445

    # Omitting the amount refunds whatever is left.
    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["refunded"] == 555
    assert body["refunded_total"] == 855
    assert body["refundable_remaining"] == 0
    assert await _balance(client, wid) == 1000

    # And nothing beyond that.
    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 409


async def test_partial_refund_cannot_exceed_remaining(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid, promo_code="SALE10")

    await client.post(
        REFUND_URL.format(wid=wid, pid=pid),
        json={"amount": 300},
    )
    resp = await client.post(
        REFUND_URL.format(wid=wid, pid=pid),
        json={"amount": 600},
    )
    assert resp.status_code == 409
    assert "only 555.00 is left" in resp.json()["detail"]
    assert await _balance(client, wid) == 445


async def test_refund_larger_than_purchase_rejected(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid)

    resp = await client.post(
        REFUND_URL.format(wid=wid, pid=pid),
        json={"amount": 5000},
    )
    assert resp.status_code == 409
    assert await _balance(client, wid) == 50


async def test_free_purchase_has_nothing_to_refund(client):
    """CLEARANCE makes the cart free, so there is no money to return."""
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, total = await _buy(client, wid, promo_code="CLEARANCE")
    assert total == 0

    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 409
    assert await _balance(client, wid) == 1000


async def test_refund_unknown_purchase_404(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        REFUND_URL.format(wid=wid, pid=uuid.uuid4()),
    )
    assert resp.status_code == 404
    assert await _balance(client, wid) == 1000


async def test_cannot_refund_someone_elses_purchase(client):
    """A purchase may only be refunded to the wallet that paid for it."""
    buyer = uuid.uuid4()
    stranger = uuid.uuid4()
    await _topup(client, buyer, 1000)
    await _topup(client, stranger, 10)
    pid, _ = await _buy(client, buyer)

    resp = await client.post(REFUND_URL.format(wid=stranger, pid=pid))
    assert resp.status_code == 404

    # Neither wallet moved.
    assert await _balance(client, stranger) == 10
    assert await _balance(client, buyer) == 50


@pytest.mark.parametrize("amount", [0, -100])
async def test_non_positive_refund_amount_422(client, amount):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid)

    resp = await client.post(
        REFUND_URL.format(wid=wid, pid=pid),
        json={"amount": amount},
    )
    assert resp.status_code == 422
    assert await _balance(client, wid) == 50


async def test_refunded_money_can_be_spent_again(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid)

    await client.post(REFUND_URL.format(wid=wid, pid=pid))

    second_pid, _ = await _buy(client, wid)
    assert second_pid != pid
    assert await _balance(client, wid) == 50


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "True row-level locking needs Postgres. "
        "Set TEST_DATABASE_URL to a Postgres DSN to run this test."
    ),
)
async def test_concurrent_full_refunds_pay_out_once(client):
    """Two simultaneous refunds of one purchase: only one may pay out.

    Without the lock on the purchase row both requests would read
    refunded_total = 0, both would credit 855, and the wallet would end up
    at 1855 - richer than before it ever bought anything.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid, promo_code="SALE10")

    responses = await asyncio.gather(
        client.post(REFUND_URL.format(wid=wid, pid=pid)),
        client.post(REFUND_URL.format(wid=wid, pid=pid)),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert await _balance(client, wid) == 1000


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "True row-level locking needs Postgres. "
        "Set TEST_DATABASE_URL to a Postgres DSN to run this test."
    ),
)
async def test_concurrent_partial_refunds_never_exceed_the_charge(client):
    """Ten parallel refunds of 100 against a charge of 855.

    Eight fit, the remaining 55 cannot cover a ninth, so exactly eight
    succeed and the payout stops at 800.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)
    pid, _ = await _buy(client, wid, promo_code="SALE10")

    responses = await asyncio.gather(
        *[
            client.post(
                REFUND_URL.format(wid=wid, pid=pid),
                json={"amount": 100},
            )
            for _ in range(10)
        ]
    )
    ok = [r for r in responses if r.status_code == 200]
    rejected = [r for r in responses if r.status_code == 409]
    assert len(ok) == 8
    assert len(rejected) == 2

    # 145 left after the purchase, plus 800 refunded.
    assert await _balance(client, wid) == 945

    # The last successful refund reports the true running total.
    totals = sorted(r.json()["refunded_total"] for r in ok)
    assert totals[-1] == 800
