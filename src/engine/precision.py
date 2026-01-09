from decimal import ROUND_DOWN, Decimal


class Precision:
    """
    Handles precision logic for a specific number of decimal places.
    Used for rounding prices and sizes, and converting to integer representations (atoms).
    """

    def __init__(self, decimals: int):
        self.decimals = decimals
        self.quantizer = Decimal("1." + "0" * decimals)  # e.g., 0.0001 for decimals=4
        self.multiplier = Decimal("10") ** decimals

    def round(self, value: Decimal) -> Decimal:
        if not isinstance(value, Decimal):
            raise TypeError(f"Precision.round expected Decimal, got {type(value)}")

        return value.quantize(Decimal("10") ** -self.decimals, rounding=ROUND_DOWN)

    def to_int(self, value: Decimal) -> int:
        d_val = self.round(value)
        return int(d_val * self.multiplier)

    def from_int(self, value: int) -> Decimal:
        return Decimal(value) / self.multiplier
