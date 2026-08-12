from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median

from .catalog import Instrument
from .config import TIMEFRAME_SECONDS
from .models import Candle, Tick


def bucket_for(value: datetime, timeframe: str) -> datetime:
    utc = value.astimezone(timezone.utc)
    seconds = TIMEFRAME_SECONDS[timeframe]
    epoch = int(utc.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


@dataclass
class CandleBuilder:
    time: datetime
    offer_side: str
    volume_type: str
    instrument: Instrument
    prices: list[float] = field(default_factory=list)
    bid_volume: float = 0
    ask_volume: float = 0
    spreads: list[int] = field(default_factory=list)

    def add(self, tick: Tick) -> None:
        price = tick.bid if self.offer_side == "bid" else tick.ask if self.offer_side == "ask" else (tick.bid + tick.ask) / 2
        self.prices.append(price)
        self.bid_volume += tick.bid_volume
        self.ask_volume += tick.ask_volume
        self.spreads.append(max(0, round((tick.ask - tick.bid) / self.instrument.point_size)))

    def build(self) -> Candle:
        volumes = {"bid": self.bid_volume, "ask": self.ask_volume, "total": self.bid_volume + self.ask_volume, "ticks": len(self.prices)}
        return Candle(self.time, self.prices[0], max(self.prices), min(self.prices), self.prices[-1], len(self.prices), volumes[self.volume_type], round(median(self.spreads)))


def aggregate_ticks(ticks: list[Tick], timeframe: str, offer_side: str, volume_type: str, instrument: Instrument) -> list[Candle]:
    aggregator = TickAggregator(timeframe, offer_side, volume_type, instrument)
    aggregator.add_ticks(ticks)
    return aggregator.candles()


class TickAggregator:
    def __init__(self, timeframe: str, offer_side: str, volume_type: str, instrument: Instrument) -> None:
        self.timeframe = timeframe
        self.offer_side = offer_side
        self.volume_type = volume_type
        self.instrument = instrument
        self.builders: dict[datetime, CandleBuilder] = {}

    def add_ticks(self, ticks: list[Tick]) -> None:
        for tick in sorted(ticks, key=lambda item: item.time):
            bucket = bucket_for(tick.time, self.timeframe)
            self.builders.setdefault(bucket, CandleBuilder(bucket, self.offer_side, self.volume_type, self.instrument)).add(tick)

    def candles(self) -> list[Candle]:
        return [self.builders[key].build() for key in sorted(self.builders)]
