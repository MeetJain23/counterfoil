"""Money is integer paise. Never a float, anywhere, for any reason.

Floating point on currency is how reconciliation systems quietly lose rupees.
Every amount in Counterfoil is an ``int`` count of the smallest unit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Money:
    paise: int
    currency: str = "INR"

    def __post_init__(self) -> None:
        if not isinstance(self.paise, int) or isinstance(self.paise, bool):
            raise TypeError(f"Money.paise must be int, got {type(self.paise).__name__}")

    @classmethod
    def rupees(cls, amount: int | str) -> Money:
        """Build from whole rupees. Strings allowed so callers never touch float."""
        return cls(int(amount) * 100)

    @classmethod
    def zero(cls, currency: str = "INR") -> Money:
        return cls(0, currency)

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.paise + other.paise, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.paise - other.paise, self.currency)

    def __mul__(self, factor: int) -> Money:
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError("Money can only be scaled by int")
        return Money(self.paise * factor, self.currency)

    @property
    def as_rupees_str(self) -> str:
        sign = "-" if self.paise < 0 else ""
        whole, frac = divmod(abs(self.paise), 100)
        return f"{sign}{whole:,}.{frac:02d}"

    def __str__(self) -> str:
        return f"Rs {self.as_rupees_str}"
