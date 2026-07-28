# Работа с деньгами: транзакции и блокировки строк
"""Data-access layer.

All balance mutations acquire a row-level lock (``SELECT ... FOR UPDATE``)
before the read-modify-write cycle. This makes concurrent operations on the
*same* wallet serialise at the database level, which is what prevents lost
updates when many requests hit one wallet at once.
"""
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.money import MAX_BALANCE
from app.models import (
    CouponRedemption,
    CouponUsage,
    IdempotencyKey,
    Operation,
    Purchase,
    Wallet,
)
from app.promos import (
    CouponAlreadyUsedError,
    CouponLimitReachedError,
    Promo,
)
from app.schemas import OperationType


# кошелёк не существует
class WalletNotFoundError(Exception):
    """Raised when an operation targets a wallet that does not exist."""

    def __init__(self, wallet_id: uuid.UUID) -> None:
        self.wallet_id = wallet_id
        super().__init__(f"Wallet {wallet_id} not found")


# не хватает средств на списание
class InsufficientFundsError(Exception):
    """Raised when a debit exceeds the available balance."""

    def __init__(
        self,
        wallet_id: uuid.UUID,
        required: Decimal | None = None,
        available: Decimal | None = None,
    ) -> None:
        self.wallet_id = wallet_id
        self.required = required
        self.available = available
        message = f"Insufficient funds on wallet {wallet_id}"
        if required is not None and available is not None:
            shortfall = required - available
            message += (
                f": required {required}, available {available}, "
                f"short by {shortfall}"
            )
        super().__init__(message)


# итог не совпал с ожидаемым клиентом
class TotalMismatchError(Exception):
    """Raised when the client's expected total differs from the real one."""

    def __init__(self, expected: Decimal, actual: Decimal) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Total mismatch: client expected {expected}, "
            f"server computed {actual}"
        )


# покупка не найдена или чужая
class PurchaseNotFoundError(Exception):
    """Raised when no such purchase exists on the given wallet.

    A purchase belonging to a different wallet is reported the same way,
    so the endpoint does not confirm the existence of other people's
    purchases to someone guessing identifiers.
    """

    def __init__(self, purchase_id: uuid.UUID) -> None:
        self.purchase_id = purchase_id
        super().__init__(f"Purchase {purchase_id} not found")


# возврат больше, чем было списано
class RefundNotAllowedError(Exception):
    """Raised when a refund would pay out more than was charged."""

    def __init__(
        self,
        purchase_id: uuid.UUID,
        requested: Decimal,
        remaining: Decimal,
    ) -> None:
        self.purchase_id = purchase_id
        self.requested = requested
        self.remaining = remaining
        if remaining <= 0:
            reason = "nothing is left to refund"
        else:
            reason = f"only {remaining} is left to refund"
        super().__init__(
            f"Cannot refund {requested} of purchase {purchase_id}: "
            f"{reason}"
        )


# списание с замороженного кошелька
class WalletFrozenError(Exception):
    """Raised when a debit is attempted on a frozen wallet.

    Freezing blocks spending but not receiving. A freeze exists to stop
    money leaving an account under dispute; refusing incoming refunds
    would trap the customer's money instead of protecting it.
    """

    def __init__(self, wallet_id: uuid.UUID) -> None:
        self.wallet_id = wallet_id
        super().__init__(
            f"Wallet {wallet_id} is frozen and cannot be spent from"
        )


# пополнение выше потолка баланса
class BalanceLimitError(Exception):
    """Raised when a credit would push the balance past the storage limit.

    Checked rather than caught: letting the database raise a numeric
    overflow would surface as a 500, which tells the client nothing.
    """

    def __init__(
        self,
        wallet_id: uuid.UUID,
        attempted: Decimal,
    ) -> None:
        self.wallet_id = wallet_id
        self.attempted = attempted
        super().__init__(
            f"Balance limit exceeded on wallet {wallet_id}: "
            f"{attempted} is above the maximum {MAX_BALANCE}"
        )


# ключ переиспользован для другого запроса
class IdempotencyConflictError(Exception):
    """Raised when one key is reused for a different request."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"Idempotency key {key!r} was already used for a different "
            f"request"
        )


# запрос с этим ключом ещё выполняется
class IdempotencyInProgressError(Exception):
    """Raised when an identical request is still being processed."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"A request with idempotency key {key!r} is still in progress"
        )


WALLET_ACTIVE = "ACTIVE"
WALLET_FROZEN = "FROZEN"

KIND_DEPOSIT = "DEPOSIT"
KIND_WITHDRAW = "WITHDRAW"
KIND_PURCHASE = "PURCHASE"
KIND_REFUND = "REFUND"


# запрет операции для замороженного кошелька
def _ensure_spendable(wallet: Wallet) -> None:
    """Reject debits from a frozen wallet."""
    if wallet.status == WALLET_FROZEN:
        raise WalletFrozenError(wallet.id)


# пополнение с проверкой потолка баланса
def _credit(wallet: Wallet, amount: Decimal) -> None:
    """Add to a balance, refusing to exceed what the column can hold."""
    new_balance = wallet.balance + amount
    if new_balance > MAX_BALANCE:
        raise BalanceLimitError(wallet.id, new_balance)
    wallet.balance = new_balance


# запись строки в журнал операций
def _record_operation(
    session: AsyncSession,
    wallet: Wallet,
    kind: str,
    amount: Decimal,
    purchase_id: uuid.UUID | None = None,
) -> None:
    """Append one signed ledger row for a movement just applied.

    Called after the balance has been changed, inside the same
    transaction, so the ledger and the balance commit together or not at
    all. ``amount`` is signed: credits positive, debits negative.
    """
    session.add(
        Operation(
            id=uuid.uuid4(),
            wallet_id=wallet.id,
            kind=kind,
            amount=amount,
            balance_after=wallet.balance,
            purchase_id=purchase_id,
        )
    )


# захватить ключ или вернуть прежний ответ
async def _claim_idempotency(
    session: AsyncSession,
    key: str | None,
    wallet_id: uuid.UUID,
    endpoint: str,
    request_hash: str,
) -> dict[str, Any] | None:
    """Claim a key, or return the stored result of the original request.

    ``None`` means "go ahead and do the work". A dict means this exact
    request already succeeded and its result should be replayed verbatim.

    The insert is the claim. A concurrent duplicate blocks on the primary
    key until the first transaction commits, and then takes the replay
    path - so two simultaneous retries cannot both charge.
    """
    if key is None:
        return None

    try:
        async with session.begin_nested():
            session.add(
                IdempotencyKey(
                    key=key,
                    wallet_id=wallet_id,
                    endpoint=endpoint,
                    request_hash=request_hash,
                )
            )
            await session.flush()
        return None
    except IntegrityError:
        pass

    existing = await session.get(IdempotencyKey, key)
    if existing is None:  # pragma: no cover - the row was just rolled back
        raise IdempotencyInProgressError(key)

    if (
        existing.endpoint != endpoint
        or existing.request_hash != request_hash
    ):
        raise IdempotencyConflictError(key)

    if existing.response is None:
        raise IdempotencyInProgressError(key)

    return existing.response


# сохранить ответ под ключом идемпотентности
async def _store_idempotent_result(
    session: AsyncSession,
    key: str | None,
    payload: dict[str, Any],
) -> None:
    """Remember the result so a retry can be answered without redoing it."""
    if key is None:
        return
    record = await session.get(IdempotencyKey, key)
    if record is not None:
        record.response = payload


# прочитать баланс или 404
async def get_balance(
    session: AsyncSession,
    wallet_id: uuid.UUID,
) -> Decimal:
    """Return the current balance or raise ``WalletNotFoundError``."""
    wallet = await session.get(Wallet, wallet_id)
    if wallet is None:
        raise WalletNotFoundError(wallet_id)
    return wallet.balance


# заблокировать строку кошелька (FOR UPDATE)
async def _lock_wallet(
    session: AsyncSession,
    wallet_id: uuid.UUID,
) -> Wallet | None:
    """Load a wallet with a row-level write lock (``FOR UPDATE``)."""
    stmt = select(Wallet).where(Wallet.id == wallet_id).with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# создать кошелёк, пережив гонку вставки
async def _create_wallet_locked(
    session: AsyncSession,
    wallet_id: uuid.UUID,
) -> Wallet:
    """Create a zero-balance wallet, tolerating a concurrent creator.

    Two simultaneous deposits to a brand-new wallet can both see "no row"
    and try to insert. The unique primary key lets only one succeed; the
    loser catches the ``IntegrityError`` (inside a SAVEPOINT so the outer
    transaction survives) and re-selects the now-existing row with a lock.
    """
    try:
        async with session.begin_nested():
            wallet = Wallet(id=wallet_id, balance=Decimal("0"))
            session.add(wallet)
            await session.flush()
        return wallet
    except IntegrityError:
        wallet = await _lock_wallet(session, wallet_id)
        if wallet is None:  # pragma: no cover - should be unreachable
            raise
        return wallet


# пополнение/списание в одной транзакции
async def perform_operation(
    session: AsyncSession,
    wallet_id: uuid.UUID,
    operation_type: OperationType,
    amount: Decimal,
    idempotency_key: str | None = None,
    request_hash: str = "",
) -> dict[str, Any]:
    """Apply a deposit or withdrawal atomically; return the result payload.

    A DEPOSIT auto-creates the wallet if needed; a WITHDRAW against a
    missing wallet raises ``WalletNotFoundError``. A frozen wallet accepts
    deposits but refuses withdrawals.
    """
    async with session.begin():
        replay = await _claim_idempotency(
            session,
            idempotency_key,
            wallet_id,
            "operation",
            request_hash,
        )
        if replay is not None:
            return replay

        wallet = await _lock_wallet(session, wallet_id)

        if wallet is None:
            if operation_type == OperationType.WITHDRAW:
                raise WalletNotFoundError(wallet_id)
            wallet = await _create_wallet_locked(session, wallet_id)

        if operation_type == OperationType.DEPOSIT:
            _credit(wallet, amount)
            kind, signed = KIND_DEPOSIT, amount
        else:
            _ensure_spendable(wallet)
            if wallet.balance < amount:
                raise InsufficientFundsError(
                    wallet_id,
                    required=amount,
                    available=wallet.balance,
                )
            wallet.balance = wallet.balance - amount
            kind, signed = KIND_WITHDRAW, -amount

        await session.flush()
        _record_operation(session, wallet, kind, signed)
        await session.flush()

        payload = {"balance": str(wallet.balance)}
        await _store_idempotent_result(session, idempotency_key, payload)
        return payload


# создать счётчик купона, пережив гонку
async def _ensure_usage_row(session: AsyncSession, code: str) -> None:
    """Make sure the counter row for ``code`` exists.

    Two first-ever uses of a coupon can both find no row and both try to
    insert it. The primary key lets one win; the loser catches the
    IntegrityError inside a SAVEPOINT so the outer transaction survives,
    and then simply proceeds - the row it needed now exists.
    """
    existing = await session.get(CouponUsage, code)
    if existing is not None:
        return
    try:
        async with session.begin_nested():
            session.add(CouponUsage(code=code, used_count=0))
            await session.flush()
    except IntegrityError:
        pass


# забрать использование купона условным UPDATE
async def _claim_coupon(
    session: AsyncSession,
    promo: Promo,
    wallet_id: uuid.UUID,
) -> None:
    """Consume one use of a coupon, or raise.

    The global limit is enforced by a single conditional UPDATE rather
    than a read followed by a write::

        UPDATE coupon_usages SET used_count = used_count + 1
         WHERE code = :code AND used_count < :max_uses

    If the statement matches no row, the limit is already spent. Because
    the check lives in the WHERE clause, there is no gap between deciding
    and claiming for a competitor to slip into - which is the same problem
    ``SELECT ... FOR UPDATE`` solves for the balance, expressed in one
    statement instead of two.

    That UPDATE also locks the counter row until the transaction ends,
    which is what makes the per-wallet count below reliable: every
    redemption of this coupon is serialised behind it.
    """
    await _ensure_usage_row(session, promo.code)

    stmt = (
        update(CouponUsage)
        .where(CouponUsage.code == promo.code)
        .values(used_count=CouponUsage.used_count + 1)
    )
    if promo.max_uses is not None:
        stmt = stmt.where(CouponUsage.used_count < promo.max_uses)

    result = await session.execute(stmt)
    if result.rowcount == 0:
        raise CouponLimitReachedError(promo.code, promo.max_uses)

    if promo.max_uses_per_wallet is not None:
        used_here = await session.scalar(
            select(func.count())
            .select_from(CouponRedemption)
            .where(
                CouponRedemption.coupon_code == promo.code,
                CouponRedemption.wallet_id == wallet_id,
            )
        )
        if used_here >= promo.max_uses_per_wallet:
            raise CouponAlreadyUsedError(
                promo.code,
                promo.max_uses_per_wallet,
            )


# проверка лимитов без списания (предпросмотр)
async def validate_coupon(
    session: AsyncSession,
    promo: Promo,
    wallet_id: uuid.UUID,
) -> int | None:
    """Read-only limit check for the advisory endpoint.

    Returns how many uses are left globally, or ``None`` when unlimited.
    Deliberately takes no locks and consumes nothing: quoting a cart must
    never burn a coupon. The verdict can therefore be stale, which is
    exactly why the purchase path re-checks while holding the counter.
    """
    usage = await session.get(CouponUsage, promo.code)
    used = usage.used_count if usage is not None else 0

    if promo.max_uses is not None and used >= promo.max_uses:
        raise CouponLimitReachedError(promo.code, promo.max_uses)

    if promo.max_uses_per_wallet is not None:
        used_here = await session.scalar(
            select(func.count())
            .select_from(CouponRedemption)
            .where(
                CouponRedemption.coupon_code == promo.code,
                CouponRedemption.wallet_id == wallet_id,
            )
        )
        if used_here >= promo.max_uses_per_wallet:
            raise CouponAlreadyUsedError(
                promo.code,
                promo.max_uses_per_wallet,
            )

    if promo.max_uses is None:
        return None
    return promo.max_uses - used


# покупка: купон + списание + запись + журнал
async def purchase(
    session: AsyncSession,
    wallet_id: uuid.UUID,
    subtotal: Decimal,
    discount: Decimal,
    total: Decimal,
    promo: Promo | None,
    items: list[dict[str, Any]],
    idempotency_key: str | None = None,
    request_hash: str = "",
) -> dict[str, Any]:
    """Charge ``total`` and record the purchase; return its id and balance.

    Everything happens in one transaction: claiming the coupon, checking
    affordability, debiting the wallet, writing the purchase and recording
    the redemption. So a coupon use cannot survive a failed payment, and a
    charge cannot exist without a refundable trace of it.

    Locks are taken in a fixed order - coupon counter, then wallet - and
    the refund path takes purchase, then wallet. No two paths ever want
    the same pair in opposite order, so they cannot deadlock.
    """
    async with session.begin():
        replay = await _claim_idempotency(
            session,
            idempotency_key,
            wallet_id,
            "purchase",
            request_hash,
        )
        if replay is not None:
            return replay

        if promo is not None:
            await _claim_coupon(session, promo, wallet_id)

        wallet = await _lock_wallet(session, wallet_id)

        if wallet is None:
            raise WalletNotFoundError(wallet_id)

        _ensure_spendable(wallet)

        if wallet.balance < total:
            raise InsufficientFundsError(
                wallet_id,
                required=total,
                available=wallet.balance,
            )

        wallet.balance = wallet.balance - total
        record = Purchase(
            id=uuid.uuid4(),
            wallet_id=wallet_id,
            promo_code=promo.code if promo is not None else None,
            subtotal=subtotal,
            discount=discount,
            total=total,
            refunded_total=Decimal("0"),
            items=items,
        )
        session.add(record)
        await session.flush()

        if promo is not None:
            session.add(
                CouponRedemption(
                    id=uuid.uuid4(),
                    coupon_code=promo.code,
                    wallet_id=wallet_id,
                    purchase_id=record.id,
                )
            )

        _record_operation(
            session,
            wallet,
            KIND_PURCHASE,
            -total,
            purchase_id=record.id,
        )
        await session.flush()

        payload = {
            "purchase_id": str(record.id),
            "positions": len(items),
            "items_count": sum(item["quantity"] for item in items),
            "promo_code": promo.code if promo is not None else None,
            "subtotal": str(subtotal),
            "discount": str(discount),
            "total": str(total),
            "balance": str(wallet.balance),
        }
        await _store_idempotent_result(session, idempotency_key, payload)
        return payload


# возврат под блокировкой покупки и кошелька
async def refund(
    session: AsyncSession,
    wallet_id: uuid.UUID,
    purchase_id: uuid.UUID,
    amount: Decimal | None = None,
    idempotency_key: str | None = None,
    request_hash: str = "",
) -> dict[str, Any]:
    """Refund a purchase, fully by default or partially by ``amount``.

    Two rows are locked: the purchase and then the wallet, always in that
    order. A consistent order is what keeps concurrent refunds from
    deadlocking against each other. The purchase lock is also what makes
    double refunds impossible: the second request waits, then reads the
    updated ``refunded_total`` and is rejected instead of paying out
    twice.
    """
    async with session.begin():
        replay = await _claim_idempotency(
            session,
            idempotency_key,
            wallet_id,
            "refund",
            request_hash,
        )
        if replay is not None:
            return replay

        stmt = (
            select(Purchase)
            .where(
                Purchase.id == purchase_id,
                Purchase.wallet_id == wallet_id,
            )
            .with_for_update()
        )
        record = (await session.execute(stmt)).scalar_one_or_none()

        if record is None:
            raise PurchaseNotFoundError(purchase_id)

        remaining = record.total - record.refunded_total
        requested = remaining if amount is None else amount

        if requested <= 0 or requested > remaining:
            raise RefundNotAllowedError(purchase_id, requested, remaining)

        wallet = await _lock_wallet(session, wallet_id)
        if wallet is None:  # pragma: no cover - guarded by foreign key
            raise WalletNotFoundError(wallet_id)

        _credit(wallet, requested)
        record.refunded_total = record.refunded_total + requested
        await session.flush()

        _record_operation(
            session,
            wallet,
            KIND_REFUND,
            requested,
            purchase_id=record.id,
        )
        await session.flush()

        payload = {
            "purchase_id": str(record.id),
            "refunded": str(requested),
            "refunded_total": str(record.refunded_total),
            "refundable_remaining": str(
                record.total - record.refunded_total
            ),
            "balance": str(wallet.balance),
        }
        await _store_idempotent_result(session, idempotency_key, payload)
        return payload


# страница журнала операций кошелька
async def list_operations(
    session: AsyncSession,
    wallet_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[Operation]:
    """Return a wallet's ledger, newest first.

    Raises ``WalletNotFoundError`` for an unknown wallet rather than
    returning an empty list, so a typo in the id is not mistaken for a
    wallet with no history.
    """
    wallet = await session.get(Wallet, wallet_id)
    if wallet is None:
        raise WalletNotFoundError(wallet_id)

    stmt = (
        select(Operation)
        .where(Operation.wallet_id == wallet_id)
        .order_by(Operation.created_at.desc(), Operation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars())


# сменить статус под блокировкой строки
async def set_status(
    session: AsyncSession,
    wallet_id: uuid.UUID,
    status: str,
) -> str:
    """Freeze or unfreeze a wallet, under the same row lock as spending.

    Taking the lock matters: without it a freeze could land in the middle
    of a purchase that already passed its own status check.
    """
    async with session.begin():
        wallet = await _lock_wallet(session, wallet_id)
        if wallet is None:
            raise WalletNotFoundError(wallet_id)
        wallet.status = status
        await session.flush()
        return wallet.status
