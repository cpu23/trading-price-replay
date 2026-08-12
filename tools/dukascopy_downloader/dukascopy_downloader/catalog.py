from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone


# US Eastern DST: since 2007 it runs from 02:00 local on the second Sunday of
# March to 02:00 local on the first Sunday of November; earlier years used the
# first Sunday of April through the last Sunday of October.
def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _last_sunday(year: int, month: int) -> date:
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = first_of_next - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def _dst_start_utc(year: int) -> datetime:
    if year >= 2007:
        start = _nth_sunday(year, 3, 2)
    else:
        start = _nth_sunday(year, 4, 1)
    # 02:00 EST == 07:00 UTC.
    return datetime(start.year, start.month, start.day, 7, tzinfo=timezone.utc)


def _dst_end_utc(year: int) -> datetime:
    if year >= 2007:
        end = _nth_sunday(year, 11, 1)
    else:
        end = _last_sunday(year, 10)
    # 02:00 EDT == 06:00 UTC.
    return datetime(end.year, end.month, end.day, 6, tzinfo=timezone.utc)


def _eastern_utc_offset(value: datetime) -> timedelta:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if _dst_start_utc(value.year) <= value < _dst_end_utc(value.year):
        return timedelta(hours=-4)
    return timedelta(hours=-5)


# FX weekly session: Sunday 17:00 through Friday 17:00 US Eastern. Expressed in
# minutes since the start of the FX week (Sunday 00:00), with Sunday as day 0.
_FX_SESSION_START_MINUTES = 17 * 60
_FX_SESSION_END_MINUTES = 5 * 24 * 60 + 17 * 60


def _fx_session_open(value: datetime) -> bool:
    local = value + _eastern_utc_offset(value)
    minutes = ((local.weekday() + 1) % 7) * 24 * 60 + local.hour * 60 + local.minute
    return _FX_SESSION_START_MINUTES <= minutes < _FX_SESSION_END_MINUTES


@dataclass(frozen=True)
class Instrument:
    symbol: str
    display_name: str
    asset_class: str
    price_scale: int
    point_size: float
    precision: int
    tick_volume_scale: int
    earliest_date: date
    native_timeframes: tuple[str, ...]
    session_model: str
    closed_weekdays: tuple[int, ...]
    sparse_candles_expected: bool
    open_hours_utc: tuple[int, ...] = tuple(range(24))
    maintenance_hours_utc: tuple[int, ...] = ()
    maintenance_ranges_utc: tuple[tuple[int, int], ...] = ()
    known_closures: tuple[date, ...] = ()

    def expected_open(self, value: datetime) -> bool:
        if value.date() < self.earliest_date or value.date() in self.known_closures:
            return False
        if self.session_model == "continuous":
            return True
        if self.session_model == "fx_24x5":
            return _fx_session_open(value)
        minute_of_day = value.hour * 60 + value.minute
        return (
            value.weekday() not in self.closed_weekdays
            and value.hour in self.open_hours_utc
            and value.hour not in self.maintenance_hours_utc
            and not any(start <= minute_of_day < end for start, end in self.maintenance_ranges_utc)
        )

    def describe(self) -> dict[str, object]:
        value = asdict(self)
        value["earliest_date"] = self.earliest_date.isoformat()
        value["closed_weekdays"] = [("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[day] for day in self.closed_weekdays]
        value["known_closures"] = [item.isoformat() for item in self.known_closures]
        return value


def _forex(symbol: str, display_name: str, scale: int = 100_000, precision: int = 5) -> Instrument:
    return Instrument(symbol, display_name, "forex", scale, 1 / scale, precision, 1_000_000, date(2003, 1, 1), ("M1", "H1", "D1"), "fx_24x5", (), True)


INSTRUMENTS = {
    "EURUSD": _forex("EURUSD", "Euro / US Dollar"),
    "GBPUSD": _forex("GBPUSD", "British Pound / US Dollar"),
    "USDJPY": _forex("USDJPY", "US Dollar / Japanese Yen", 1_000, 3),
    "DEUIDXEUR": Instrument("DEUIDXEUR", "Germany 40 Index", "index", 1_000, 0.001, 3, 1_000_000, date(2013, 9, 30), ("M1", "H1", "D1"), "index_weekday", (5, 6), True, tuple(range(7, 21))),
    "XAUUSD": Instrument("XAUUSD", "Gold / US Dollar", "metal", 1_000, 0.001, 3, 1_000_000, date(2003, 5, 5), ("M1", "H1", "D1"), "metal_24x5", (5, 6), False, tuple(range(24)), (22,)),
    "BTCUSD": Instrument("BTCUSD", "Bitcoin / US Dollar", "crypto", 10, 0.1, 1, 1_000_000, date(2017, 5, 7), ("M1", "H1", "D1"), "continuous", (), False),
    "USATECHIDXUSD": Instrument("USATECHIDXUSD", "US Tech 100 Index", "index", 1_000, 0.001, 3, 1_000_000, date(2017, 11, 5), ("M1", "H1", "D1"), "index_weekday", (5, 6), True, tuple(range(1, 24)), (22,), ((21 * 60 + 15, 22 * 60),)),
}


def get_instrument(symbol: str) -> Instrument:
    normalized = symbol.upper()
    try:
        return INSTRUMENTS[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported instrument {symbol!r}; use --list-instruments") from exc
