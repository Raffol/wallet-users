# Схемы запросов и ответов (валидация Pydantic)
"""Request and response schemas."""
import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.money import to_cents


# DEPOSIT или WITHDRAW
class OperationType(str, Enum):
    """Supported balance operations."""

    DEPOSIT = "DEPOSIT"
    WITHDRAW = "WITHDRAW"


# Amount must be strictly positive and limited to 2 decimal places.
Amount = Annotated[Decimal, Field(gt=0, max_digits=20, decimal_places=2)]


# тело запроса на изменение баланса
class OperationRequest(BaseModel):
    """Body of ``POST /wallets/{uuid}/operation``."""

    operation_type: OperationType
    amount: Amount

    # Reject unknown fields so malformed payloads fail fast (HTTP 422).
    model_config = ConfigDict(extra="forbid")


# ответ с балансом кошелька
class BalanceResponse(BaseModel):
    """Wallet balance returned by both endpoints."""

    wallet_uuid: uuid.UUID
    balance: Decimal

    @field_serializer("balance")
    def _serialize_balance(self, value: Decimal) -> float:
        # Expose the balance as a JSON number for client convenience.
        return float(value)


# Quantity of a single position in a cart.
Quantity = Annotated[int, Field(gt=0, le=10_000)]


# одна позиция корзины
class PurchaseItem(BaseModel):
    """One position in the cart."""

    name: str = Field(min_length=1, max_length=200)
    price: Amount
    quantity: Quantity = 1

    model_config = ConfigDict(extra="forbid")

    @property
    def subtotal(self) -> Decimal:
        """Price multiplied by quantity, rounded to cents."""
        return to_cents(self.price * self.quantity)


# тело запроса на покупку/предпросмотр
class PurchaseRequest(BaseModel):
    """Body of ``POST /wallets/{uuid}/purchase``.

    ``promo_code`` names an offer; the discount itself is resolved on the
    server. ``expected_total`` is optional and is compared with the final
    total *after* any discount, so a stale price list or a client-side
    discount miscalculation aborts the purchase with HTTP 409 instead of
    charging an unexpected amount.
    """

    items: list[PurchaseItem] = Field(min_length=1, max_length=100)
    promo_code: str | None = Field(default=None, min_length=1, max_length=32)
    expected_total: Amount | None = None

    model_config = ConfigDict(extra="forbid")

    @property
    def subtotal(self) -> Decimal:
        """Cart value before any discount, computed server-side."""
        return to_cents(
            sum(
                (item.subtotal for item in self.items),
                Decimal("0"),
            )
        )

    @property
    def items_count(self) -> int:
        """Total number of units across all positions."""
        return sum(item.quantity for item in self.items)


# ответ на покупку с разбивкой
class PurchaseResponse(BaseModel):
    """Result of a successful purchase.

    ``purchase_id`` is what a client keeps in order to refund later.
    """

    wallet_uuid: uuid.UUID
    purchase_id: uuid.UUID
    positions: int
    items_count: int
    promo_code: str | None
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    balance: Decimal

    @field_serializer("subtotal", "discount", "total", "balance")
    def _serialize_money(self, value: Decimal) -> float:
        return float(value)


# предпросмотр: по карману ли корзина
class PurchaseCheckResponse(BaseModel):
    """Advisory pre-check: can this cart be paid for right now?

    The answer is only a snapshot. It may become stale the moment it is
    returned, so the binding check always happens inside the purchase
    transaction itself.
    """

    wallet_uuid: uuid.UUID
    positions: int
    items_count: int
    promo_code: str | None
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    balance: Decimal
    affordable: bool
    shortfall: Decimal
    # How many uses the coupon has left globally; None when unlimited.
    coupon_uses_left: int | None = None

    @field_serializer(
        "subtotal",
        "discount",
        "total",
        "balance",
        "shortfall",
    )
    def _serialize_money(self, value: Decimal) -> float:
        return float(value)


# тело запроса на возврат (сумма необязательна)
class RefundRequest(BaseModel):
    """Body of the refund endpoint.

    The body is optional. Sending none (or a null ``amount``) refunds
    everything still refundable; sending an amount refunds just that part.
    """

    amount: Amount | None = None

    model_config = ConfigDict(extra="forbid")


# ответ на возврат
class RefundResponse(BaseModel):
    """Result of a refund."""

    wallet_uuid: uuid.UUID
    purchase_id: uuid.UUID
    refunded: Decimal
    refunded_total: Decimal
    refundable_remaining: Decimal
    balance: Decimal

    @field_serializer(
        "refunded",
        "refunded_total",
        "refundable_remaining",
        "balance",
    )
    def _serialize_money(self, value: Decimal) -> float:
        return float(value)


# ACTIVE или FROZEN
class WalletStatus(str, Enum):
    """Whether a wallet may be spent from."""

    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"


# тело запроса смены статуса
class StatusRequest(BaseModel):
    """Body of the freeze/unfreeze endpoint."""

    status: WalletStatus

    model_config = ConfigDict(extra="forbid")


# ответ со статусом кошелька
class StatusResponse(BaseModel):
    """Wallet status after the change."""

    wallet_uuid: uuid.UUID
    status: WalletStatus


# вид записи в журнале
class OperationKind(str, Enum):
    """What kind of movement a ledger row records."""

    DEPOSIT = "DEPOSIT"
    WITHDRAW = "WITHDRAW"
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"


# одна строка журнала операций
class OperationRecord(BaseModel):
    """One ledger row.

    ``amount`` keeps its sign - credits positive, debits negative - so a
    client can sum a statement and arrive at the balance.
    """

    id: uuid.UUID
    kind: OperationKind
    amount: Decimal
    balance_after: Decimal
    purchase_id: uuid.UUID | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("amount", "balance_after")
    def _serialize_money(self, value: Decimal) -> float:
        return float(value)


# страница истории операций
class HistoryResponse(BaseModel):
    """A page of a wallet's ledger, newest first."""

    wallet_uuid: uuid.UUID
    count: int
    operations: list[OperationRecord]
