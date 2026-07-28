# Каталог купонов и правила скидок
"""Coupons: the discount rules and the conditions attached to them.

Discounts never arrive in the request body. A client that could name its
own discount could pay nothing, so the request carries only a *code* and
the rule behind it lives here, on the server. In a production system this
catalogue would be a database table so marketing can change offers
without a redeploy; keeping it in code here keeps the example small.

How many times a coupon has actually been used is a different matter:
that *is* stored in the database, because two parallel purchases must not
both be allowed to take the last remaining use. See ``models.CouponUsage``.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from app.money import to_cents

HUNDRED = Decimal("100")
_UTC = timezone.utc


# тип скидки: процент или фикс
class DiscountType(str, Enum):
    """How a coupon reduces the cart subtotal."""

    PERCENT = "PERCENT"
    FIXED = "FIXED"


# неизвестный промокод
class UnknownPromoCodeError(Exception):
    """Raised when the supplied coupon code does not exist."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Unknown promo code {code!r}")


# корзина не дотягивает до минимума
class PromoNotApplicableError(Exception):
    """Raised when a coupon exists but the cart does not qualify."""

    def __init__(
        self,
        code: str,
        min_total: Decimal,
        subtotal: Decimal,
    ) -> None:
        self.code = code
        self.min_total = min_total
        self.subtotal = subtotal
        super().__init__(
            f"Promo code {code!r} requires a subtotal of at least "
            f"{min_total}, cart subtotal is {subtotal}"
        )


# купон вне срока действия
class CouponWindowError(Exception):
    """Raised when a coupon is used outside its validity window."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"Promo code {code!r} {reason}")


# исчерпан общий лимит купона
class CouponLimitReachedError(Exception):
    """Raised when a coupon has been used as many times as allowed."""

    def __init__(self, code: str, max_uses: int) -> None:
        self.code = code
        self.max_uses = max_uses
        super().__init__(
            f"Promo code {code!r} has reached its limit of "
            f"{max_uses} use(s)"
        )


# исчерпан лимит купона на кошелёк
class CouponAlreadyUsedError(Exception):
    """Raised when this wallet has exhausted its own allowance."""

    def __init__(self, code: str, max_per_wallet: int) -> None:
        self.code = code
        self.max_per_wallet = max_per_wallet
        super().__init__(
            f"Promo code {code!r} is limited to {max_per_wallet} use(s) "
            f"per wallet and has already been used"
        )


# одно предложение: скидка и условия
@dataclass(frozen=True)
class Promo:
    """A single offer.

    ``value`` is a percentage for PERCENT and an absolute amount for
    FIXED. ``None`` on any limit means "no limit".
    """

    code: str
    discount_type: DiscountType
    value: Decimal
    min_total: Decimal = Decimal("0")
    max_uses: int | None = None
    max_uses_per_wallet: int | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None


_ALL_PROMOS = (
    Promo("SALE10", DiscountType.PERCENT, Decimal("10")),
    Promo("SALE25", DiscountType.PERCENT, Decimal("25")),
    Promo("HALF", DiscountType.PERCENT, Decimal("50")),
    Promo(
        "MINUS100",
        DiscountType.FIXED,
        Decimal("100"),
        min_total=Decimal("500"),
    ),
    Promo("CLEARANCE", DiscountType.FIXED, Decimal("5000")),
    # One per customer.
    Promo(
        "ONCE",
        DiscountType.PERCENT,
        Decimal("10"),
        max_uses_per_wallet=1,
    ),
    # First three purchases overall, whoever gets there first.
    Promo(
        "FIRST3",
        DiscountType.FIXED,
        Decimal("100"),
        max_uses=3,
    ),
    # Two per customer, five in total: both limits at once.
    Promo(
        "SPRING",
        DiscountType.PERCENT,
        Decimal("10"),
        max_uses=5,
        max_uses_per_wallet=2,
    ),
    Promo(
        "EXPIRED",
        DiscountType.PERCENT,
        Decimal("10"),
        valid_to=datetime(2020, 1, 1, tzinfo=_UTC),
    ),
    Promo(
        "FUTURE",
        DiscountType.PERCENT,
        Decimal("10"),
        valid_from=datetime(2099, 1, 1, tzinfo=_UTC),
    ),
)

PROMOS: dict[str, Promo] = {promo.code: promo for promo in _ALL_PROMOS}


# найти купон без учёта регистра
def get_promo(code: str) -> Promo:
    """Look a coupon up case-insensitively."""
    promo = PROMOS.get(code.strip().upper())
    if promo is None:
        raise UnknownPromoCodeError(code)
    return promo


# проверить срок действия (без БД)
def check_window(promo: Promo, now: datetime | None = None) -> None:
    """Reject a coupon that is expired or not yet active.

    This check needs no database access and no lock: the answer depends
    only on the clock, so it runs before anything is claimed.
    """
    moment = now or datetime.now(_UTC)

    if promo.valid_from is not None and moment < promo.valid_from:
        raise CouponWindowError(
            promo.code,
            f"is not valid until {promo.valid_from.date()}",
        )

    if promo.valid_to is not None and moment > promo.valid_to:
        raise CouponWindowError(
            promo.code,
            f"expired on {promo.valid_to.date()}",
        )


# размер скидки, обрезанный по сумме корзины
def compute_discount(subtotal: Decimal, promo: Promo) -> Decimal:
    """Return the effective discount for ``subtotal``.

    The result is capped at the subtotal, so a generous fixed offer makes
    an order free rather than making the total negative and crediting the
    customer.
    """
    if subtotal < promo.min_total:
        raise PromoNotApplicableError(
            promo.code,
            promo.min_total,
            subtotal,
        )

    if promo.discount_type is DiscountType.PERCENT:
        raw = to_cents(subtotal * promo.value / HUNDRED)
    else:
        raw = to_cents(promo.value)

    return min(raw, subtotal)
