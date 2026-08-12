from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import date
from pathlib import Path

from .aggregate import TickAggregator
from .catalog import INSTRUMENTS, Instrument, get_instrument
from .config import PRICES, TIMEFRAMES, VOLUMES, default_output_dir, inclusive_date_range, output_paths
from .models import FetchResult, SourcePeriod
from .output import manifest, provenance, write_candles, write_json, write_ticks
from .sources import append_status, fetch_to_cache, native_periods, parse_native_payload, parse_tick_payload, tick_periods
from .validation import validate_candles, validate_ticks


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Download audited Dukascopy tick and candle data.")
    value.add_argument("symbols", nargs="*", help="Curated Dukascopy instrument symbols.")
    discovery = value.add_mutually_exclusive_group()
    discovery.add_argument("--list-instruments", action="store_true", help="List supported instruments.")
    discovery.add_argument("--describe", metavar="SYMBOL", help="Describe one supported instrument.")
    value.add_argument("--start", type=date.fromisoformat, help="Inclusive UTC start date (YYYY-MM-DD).")
    value.add_argument("--end", type=date.fromisoformat, help="Inclusive UTC end date (YYYY-MM-DD).")
    value.add_argument("--timeframe", choices=TIMEFRAMES, help="TICK, M1, H1, or D1.")
    value.add_argument("--source", choices=("auto", "native", "tick"), default="auto")
    value.add_argument("--price", choices=PRICES, default=None, help="Candle offer side; defaults to bid.")
    value.add_argument("--volume", choices=VOLUMES, default=None, help="Candle volume semantics; defaults to native.")
    value.add_argument("--allow-partial", action="store_true", help="Publish incomplete outputs marked untrusted.")
    value.add_argument("--refresh", action="store_true", help="Redownload source files instead of using cache.")
    value.add_argument("--output-dir", type=Path, default=default_output_dir())
    value.add_argument("--workers", type=int, default=5)
    value.add_argument("--retries", type=int, default=3)
    value.add_argument("--timeout", type=float, default=30)
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = parser().parse_args(argv)
    if args.list_instruments or args.describe:
        return args
    if not args.symbols or args.start is None or args.end is None or args.timeframe is None:
        parser().error("symbols, --start, --end, and --timeframe are required for downloads")
    if args.workers < 1 or args.retries < 1 or args.timeout <= 0:
        parser().error("--workers and --retries must be positive integers; --timeout must be positive")
    if args.timeframe == "TICK":
        if args.price is not None or args.volume is not None:
            parser().error("--price and --volume are not valid with --timeframe TICK")
        if args.source == "native":
            parser().error("--source native is not valid with --timeframe TICK")
        args.source_kind = "tick"
        return args
    args.price = args.price or "bid"
    args.volume = args.volume or "native"
    args.source_kind = select_source(args.source, args.price, args.volume)
    return args


def select_source(source: str, price: str, volume: str) -> str:
    if source == "native":
        if price == "mid" or volume != "native":
            raise ValueError("--source native requires --price bid|ask and --volume native")
        return "native_candle"
    if source == "tick":
        if volume == "native":
            raise ValueError("--source tick requires --volume bid|ask|total|ticks")
        return "tick"
    return "native_candle" if price in {"bid", "ask"} and volume == "native" else "tick"


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.list_instruments:
            for symbol in sorted(INSTRUMENTS):
                item = INSTRUMENTS[symbol]
                print(f"{item.symbol:<16} {item.asset_class:<8} {item.display_name}")
            return 0
        if args.describe:
            print(json.dumps(get_instrument(args.describe).describe(), indent=2, sort_keys=True))
            return 0
        start, end = inclusive_date_range(args.start, args.end)
        failures = 0
        for symbol in args.symbols:
            failures += run_symbol(get_instrument(symbol), args, start, end)
        return 1 if failures else 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def run_symbol(instrument: Instrument, args: argparse.Namespace, start, end) -> int:
    source_kind = args.source_kind
    if source_kind == "native_candle" and args.timeframe not in instrument.native_timeframes:
        raise ValueError(f"{instrument.symbol} does not support native {args.timeframe} candles")
    offer_side = None if args.timeframe == "TICK" else args.price
    volume = None if args.timeframe == "TICK" else args.volume
    source_label = "native" if source_kind == "native_candle" else "tick"
    volume_label = "raw" if volume is None else volume
    price_label = "raw" if offer_side is None else offer_side
    stem = (
        f"{instrument.symbol}_DUKASCOPY_TICK_{args.start:%Y%m%d}_{args.end:%Y%m%d}"
        if args.timeframe == "TICK"
        else f"{instrument.symbol}_DUKASCOPY_{args.timeframe}_{price_label}_{volume_label}_{args.start:%Y%m%d}_{args.end:%Y%m%d}"
    )
    paths = output_paths(args.output_dir, stem)
    cache_root = args.output_dir / "cache"
    periods = (
        native_periods(instrument, args.timeframe, offer_side, start, end, cache_root)
        if source_kind == "native_candle"
        else tick_periods(instrument, start, end, cache_root)
    )
    print(f"{instrument.symbol}: {source_label} {args.timeframe}, {len(periods)} source periods")
    results = download_periods(periods, args)
    if args.timeframe == "TICK":
        ticks, results = read_ticks(results, instrument, start, end)
        record_results(paths.status_path, results)
        quality = {**validate_ticks(results, len(ticks)), **provenance("tick", None, None)}
        return publish_ticks(args, instrument, paths, start, end, ticks, quality)
    if source_kind == "native_candle":
        candles, results = read_native_candles(results, instrument, start, end)
    else:
        candles, results = read_tick_candles(results, instrument, args.timeframe, offer_side, volume, start, end)
    record_results(paths.status_path, results)
    quality = {**validate_candles(candles, results, instrument, args.timeframe, start, end), **provenance(source_kind, offer_side, volume)}
    return publish_candles(args, instrument, paths, start, end, source_kind, offer_side, volume, candles, quality)


def download_periods(periods: list[SourcePeriod], args: argparse.Namespace) -> list[FetchResult]:
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_to_cache, period, args.retries, args.timeout, args.refresh): period for period in periods}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
    return sorted(results, key=lambda item: item.period.base_time)


def record_results(status_path: Path, results: list[FetchResult]) -> None:
    for result in results:
        append_status(status_path, result)


def read_ticks(results: list[FetchResult], instrument: Instrument, start, end):
    ticks = []
    updated = []
    for result in results:
        if result.status not in {"cached", "downloaded"}:
            updated.append(result)
            continue
        try:
            parsed = parse_tick_payload(result.period.cache_path.read_bytes(), result.period.base_time, instrument)
            ticks.extend(item for item in parsed if start <= item.time < end)
            updated.append(replace(result, record_count=len(parsed)))
        except Exception as exc:
            updated.append(replace(result, status="parse_error", error=str(exc)))
    return sorted(ticks, key=lambda item: item.time), updated


def read_native_candles(results: list[FetchResult], instrument: Instrument, start, end):
    candles = []
    updated = []
    for result in results:
        if result.status not in {"cached", "downloaded"}:
            updated.append(result)
            continue
        try:
            parsed = parse_native_payload(result.period.cache_path.read_bytes(), result.period.base_time, instrument)
            candles.extend(item for item in parsed if start <= item.time < end)
            updated.append(replace(result, record_count=len(parsed)))
        except Exception as exc:
            updated.append(replace(result, status="parse_error", error=str(exc)))
    return sorted(candles, key=lambda item: item.time), updated


def read_tick_candles(results: list[FetchResult], instrument: Instrument, timeframe: str, price: str, volume: str, start, end):
    aggregator = TickAggregator(timeframe, price, volume, instrument)
    updated = []
    for result in results:
        if result.status not in {"cached", "downloaded"}:
            updated.append(result)
            continue
        try:
            ticks = parse_tick_payload(result.period.cache_path.read_bytes(), result.period.base_time, instrument)
            aggregator.add_ticks([item for item in ticks if start <= item.time < end])
            updated.append(replace(result, record_count=len(ticks)))
        except Exception as exc:
            updated.append(replace(result, status="parse_error", error=str(exc)))
    return aggregator.candles(), updated


def publish_candles(args, instrument, paths, start, end, source_kind, offer_side, volume, candles, quality) -> int:
    trusted = bool(quality["trusted"])
    quality["trusted"] = trusted
    write_json(paths.quality_path, quality)
    if not trusted and not args.allow_partial:
        paths.csv_path.unlink(missing_ok=True)
        paths.manifest_path.unlink(missing_ok=True)
        print(f"{instrument.symbol}: validation failed; no CSV published. Quality: {paths.quality_path}", file=sys.stderr)
        if source_kind == "native_candle":
            print(f"{instrument.symbol}: retry with --source tick and a tick-derived --volume option", file=sys.stderr)
        return 1
    write_candles(paths.csv_path, candles, instrument.precision)
    write_json(paths.manifest_path, manifest(instrument, args.timeframe, source_kind, offer_side, volume, start, end, paths, trusted, len(candles)))
    print(f"{instrument.symbol}: wrote {len(candles)} candles to {paths.csv_path} (trusted={trusted})")
    if source_kind == "native_candle":
        print(f"{instrument.symbol}: Spread and TickVolume are unavailable for native candles and were written as zero.")
        if quality.get("unexpected_source_periods"):
            print(f"{instrument.symbol}: native source was incomplete; retry with --source tick and a tick-derived --volume option.")
    return 0


def publish_ticks(args, instrument, paths, start, end, ticks, quality) -> int:
    trusted = bool(quality["trusted"])
    write_json(paths.quality_path, quality)
    if not trusted and not args.allow_partial:
        paths.csv_path.unlink(missing_ok=True)
        paths.manifest_path.unlink(missing_ok=True)
        print(f"{instrument.symbol}: validation failed; no CSV published. Quality: {paths.quality_path}", file=sys.stderr)
        return 1
    write_ticks(paths.csv_path, ticks, instrument.precision)
    write_json(paths.manifest_path, manifest(instrument, "TICK", "tick", None, None, start, end, paths, trusted, len(ticks)))
    print(f"{instrument.symbol}: wrote {len(ticks)} ticks to {paths.csv_path} (trusted={trusted})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
