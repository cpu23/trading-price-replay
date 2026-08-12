from __future__ import annotations

from .domain import DisplayBar


def sma(bars: list[DisplayBar], period: int = 35) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    closes: list[float] = []
    for bar in bars:
        closes.append(bar.close)
        if len(closes) >= period:
            values.append({"time": bar.timestamp.isoformat(), "value": sum(closes[-period:]) / period})
    return values

