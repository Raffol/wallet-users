# HTTP-эндпоинты: путь, схема, вызов crud, ответ
"""HTTP routes for wallet operations."""
import hashlib
import json
import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.database import get_session
from app.pricing import price_cart
from app.promos import check_window, get_promo
from app.schemas import (
    BalanceResponse,
    HistoryResponse,
    OperationRecord,
    OperationRequest,
    PurchaseCheckResponse,
    PurchaseRequest,
    PurchaseResponse,
    RefundRequest,
    RefundResponse,
    StatusRequest,
    StatusResponse,
)

router = APIRouter(tags=["wallets"])

# Declared as an Annotated alias so the same header definition can be
# reused across endpoints without sharing a mutable default.
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=255,
        description=(
            "Optional. Send the same key when retrying a request that "
            "may already have been applied; the original result is "
            "returned instead of charging twice."
        ),
    ),
]


# отпечаток тела запроса для идемпотентности
def _fingerprint(payload: dict[str, Any] | None) -> str:
    """Hash a request body so one key cannot cover two different requests.

    Keys are sorted, so a client that serialises its JSON differently on a
    retry still matches.
    """
    body = json.dumps(payload or {}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


# пополнение или списание
@router.post(
    "/wallets/{wallet_uuid}/operation",
    response_model=BalanceResponse,
)
async def change_balance(
    wallet_uuid: uuid.UUID,
    payload: OperationRequest,
    idempotency_key: IdempotencyKeyHeader = None,
    session: AsyncSession = Depends(get_session),
) -> BalanceResponse:
    """Deposit to or withdraw from a wallet; returns the new balance."""
    result = await crud.perform_operation(
        session,
        wallet_uuid,
        payload.operation_type,
        payload.amount,
        idempotency_key=idempotency_key,
        request_hash=_fingerprint(payload.model_dump(mode="json")),
    )
    return BalanceResponse(wallet_uuid=wallet_uuid, **result)


# текущий баланс
@router.get(
    "/wallets/{wallet_uuid}",
    response_model=BalanceResponse,
)
async def read_balance(
    wallet_uuid: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> BalanceResponse:
    """Return the current balance of a wallet."""
    balance = await crud.get_balance(session, wallet_uuid)
    return BalanceResponse(wallet_uuid=wallet_uuid, balance=balance)


# история операций, новые сверху
@router.get(
    "/wallets/{wallet_uuid}/operations",
    response_model=HistoryResponse,
)
async def read_history(
    wallet_uuid: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> HistoryResponse:
    """Return the wallet's ledger, newest first.

    Summing ``amount`` over the whole ledger reproduces the balance, which
    is what makes the history auditable rather than decorative.
    """
    rows = await crud.list_operations(session, wallet_uuid, limit, offset)
    return HistoryResponse(
        wallet_uuid=wallet_uuid,
        count=len(rows),
        operations=[OperationRecord.model_validate(row) for row in rows],
    )


# заморозка или разморозка кошелька
@router.post(
    "/wallets/{wallet_uuid}/status",
    response_model=StatusResponse,
)
async def change_status(
    wallet_uuid: uuid.UUID,
    payload: StatusRequest,
    session: AsyncSession = Depends(get_session),
) -> StatusResponse:
    """Freeze or unfreeze a wallet.

    A frozen wallet refuses withdrawals and purchases but still accepts
    deposits and refunds, so freezing protects a balance without trapping
    money that is owed back.
    """
    status = await crud.set_status(
        session,
        wallet_uuid,
        payload.status.value,
    )
    return StatusResponse(wallet_uuid=wallet_uuid, status=status)


# покупка корзины со скидкой
@router.post(
    "/wallets/{wallet_uuid}/purchase",
    response_model=PurchaseResponse,
)
async def make_purchase(
    wallet_uuid: uuid.UUID,
    payload: PurchaseRequest,
    idempotency_key: IdempotencyKeyHeader = None,
    session: AsyncSession = Depends(get_session),
) -> PurchaseResponse:
    """Pay for a cart of items from the wallet balance.

    Subtotal, discount and total are all recomputed here, so a client can
    neither dictate what it is charged nor grant itself a discount. If the
    client sent ``expected_total`` and it disagrees, the purchase is
    refused before any money moves.
    """
    promo = None
    if payload.promo_code is not None:
        promo = get_promo(payload.promo_code)
        check_window(promo)

    totals = price_cart(payload.subtotal, promo)

    if (
        payload.expected_total is not None
        and payload.expected_total != totals.total
    ):
        raise crud.TotalMismatchError(payload.expected_total, totals.total)

    items = [
        {
            "name": item.name,
            "price": str(item.price),
            "quantity": item.quantity,
        }
        for item in payload.items
    ]

    result = await crud.purchase(
        session,
        wallet_uuid,
        subtotal=totals.subtotal,
        discount=totals.discount,
        total=totals.total,
        promo=promo,
        items=items,
        idempotency_key=idempotency_key,
        request_hash=_fingerprint(payload.model_dump(mode="json")),
    )
    return PurchaseResponse(wallet_uuid=wallet_uuid, **result)


# расчёт корзины без списания
@router.post(
    "/wallets/{wallet_uuid}/purchase/check",
    response_model=PurchaseCheckResponse,
)
async def check_purchase(
    wallet_uuid: uuid.UUID,
    payload: PurchaseRequest,
    session: AsyncSession = Depends(get_session),
) -> PurchaseCheckResponse:
    """Quote a cart and say whether it is affordable, without charging.

    Useful for showing a cart total with the discount applied, or for
    disabling a checkout button. The verdict is advisory only: it reflects
    the balance at this instant and reserves nothing, so the purchase
    endpoint checks again inside its transaction.
    """
    promo = None
    coupon_uses_left = None
    if payload.promo_code is not None:
        promo = get_promo(payload.promo_code)
        check_window(promo)
        coupon_uses_left = await crud.validate_coupon(
            session,
            promo,
            wallet_uuid,
        )

    totals = price_cart(payload.subtotal, promo)
    balance = await crud.get_balance(session, wallet_uuid)
    shortfall = totals.total - balance
    return PurchaseCheckResponse(
        wallet_uuid=wallet_uuid,
        positions=len(payload.items),
        items_count=payload.items_count,
        promo_code=payload.promo_code,
        subtotal=totals.subtotal,
        discount=totals.discount,
        total=totals.total,
        balance=balance,
        affordable=balance >= totals.total,
        shortfall=shortfall if shortfall > 0 else Decimal("0"),
        coupon_uses_left=coupon_uses_left,
    )


# возврат покупки, полный или частичный
@router.post(
    "/wallets/{wallet_uuid}/purchases/{purchase_id}/refund",
    response_model=RefundResponse,
)
async def refund_purchase(
    wallet_uuid: uuid.UUID,
    purchase_id: uuid.UUID,
    payload: RefundRequest | None = None,
    idempotency_key: IdempotencyKeyHeader = None,
    session: AsyncSession = Depends(get_session),
) -> RefundResponse:
    """Refund a purchase, fully by default or partially by amount.

    The credit is the amount that was charged, taken from the stored
    purchase record. The request cannot name a sum of its own beyond
    asking for part of what remains, so a refund can never pay out more
    than the purchase brought in.
    """
    amount = payload.amount if payload is not None else None
    body = payload.model_dump(mode="json") if payload is not None else {}
    result = await crud.refund(
        session,
        wallet_uuid,
        purchase_id,
        amount,
        idempotency_key=idempotency_key,
        request_hash=_fingerprint(body),
    )
    return RefundResponse(wallet_uuid=wallet_uuid, **result)
