from decimal import Decimal
from typing import Union

class Precision:
    """
    Handles precision logic for a specific number of decimal places.
    Used for rounding prices and sizes, and converting to integer representations (atoms).
    """
    def __init__(self, decimals: int):
        self.decimals = decimals
        self.quantizer = Decimal("1." + "0" * decimals) # e.g., 0.0001 for decimals=4
        self.multiplier = Decimal("10") ** decimals

    def round(self, value: Union[float, Decimal, int, str]) -> Decimal:
        """
        Rounds the value to the specified number of decimal places.
        Returns a Decimal.
        """
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        
        # QUANTIZE rounds to the nearest even number by default (ROUND_HALF_EVEN),
        # but standard financial rounding often expects ROUND_HALF_UP.
        # Python's round() uses ROUND_HALF_EVEN. 
        # For simplicity and consistency with previous float round(), we used standard round().
        # Here we use quantized rounding.
        
        return value.quantize(Decimal("10") ** -self.decimals)

    def to_int(self, value: Union[float, Decimal, int, str]) -> int:
        """
        Converts the value to its integer representation (atoms) based on precision.
        e.g. value=1.23, decimals=2 -> 123
        """
        d_val = self.round(value)
        return int(d_val * self.multiplier)

    def from_int(self, value: int) -> Decimal:
        """
        Converts an integer (atoms) back to Decimal value.
        e.g. value=123, decimals=2 -> 1.23
        """
        return Decimal(value) / self.multiplier
