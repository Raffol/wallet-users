# Таблицы базы данных (ORM-модели)
"""ORM models."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# базовый класс для всех моделей
class Base(DeclarativeBase):
    """Declarative base for all models."""


# кошелёк: баланс и статус
class Wallet(Base):
    """A user wallet holding a single monetary balance.

    ``balance`` uses ``Numeric`` (exact decimal arithmetic) rather than a
    floating point type so that money is never subject to rounding error.
    """

    __tablename__ = "wallets"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )
    balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )
    # ACTIVE or FROZEN. A frozen wallet cannot be spent from, but can
    # still receive money - see ``crud`` for the reasoning.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# покупка: что списали со скидкой, основа возврата
class Purchase(Base):
    """A recorded purchase, and the basis for refunding it.

    A refund needs to credit the amount that was actually charged, which
    is the discounted total rather than the cart subtotal. That number
    only exists if the purchase was written down, so every purchase is
    persisted in the same transaction that debits the wallet.

    ``refunded_total`` accumulates refunds against this purchase. Keeping
    a running sum (instead of a refunded yes/no flag) is what makes
    partial refunds possible while still capping the lifetime payout at
    ``total``.
    """

    __tablename__ = "purchases"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("wallets.id"),
        nullable=False,
        index=True,
    )
    promo_code: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    discount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    refunded_total: Mapped[Decimal] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )
    # Snapshot of the cart. Prices are stored as strings so the exact
    # decimal values survive the JSON round-trip.
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


# глобальный счётчик использований купона
class CouponUsage(Base):
    """Global redemption counter for one coupon code.

    A "first 100 customers" limit cannot live in application memory: two
    parallel requests would both read 99 and both proceed. The counter is
    a database row, and it is incremented with a conditional UPDATE that
    only matches while the limit still allows it (see ``crud``). That
    single statement is the whole guard - no separate read is involved,
    so there is no window between checking and claiming.
    """

    __tablename__ = "coupon_usages"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    used_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# кто и на какой покупке применил купон
class CouponRedemption(Base):
    """One recorded use of a coupon, by one wallet, on one purchase.

    Per-customer limits ("one per account") are answered by counting these
    rows. The count is only trustworthy because the transaction already
    holds the ``coupon_usages`` row for this code, which serialises every
    redemption of that coupon.
    """

    __tablename__ = "coupon_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    coupon_code: Mapped[str] = mapped_column(String(32), nullable=False)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("wallets.id"),
        nullable=False,
    )
    purchase_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchases.id"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_redemptions_code_wallet", "coupon_code", "wallet_id"),
    )


# знаковый журнал движений денег
class Operation(Base):
    """One movement of money: the append-only ledger.

    Storing only a balance answers "how much is there" but never "how did
    it get there". Every deposit, withdrawal, purchase and refund writes
    exactly one row here, inside the same transaction that moves the
    money, so the ledger cannot drift from the balance.

    ``amount`` is signed - credits positive, debits negative - which makes
    the invariant checkable: the sum of a wallet's operations equals its
    balance. ``balance_after`` snapshots the result, so a statement can be
    rendered without replaying arithmetic.
    """

    __tablename__ = "operations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("wallets.id"),
        nullable=False,
        index=True,
    )
    # DEPOSIT, WITHDRAW, PURCHASE or REFUND.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(
        Numeric(20, 2),
        nullable=False,
    )
    # Set for PURCHASE and REFUND: this is what turns the ledger into a
    # refund journal, recording which purchase was returned and when.
    purchase_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchases.id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


# сохранённый ответ по ключу идемпотентности
class IdempotencyKey(Base):
    """A remembered result, so a retried request is not applied twice.

    A client that times out cannot tell whether its charge went through.
    Retrying is the only sane thing to do, and without this table the
    retry would charge again. The key is claimed and the result stored in
    the *same* transaction as the work, so either both exist or neither
    does.

    The primary key does the heavy lifting: a duplicate request blocks on
    the unique index until the first transaction finishes, then finds the
    stored response. If the first attempt failed and rolled back, the key
    is gone and the retry does the work for real.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    # Endpoint name plus a hash of the body: reusing one key for a
    # different request is a client bug worth reporting, not replaying.
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
