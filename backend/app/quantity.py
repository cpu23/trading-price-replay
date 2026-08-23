"""Centralized close-quantity policy.

Every backend close entry point resolves its executed fill quantity and the
resulting stored remainder through `resolve_close`; no caller implements its
own epsilon, lot step, or rounding. Quantities stay binary floats on the wire
and in storage (legacy sessions load unchanged), so the policy:

- subtracts exact decimal values of the floats' shortest representations
  (`Decimal(str(value))`), which removes ordinary decimal drift: three 0.1
  closes of a 0.3 position land on exactly 0.0 instead of a float residue,
  and the stored remainder after a 0.1 close displays as 0.2, never as
  0.19999999999999998;
- admits an ULP-scaled tolerance *only* for representational overshoot or
  residual — the binary-storage artifacts that appear when a position is
  closed in many chunks (e.g. the 1e-16 dust after three
  0.3333333333333333 closes of a 1.0 position). The window is a fixed number
  of ULPs of the larger quantity in play, never an absolute or relative
  epsilon, and there is no symbol lot step. Tiny positions keep the same
  scale-sensitive treatment as large ones: the required 5e-13 remainder on a
  1.0 position and 9e-13 remainder on a 1e-12 position both remain open;
- a tolerated final close books the actual pre-close remainder as the fill
  quantity and stores remainder exactly 0.0, so the final close never
  fabricates quantity;
- a requested quantity that genuinely exceeds the remainder (beyond the
  window) is rejected as oversize.
"""

from __future__ import annotations

from decimal import Decimal
from math import ulp

# A close that leaves a residual within this many ULPs of the larger quantity
# in play is the intended final close: the residual is a float-storage
# artifact, not a position the user wants to keep. The window is wide enough
# that even a position closed in ~30 equal chunks (the worst repeated-chunk
# accumulation, ~135 ULPs) still completes, yet narrow enough that the
# smallest legitimate remainder the policy must keep open (5e-13 on a 1.0
# position, ~2250 ULPs) stays open with an order of magnitude of headroom.
_ULP_WINDOW = 256


def _tolerance(remaining_quantity: float, requested: float) -> Decimal:
    """Representational residual/overshoot to absorb, scaled only in ULPs."""
    value = _ULP_WINDOW * max(ulp(remaining_quantity), ulp(requested))
    return Decimal.from_float(value)


def resolve_close(remaining_quantity: float, requested: float) -> float:
    """Return the canonical remainder for one validated close request.

    Decimal subtraction removes ordinary base-10 drift. A result within the
    ULP-scaled representational window is canonical zero; an overshoot beyond
    that window is a genuine oversized close and is rejected.
    """
    residual = Decimal(str(remaining_quantity)) - Decimal(str(requested))
    tolerance = _tolerance(remaining_quantity, requested)
    if residual < -tolerance:
        raise ValueError("close quantity must be positive and no greater than remaining quantity")
    if residual <= tolerance:
        return 0.0
    return float(residual)
