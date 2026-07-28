# Тесты: базовые операции с балансом
"""Endpoint tests for the wallet service."""
import asyncio
import uuid

import pytest

from tests.conftest import IS_SQLITE

OP_URL = "/api/v1/wallets/{wid}/operation"
GET_URL = "/api/v1/wallets/{wid}"


async def test_deposit_creates_wallet(client):
    wid = uuid.uuid4()
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "DEPOSIT", "amount": 1000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["balance"] == 1000
    assert body["wallet_uuid"] == str(wid)


async def test_get_balance_after_deposit(client):
    wid = uuid.uuid4()
    await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "DEPOSIT", "amount": 250},
    )
    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.status_code == 200
    assert resp.json()["balance"] == 250


async def test_withdraw_reduces_balance(client):
    wid = uuid.uuid4()
    await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "DEPOSIT", "amount": 100},
    )
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "WITHDRAW", "amount": 40},
    )
    assert resp.status_code == 200
    assert resp.json()["balance"] == 60


async def test_withdraw_insufficient_funds(client):
    wid = uuid.uuid4()
    await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "DEPOSIT", "amount": 30},
    )
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "WITHDRAW", "amount": 50},
    )
    assert resp.status_code == 400
    # Balance must be untouched after a rejected withdrawal.
    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.json()["balance"] == 30


async def test_withdraw_missing_wallet_404(client):
    wid = uuid.uuid4()
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "WITHDRAW", "amount": 10},
    )
    assert resp.status_code == 404


async def test_get_missing_wallet_404(client):
    resp = await client.get(GET_URL.format(wid=uuid.uuid4()))
    assert resp.status_code == 404


async def test_invalid_operation_type_422(client):
    wid = uuid.uuid4()
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "TRANSFER", "amount": 10},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("amount", [0, -5])
async def test_non_positive_amount_422(client, amount):
    wid = uuid.uuid4()
    resp = await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": "DEPOSIT", "amount": amount},
    )
    assert resp.status_code == 422


async def test_invalid_uuid_422(client):
    resp = await client.get(GET_URL.format(wid="not-a-uuid"))
    assert resp.status_code == 422


@pytest.mark.skipif(
    IS_SQLITE,
    reason=(
        "True row-level locking needs Postgres. "
        "Set TEST_DATABASE_URL to a Postgres DSN to run this test."
    ),
)
async def test_concurrent_deposits_no_lost_updates(client):
    wid = uuid.uuid4()
    requests = 50
    per_request = 10

    tasks = [
        client.post(
            OP_URL.format(wid=wid),
            json={"operation_type": "DEPOSIT", "amount": per_request},
        )
        for _ in range(requests)
    ]
    responses = await asyncio.gather(*tasks)

    assert all(r.status_code == 200 for r in responses)

    resp = await client.get(GET_URL.format(wid=wid))
    assert resp.json()["balance"] == requests * per_request
