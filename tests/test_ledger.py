# Тесты: журнал операций
"""Tests for the operations ledger."""
import uuid

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


async def _op(client, wid, kind, amount):
    return await client.post(
        OP_URL.format(wid=wid),
        json={"operation_type": kind, "amount": amount},
    )


async def _history(client, wid, **params):
    resp = await client.get(HIST_URL.format(wid=wid), params=params)
    assert resp.status_code == 200
    return resp.json()


async def test_deposit_is_recorded(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)

    body = await _history(client, wid)
    assert body["count"] == 1
    row = body["operations"][0]
    assert row["kind"] == "DEPOSIT"
    assert row["amount"] == 1000
    assert row["balance_after"] == 1000
    assert row["purchase_id"] is None
    assert uuid.UUID(row["id"])
    assert row["created_at"]


async def test_withdrawal_is_recorded_as_negative(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)
    await _op(client, wid, "WITHDRAW", 400)

    row = (await _history(client, wid))["operations"][0]
    assert row["kind"] == "WITHDRAW"
    assert row["amount"] == -400
    assert row["balance_after"] == 600


async def test_purchase_and_refund_link_to_the_purchase(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "SALE10"},
    )
    pid = resp.json()["purchase_id"]

    row = (await _history(client, wid))["operations"][0]
    assert row["kind"] == "PURCHASE"
    assert row["amount"] == -855
    assert row["balance_after"] == 145
    assert row["purchase_id"] == pid

    await client.post(REFUND_URL.format(wid=wid, pid=pid))

    row = (await _history(client, wid))["operations"][0]
    assert row["kind"] == "REFUND"
    assert row["amount"] == 855
    assert row["balance_after"] == 1000
    # This link is what makes the ledger a refund journal.
    assert row["purchase_id"] == pid


async def test_ledger_sums_to_the_balance(client):
    """The invariant that makes the history auditable, not decorative."""
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 1000)
    await _op(client, wid, "WITHDRAW", 100)
    await _op(client, wid, "DEPOSIT", 250)

    resp = await client.post(
        BUY_URL.format(wid=wid),
        json={"items": CART_950, "promo_code": "HALF"},
    )
    pid = resp.json()["purchase_id"]
    await client.post(
        REFUND_URL.format(wid=wid, pid=pid),
        json={"amount": 200},
    )

    body = await _history(client, wid, limit=200)
    assert body["count"] == 5

    ledger_sum = sum(row["amount"] for row in body["operations"])
    balance = (await client.get(GET_URL.format(wid=wid))).json()["balance"]
    assert ledger_sum == balance


async def test_rejected_operations_leave_no_trace(client):
    """Only movements that happened are written down."""
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 100)

    assert (await _op(client, wid, "WITHDRAW", 500)).status_code == 400
    resp = await client.post(BUY_URL.format(wid=wid), json={"items": CART_950})
    assert resp.status_code == 400

    body = await _history(client, wid)
    assert body["count"] == 1
    assert body["operations"][0]["kind"] == "DEPOSIT"


async def test_history_is_newest_first(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 100)
    await _op(client, wid, "DEPOSIT", 200)
    await _op(client, wid, "DEPOSIT", 300)

    rows = (await _history(client, wid))["operations"]
    assert [r["amount"] for r in rows] == [300, 200, 100]
    assert [r["balance_after"] for r in rows] == [600, 300, 100]


async def test_history_pagination(client):
    wid = uuid.uuid4()
    for amount in (10, 20, 30, 40):
        await _op(client, wid, "DEPOSIT", amount)

    page = await _history(client, wid, limit=2)
    assert [r["amount"] for r in page["operations"]] == [40, 30]

    page = await _history(client, wid, limit=2, offset=2)
    assert [r["amount"] for r in page["operations"]] == [20, 10]


async def test_history_of_unknown_wallet_404(client):
    """An unknown wallet is an error, not an empty statement."""
    resp = await client.get(HIST_URL.format(wid=uuid.uuid4()))
    assert resp.status_code == 404


async def test_history_limit_is_bounded(client):
    wid = uuid.uuid4()
    await _op(client, wid, "DEPOSIT", 100)

    resp = await client.get(HIST_URL.format(wid=wid), params={"limit": 5000})
    assert resp.status_code == 422

    resp = await client.get(HIST_URL.format(wid=wid), params={"offset": -1})
    assert resp.status_code == 422
