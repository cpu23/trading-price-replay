from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dukascopy_downloader.catalog import get_instrument
from dukascopy_downloader.cli import publish_candles
from dukascopy_downloader.config import output_paths
from dukascopy_downloader.models import Candle, SourcePeriod
from dukascopy_downloader.sources import fetch_to_cache
from dukascopy_downloader.validation import validate_candles


UTC = timezone.utc


def test_strict_failure_publishes_quality_only(tmp_path: Path) -> None:
    paths = output_paths(tmp_path, "run")
    args = argparse.Namespace(allow_partial=False, timeframe="M1")
    candle = Candle(datetime(2024, 1, 2, tzinfo=UTC), 1, 1, 1, 1, 0, 1, 0)
    result = publish_candles(args, get_instrument("EURUSD"), paths, candle.time, candle.time, "native_candle", "bid", "native", [candle], {"trusted": False})
    assert result == 1
    assert paths.quality_path.exists()
    assert not paths.csv_path.exists()
    assert not paths.manifest_path.exists()


def test_allow_partial_marks_outputs_untrusted(tmp_path: Path) -> None:
    paths = output_paths(tmp_path, "run")
    args = argparse.Namespace(allow_partial=True, timeframe="M1")
    candle = Candle(datetime(2024, 1, 2, tzinfo=UTC), 1, 1, 1, 1, 0, 1, 0)
    result = publish_candles(args, get_instrument("EURUSD"), paths, candle.time, candle.time, "native_candle", "bid", "native", [candle], {"trusted": False})
    assert result == 0
    assert '"trusted": false' in paths.manifest_path.read_text()
    assert paths.csv_path.exists()


def test_cache_is_reused_without_network(tmp_path: Path) -> None:
    cache_path = tmp_path / "cached.bi5"
    cache_path.write_bytes(b"cached")
    period = SourcePeriod("key", datetime(2024, 1, 2, tzinfo=UTC), "https://invalid.example", cache_path, True)
    result = fetch_to_cache(period, retries=1, timeout=0.01, refresh=False)
    assert result.status == "cached"
    assert result.byte_count == 6


def test_refresh_replaces_cached_source(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "cached.bi5"
    cache_path.write_bytes(b"old")
    period = SourcePeriod("key", datetime(2024, 1, 2, tzinfo=UTC), "https://example.test", cache_path, True)
    monkeypatch.setattr("dukascopy_downloader.sources.fetch_url", lambda url, request, timeout: b"new")
    result = fetch_to_cache(period, retries=1, timeout=1, refresh=True)
    assert result.status == "downloaded"
    assert cache_path.read_bytes() == b"new"


def test_empty_range_strict_publish_refuses(tmp_path: Path) -> None:
    paths = output_paths(tmp_path, "run")
    args = argparse.Namespace(allow_partial=False, timeframe="M1")
    start = datetime(2024, 1, 2, tzinfo=UTC)
    result = publish_candles(args, get_instrument("EURUSD"), paths, start, start, "native_candle", "bid", "native", [], {"trusted": False})
    assert result == 1
    assert paths.quality_path.exists()
    assert not paths.csv_path.exists()
    assert not paths.manifest_path.exists()


def test_empty_range_allow_partial_publishes_header_only_untrusted(tmp_path: Path) -> None:
    paths = output_paths(tmp_path, "run")
    args = argparse.Namespace(allow_partial=True, timeframe="M1")
    instrument = get_instrument("EURUSD")
    start = datetime(2024, 1, 2, tzinfo=UTC)
    quality = validate_candles([], [], instrument, "M1", start, start)
    assert quality["trusted"] is False
    assert quality["bar_count"] == 0
    result = publish_candles(args, instrument, paths, start, start, "native_candle", "bid", "native", [], quality)
    assert result == 0
    assert paths.csv_path.read_text().splitlines() == ["Date,Time,Open,High,Low,Close,TickVolume,Volume,Spread"]
    manifest = json.loads(paths.manifest_path.read_text())
    assert manifest["trusted"] is False
    assert manifest["record_count"] == 0
    assert manifest["record_count"] == quality["bar_count"]
