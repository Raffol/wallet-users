# Деньги: точная арифметика и округление до копеек
"""Money helpers: exact decimal arithmetic with explicit rounding."""
from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")

# The largest value NUMERIC(20, 2) can hold: 18 integer digits plus cents.
# The cap is the storage limit rather than an invented business figure, so
# no balance can ever overflow its column. A single request is already
# bounded by the Amount type; only an accumulating balance could exceed it.
MAX_BALANCE = Decimal("9" * 18 + ".99")


# округлить до копеек (ROUND_HALF_UP, не банковское)
def to_cents(value: Decimal) -> Decimal:
    """Round a monetary value to two decimal places.

    The rounding mode is stated explicitly on purpose. Decimal's default
    is ROUND_HALF_EVEN (banker's rounding), which turns 0.125 into 0.12,
    whereas commerce normally expects 0.13. Percentage discounts produce
    such half-cent values constantly, so leaving the mode implicit would
    make totals disagree with what a customer computes by hand.
    """
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)
