# Тесты: лимиты и срок действия купонов
"""Tests for coupon validity and usage limits."""
import asyncio
import uuid

import pytest

from tests.conftest import IS_SQLITE

OP_URL = "/api/v1/wallets/{wid}/operation"
GET_URL = "/api/v1/wallets/{wid}"
BUY_URL = "/api/v1/wallets/{wid}/purchase"
CHECK_URL = "/api/v1/wallets/{wid}/purchase/check"
REFUND_URL = "/api/v1/wallets/{wid}/purchases/{pid}/refund"

# Subtotal 950. With a 10% coupon the charge is 855; with FIRST3 it is 850.
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


async def _buy(client, wid, code=None):
    payload = {"items": CART_950}
    if code is not None:
        payload["promo_code"] = code
    return await client.post(BUY_URL.format(wid=wid), json=payload)


async def _check(client, wid, code=None):
    payload = {"items": CART_950}
    if code is not None:
        payload["promo_code"] = code
    return await client.post(CHECK_URL.format(wid=wid), json=payload)


# --- validity window ---------------------------------------------------


async def test_expired_coupon_rejected(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await _buy(client, wid, "EXPIRED")
    assert resp.status_code == 400
    assert "expired on 2020-01-01" in resp.json()["detail"]
    # Not silently charged at full price either.
    assert await _balance(client, wid) == 1000


async def test_not_yet_active_coupon_rejected(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await _buy(client, wid, "FUTURE")
    assert resp.status_code == 400
    assert "not valid until 2099-01-01" in resp.json()["detail"]
    assert await _balance(client, wid) == 1000


async def test_check_reports_expired_coupon(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await _check(client, wid, "EXPIRED")
    assert resp.status_code == 400


# --- per-wallet limit --------------------------------------------------


async def test_one_per_wallet_cannot_be_used_twice(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 5000)

    resp = await _buy(client, wid, "ONCE")
    assert resp.status_code == 200
    assert resp.json()["total"] == 855

    resp = await _buy(client, wid, "ONCE")
    assert resp.status_code == 409
    assert "already been used" in resp.json()["detail"]

    # The wallet paid once, and only once.
    assert await _balance(client, wid) == 4145

    # An unrestricted coupon still works, so the block is coupon-specific.
    resp = await _buy(client, wid, "SALE10")
    assert resp.status_code == 200


async def test_per_wallet_limit_is_per_wallet(client):
    """One customer using up their allowance must not block another."""
    first = uuid.uuid4()
    second = uuid.uuid4()
    await _topup(client, first, 1000)
    await _topup(client, second, 1000)

    assert (await _buy(client, first, "ONCE")).status_code == 200
    assert (await _buy(client, second, "ONCE")).status_code == 200

    assert await _balance(client, first) == 145
    assert await _balance(client, second) == 145


async def test_check_reports_a_used_up_coupon(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 5000)
    await _buy(client, wid, "ONCE")

    resp = await _check(client, wid, "ONCE")
    assert resp.status_code == 409


# --- global limit ------------------------------------------------------


async def test_global_limit_stops_after_the_last_use(client):
    """FIRST3 allows three uses in total, then nobody else gets it."""
    wid = uuid.uuid4()
    await _topup(client, wid, 5000)

    for _ in range(3):
        resp = await _buy(client, wid, "FIRST3")
        assert resp.status_code == 200
        assert resp.json()["total"] == 850

    resp = await _buy(client, wid, "FIRST3")
    assert resp.status_code == 409
    assert "limit of 3 use(s)" in resp.json()["detail"]

    # Charged exactly three times: 5000 - 3 * 850
    assert await _balance(client, wid) == 2450


async def test_check_reports_remaining_uses(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 5000)

    resp = await _check(client, wid, "FIRST3")
    assert resp.json()["coupon_uses_left"] == 3

    await _buy(client, wid, "FIRST3")

    resp = await _check(client, wid, "FIRST3")
    assert resp.json()["coupon_uses_left"] == 2

    # An unlimited coupon reports no number rather than a fake one.
    resp = await _check(client, wid, "SALE10")
    assert resp.json()["coupon_uses_left"] is None


async def test_both_limits_apply_together(client):
    """SPRING: two uses per wallet, five in total."""
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    for wid in (a, b, c, d):
        await _topup(client, wid, 5000)

    # A takes its two, B takes its two: four of five used.
    for wid in (a, b):
        assert (await _buy(client, wid, "SPRING")).status_code == 200
        assert (await _buy(client, wid, "SPRING")).status_code == 200

    # A wants a third: refused by the per-wallet rule, not the global one.
    resp = await _buy(client, a, "SPRING")
    assert resp.status_code == 409
    assert "per wallet" in resp.json()["detail"]

    # C takes the fifth and last one - which proves A's rejected attempt
    # did not consume a global use.
    assert (await _buy(client, c, "SPRING")).status_code == 200

    # D is too late.
    resp = await _buy(client, d, "SPRING")
    assert resp.status_code == 409
    assert "limit of 5 use(s)" in resp.json()["detail"]
    assert await _balance(client, d) == 5000


# --- a coupon must only be consumed by a completed purchase -----------


async def test_insufficient_funds_does_not_consume_the_coupon(client):
    """A payment that fails must leave the coupon untouched.

    The claim and the debit share a transaction, so the rollback that
    undoes the (never applied) charge also undoes the claim.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 100)

    resp = await _buy(client, wid, "ONCE")
    assert resp.status_code == 400

    # Now the wallet can pay, and the coupon is still available.
    await _topup(client, wid, 1000)
    resp = await _buy(client, wid, "ONCE")
    assert resp.status_code == 200
    assert resp.json()["total"] == 855


async def test_quoting_a_cart_does_not_consume_the_coupon(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    for _ in range(3):
        resp = await _check(client, wid, "ONCE")
        assert resp.status_code == 200
        assert resp.json()["total"] == 855

    resp = await _buy(client, wid, "ONCE")
    assert resp.status_code == 200


async def test_refund_does_not_give_the_coupon_back(client):
    """Deliberate policy: buy-refund-repeat must not farm a coupon.

    Returning the use would let one wallet drain a "one per customer"
    offer indefinitely. Refunds return money, not entitlements.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 5000)

    resp = await _buy(client, wid, "ONCE")
    pid = resp.json()["purchase_id"]

    resp = await client.post(REFUND_URL.format(wid=wid, pid=pid))
    assert resp.status_code == 200
    assert await _balance(client, wid) == 5000

    resp = await _buy(client, wid, "ONCE")
    assert resp.status_code == 409


# --- concurrency -------------------------------------------------------


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "Real row locking needs Postgres. Set TEST_DATABASE_URL to a "
        "Postgres DSN to run this test."
    ),
)
async def test_concurrent_uses_cannot_exceed_the_global_limit(client):
    """Ten parallel purchases race for three available uses.

    This is the case a read-then-write check gets wrong: every request
    would read used_count = 0 and every request would be let through. The
    conditional UPDATE lets exactly three win.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 5000)

    responses = await asyncio.gather(
        *[_buy(client, wid, "FIRST3") for _ in range(10)]
    )
    ok = [r for r in responses if r.status_code == 200]
    rejected = [r for r in responses if r.status_code == 409]
    assert len(ok) == 3
    assert len(rejected) == 7

    # 5000 - 3 * 850
    assert await _balance(client, wid) == 2450


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "Real row locking needs Postgres. Set TEST_DATABASE_URL to a "
        "Postgres DSN to run this test."
    ),
)
async def test_concurrent_uses_cannot_beat_the_per_wallet_limit(client):
    """Five parallel attempts to use a one-per-customer coupon.

    Exactly one may go through, and the wallet is charged the discounted
    price exactly once.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 5000)

    responses = await asyncio.gather(
        *[_buy(client, wid, "ONCE") for _ in range(5)]
    )
    ok = [r for r in responses if r.status_code == 200]
    assert len(ok) == 1
    assert all(r.status_code == 409 for r in responses if r not in ok)

    assert await _balance(client, wid) == 4145
