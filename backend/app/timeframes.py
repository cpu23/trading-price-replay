from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .domain import Bar, DisplayBar, Profile, Timeframe

MINUTES: dict[Timeframe, int] = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
NEW_YORK = ZoneInfo("America/New_York")


def bucket_for(timestamp: datetime, timeframe: Timeframe, profile: Profile) -> datetime:
    timestamp = timestamp.astimezone(timezone.utc)
    if profile == "utc_aligned":
        if timeframe == "1d":
            return timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes = MINUTES[timeframe]
        epoch_minutes = int(timestamp.timestamp() // 60)
        return datetime.fromtimestamp((epoch_minutes // minutes) * minutes * 60, timezone.utc)

    local = timestamp.astimezone(NEW_YORK)
    session_date = local.date() if local.hour >= 17 else (local - timedelta(days=1)).date()
    anchor = datetime.combine(session_date, datetime.min.time(), NEW_YORK).replace(hour=17)
    if timeframe == "1d":
        return anchor.astimezone(timezone.utc)
    elapsed = int((local - anchor).total_seconds() // 60)
    start = anchor + timedelta(minutes=(elapsed // MINUTES[timeframe]) * MINUTES[timeframe])
    return start.astimezone(timezone.utc)


def resample(bars: list[Bar], timeframe: Timeframe, profile: Profile) -> list[DisplayBar]:
    if not bars:
        return []
    result: list[DisplayBar] = []
    for bar in bars:
        bucket = bucket_for(bar.timestamp, timeframe, profile)
        if not result or result[-1].timestamp != bucket:
            result.append(DisplayBar(
                timestamp=bucket, open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                volume=bar.volume, timeframe=timeframe, is_partial=True,
                source_1m_start_time=bar.timestamp, source_1m_end_time=bar.timestamp,
            ))
        else:
            current = result[-1]
            current.high = max(current.high, bar.high)
            current.low = min(current.low, bar.low)
            current.close = bar.close
            current.volume += bar.volume
            current.source_1m_end_time = bar.timestamp
    for item in result[:-1]:
        item.is_partial = False
    return result

