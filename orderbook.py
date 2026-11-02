"""Validated, read-only price-level books; subscription sequencing lives elsewhere."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


class BookError(ValueError):
    pass


def number(value):
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise BookError("Expected a fixed-point number")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise BookError("Invalid number") from None
    if not result.is_finite():
        raise BookError("Non-finite number")
    return result


def price(value):
    result = number(value)
    if not 0 <= result <= 1:
        raise BookError("Price outside [0, 1]")
    return result


@dataclass
class Book:
    yes: dict = field(default_factory=dict)
    no: dict = field(default_factory=dict)
    valid: bool = False

    def invalidate(self):
        self.valid = False

    def replace_snapshot(self, payload):
        try:
            sides = []
            for side in ("yes_dollars_fp", "no_dollars_fp"):
                levels = {}
                if not isinstance(payload[side], list):
                    raise BookError("Invalid levels")
                for p, q in payload[side]:
                    p, q = price(p), number(q)
                    if q < 0 or p in levels:
                        raise BookError("Negative quantity or duplicate level")
                    levels[p] = q
                sides.append({p: q for p, q in levels.items() if q})
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            self.invalidate()
            raise BookError("Invalid snapshot") from exc
        self.yes, self.no = sides
        self.valid = True

    def apply_delta(self, payload):
        try:
            if not self.valid:
                raise BookError("Snapshot required")
            side = payload["side"]
            if side not in ("yes", "no"):
                raise BookError("Invalid side")
            p, change = price(payload["price_dollars"]), number(payload["delta_fp"])
            levels = self.yes if side == "yes" else self.no
            q = levels.get(p, Decimal(0)) + change
            if not q.is_finite() or q < 0:
                raise BookError("Invalid resulting quantity")
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            self.invalidate()
            raise BookError("Invalid delta") from exc
        if q:
            levels[p] = q
        else:
            levels.pop(p, None)

    def quote(self):
        if not self.valid:
            raise BookError("Book is not synchronized")
        y, n = max(self.yes, default=None), max(self.no, default=None)
        return y, Decimal(1) - n if n is not None else None, n, Decimal(1) - y if y is not None else None

    def top(self):
        return tuple(str(v) if v is not None else "—" for v in self.quote())
