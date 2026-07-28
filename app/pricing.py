# Расчёт корзины: единый для покупки и предпросмотра
"""Cart pricing: one place that turns a cart into an amount to charge."""
from dataclasses import dataclass
from decimal import Decimal

from app.money import to_cents
from app.promos import Promo, compute_discount


# разбивка: сумма, скидка, итог
@dataclass(frozen=True)
class CartTotals:
    """Breakdown of what the customer pays and why."""

    subtotal: Decimal
    discount: Decimal
    total: Decimal


# применить купон к сумме корзины
def price_cart(subtotal: Decimal, promo: Promo | None) -> CartTotals:
    """Apply a resolved coupon to a subtotal.

    Both the purchase endpoint and the advisory check call this, so a
    quoted total and a charged total can never drift apart.
    """
    if promo is None:
        return CartTotals(subtotal, Decimal("0.00"), subtotal)

    discount = compute_discount(subtotal, promo)
    return CartTotals(subtotal, discount, to_cents(subtotal - discount))
