# Тесты: покупки со скидками
"""Tests for discounted purchases."""
import asyncio
import uuid

import pytest

from tests.conftest import IS_SQLITE

OP_URL = "/api/v1/wallets/{wid}/operation"
GET_URL = "/api/v1/wallets/{wid}"
BUY_URL = "/api/v1/wallets/{wid}/purchase"
CHECK_URL = "/api/v1/wallets/{wid}/purchase/check"

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


async def test_percent_discount_applied(client):
    """SALE10 on a 950 cart charges 855 and reports the breakdown."""
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "SALE10"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["subtotal"] == 950
    assert body["discount"] == 95
    assert body["total"] == 855
    assert body["balance"] == 145
    assert body["promo_code"] == "SALE10"

    assert await _balance(client, wid) == 145


async def test_fixed_discount_applied(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "MINUS100"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["discount"] == 100
    assert body["total"] == 850
    assert body["balance"] == 150


async def test_promo_code_is_case_insensitive(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "sale10"},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 855


async def test_half_cent_rounds_up_not_bankers(client):
    """25% of 0.50 is exactly 0.125 - a half cent.

    Decimal's default ROUND_HALF_EVEN would make the discount 0.12 and
    the total 0.38. Commerce expects 0.13 / 0.37, and this test pins that
    choice down so nobody silently reverts it.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 10)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={
            "items": [{"name": "Sticker", "price": 0.5, "quantity": 1}],
            "promo_code": "SALE25",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["discount"] == 0.13
    assert body["total"] == 0.37
    assert body["balance"] == 9.63


async def test_discount_capped_at_subtotal(client):
    """A 5000 fixed discount on a 950 cart makes it free, not negative."""
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "CLEARANCE"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["discount"] == 950
    assert body["total"] == 0
    # Nothing was debited and, crucially, nothing was credited either.
    assert body["balance"] == 1000
    assert await _balance(client, wid) == 1000


async def test_promo_below_minimum_subtotal_rejected(client):
    """MINUS100 needs a subtotal of 500; a 200 cart must not qualify."""
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={
            "items": [{"name": "Mug", "price": 200, "quantity": 1}],
            "promo_code": "MINUS100",
        },
    )
    assert resp.status_code == 400
    assert "at least 500" in resp.json()["detail"]
    assert await _balance(client, wid) == 1000


async def test_unknown_promo_code_rejected(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "FREESTUFF"},
    )
    assert resp.status_code == 400
    # The cart is not silently charged at full price either.
    assert await _balance(client, wid) == 1000


async def test_client_cannot_invent_a_discount(client):
    """A discount field in the body is refused, not honoured."""
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "discount": 900},
    )
    assert resp.status_code == 422
    assert await _balance(client, wid) == 1000


async def test_expected_total_must_match_discounted_total(client):
    """Client expecting the undiscounted 950 gets a 409, not a charge."""
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={
            "items": CART_950,
            "promo_code": "SALE10",
            "expected_total": 950,
        },
    )
    assert resp.status_code == 409
    assert await _balance(client, wid) == 1000

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={
            "items": CART_950,
            "promo_code": "SALE10",
            "expected_total": 855,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["balance"] == 145


async def test_discount_can_make_an_unaffordable_cart_affordable(client):
    """900 cannot cover 950, but covers 855 once SALE10 applies."""
    wid = uuid.uuid4()
    await _topup(client, wid, 900)

    resp = await client.post(BUY_URL.format(wid=wid), json={"items": CART_950})
    assert resp.status_code == 400

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "SALE10"},
    )
    assert resp.status_code == 200
    assert resp.json()["balance"] == 45


async def test_insufficient_funds_measured_after_discount(client):
    """The shortfall is reported against the discounted total."""
    wid = uuid.uuid4()
    await _topup(client, wid, 800)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "SALE10"},
    )
    assert resp.status_code == 400
    assert "required 855.00" in resp.json()["detail"]
    assert "short by 55.00" in resp.json()["detail"]
    assert await _balance(client, wid) == 800


async def test_check_quotes_the_discounted_total(client):
    """The advisory check must quote exactly what will be charged."""
    wid = uuid.uuid4()
    await _topup(client, wid, 900)

    resp = await client.post(
        CHECK_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "SALE10"},
    )
    assert resp.status_code == 200
    quote = resp.json()
    assert quote["discount"] == 95
    assert quote["total"] == 855
    assert quote["affordable"] is True
    assert quote["shortfall"] == 0
    # Quoting must not move money.
    assert await _balance(client, wid) == 900

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "SALE10"},
    )
    assert resp.json()["total"] == quote["total"]


async def test_check_reports_shortfall_after_discount(client):
    wid = uuid.uuid4()
    await _topup(client, wid, 500)

    resp = await client.post(
        CHECK_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "HALF"},
    )
    assert resp.status_code == 200
    quote = resp.json()
    assert quote["total"] == 475
    assert quote["affordable"] is True

    resp = await client.post(
        CHECK_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "SALE10"},
    )
    quote = resp.json()
    assert quote["affordable"] is False
    assert quote["shortfall"] == 355


async def test_promo_is_not_applied_twice(client):
    """Two discounted purchases each pay 855, not a compounding amount."""
    wid = uuid.uuid4()
    await _topup(client, wid, 2000)

    for expected_balance in (1145, 290):
        resp = await client.post(
            BUY_URL.format(wid=wid),
            json={"items": CART_950, "promo_code": "SALE10"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 855
        assert resp.json()["balance"] == expected_balance


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "True row-level locking needs Postgres. "
        "Set TEST_DATABASE_URL to a Postgres DSN to run this test."
    ),
)
async def test_concurrent_discounted_purchases_cannot_overspend(client):
    """Discounts do not weaken the concurrency guarantee.

    Balance 1000, two parallel SALE10 purchases at 855 each: only one can
    be afforded, so exactly one succeeds and the balance lands on 145.
    """
    wid = uuid.uuid4()
    await _topup(client, wid, 1000)

    payload = {"items": CART_950, "promo_code": "SALE10"}
    responses = await asyncio.gather(
        client.post(BUY_URL.format(wid=wid), json=payload),
        client.post(BUY_URL.format(wid=wid), json=payload),
    )
    assert sorted(r.status_code for r in responses) == [200, 400]
    assert await _balance(client, wid) == 145
