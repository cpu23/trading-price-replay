# Dukascopy Downloader

Standalone, audited downloader for curated Dukascopy instruments. It exports raw ticks or UTC-aligned `M1`, `H1`, and `D1` candles and records provenance and data-quality results beside every output.

## Setup

```bash
cd tools/dukascopy_downloader
uv sync --dev
uv run dukascopy-download --list-instruments
```

Inspect catalog metadata:

```bash
uv run dukascopy-download --describe EURUSD
```

Download native M1 bid candles:

```bash
uv run dukascopy-download EURUSD \
  --start 2024-01-01 --end 2024-01-31 --timeframe M1
```

Download tick-derived midpoint H1 candles with combined quote volume:

```bash
uv run dukascopy-download EURUSD GBPUSD \
  --start 2024-01-01 --end 2024-01-31 --timeframe H1 \
  --source tick --price mid --volume total
```

Download raw ticks:

```bash
uv run dukascopy-download EURUSD \
  --start 2024-01-02 --end 2024-01-02 --timeframe TICK
```

## Source Selection

`--source auto` uses native Dukascopy candles when `--price` is `bid` or `ask` and `--volume` is `native`. Midpoint prices and bid, ask, total, or tick-count volume are derived from ticks.

One output always uses one source. Native failures do not silently fall back to ticks.

Raw tick exports reject `--price` and `--volume`; their schema is always:

```text
TimestampUTC,Bid,Ask,BidVolume,AskVolume
```

Raw tick manifests use `offer_side: null` and `volume_semantics: null` because
the export contains both quote sides and both quote-volume fields.

Candle exports use the Price Replay-compatible schema:

```text
Date,Time,Open,High,Low,Close,TickVolume,Volume,Spread
```

For native candles, Dukascopy's volume is labelled `dukascopy_native`; it is not claimed to be trade, bid, ask, or total volume. Native exports write unavailable `TickVolume` and `Spread` values as zero and state that explicitly at completion.

## Trust And Cache

Source `.bi5` files are cached automatically under `data/downloads/dukascopy/cache/`. Use `--refresh` to redownload the requested range.

By default, unexpected missing or invalid source periods prevent CSV publication. A failed strict run retains its cache, source ledger, and quality report.

`--allow-partial` publishes available records, with both manifest and quality report marked `trusted: false`.

The curated catalog records price scaling and expected session closures. Add a symbol only after verifying its binary scaling and running live smoke checks.

For Price Replay, prefer an `M1` candle export; the application derives higher timeframes causally.

## Tests

```bash
uv run pytest
```
