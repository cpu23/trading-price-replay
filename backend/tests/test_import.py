from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from app import config, market_data, repository

FIXTURE = Path(__file__).parent / "fixtures" / "dukascopy_1m.csv"
HEADER = "Date,Time,Open,High,Low,Close,TickVolume,Volume,Spread\n"
VALID_ROW = "2026.01.02,17:07:00,1.1025,1.1029,1.1022,1.1027,22,0,0\n"
INVALID_ROW = "2026.01.02,17:07:00,1.1025,1.1020,1.1027,1.1026,22,0,0\n"  # high < low


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(config, "OHLCV_ROOT", tmp_path / "ohlcv")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sessions" / "db.sqlite3")
    monkeypatch.setattr(market_data, "RAW_ROOT", tmp_path / "raw")
    monkeypatch.setattr(market_data, "OHLCV_ROOT", tmp_path / "ohlcv")
    monkeypatch.setattr(repository, "DB_PATH", tmp_path / "sessions" / "db.sqlite3")
    repository.initialize()
    market_data.invalidate_bars()
    yield tmp_path
    market_data.invalidate_bars()


def write_csv(directory, name, rows, header=HEADER):
    source = directory / name
    source.write_text(header + "".join(rows))
    return source


def test_known_schema_imports_to_partitioned_parquet(isolated):
    result = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 100000, "utc_aligned")
    assert result["rows_imported"] == 12
    assert (isolated / "ohlcv" / "EURUSD" / "versions" / result["id"] / "1m" / "year=2026" / "data.parquet").exists()
    assert len(market_data.load_bars("EURUSD")) == 12
    assert repository.get_symbol("EURUSD")["data_version"] == result["id"]


def test_empty_and_header_only_files_are_rejected(isolated):
    empty = isolated / "empty.csv"
    empty.write_text("")
    with pytest.raises(ValueError, match="empty"):
        market_data.import_file(str(empty), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    header_only = isolated / "header.csv"
    header_only.write_text(HEADER)
    with pytest.raises(ValueError, match="no data rows"):
        market_data.import_file(str(header_only), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")


def test_non_finite_and_invalid_ohlc_rows_are_rejected(isolated):
    nan_row = "2026.01.02,17:07:00,nan,1.1029,1.1022,1.1027,22,0,0\n"
    nan_file = write_csv(isolated, "nan.csv", [nan_row])
    with pytest.raises(ValueError, match="invalid_numeric"):
        market_data.import_file(str(nan_file), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    bad_ohlc = write_csv(isolated, "bad_ohlc.csv", [INVALID_ROW])
    with pytest.raises(ValueError, match="invalid_ohlc"):
        market_data.import_file(str(bad_ohlc), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")


def test_subminute_timestamps_are_rejected(isolated):
    subminute_row = "2026.01.02,17:07:30,1.1025,1.1029,1.1022,1.1027,22,0,0\n"
    bad = write_csv(isolated, "subminute.csv", [VALID_ROW, subminute_row])
    with pytest.raises(ValueError, match="subminute"):
        market_data.import_file(str(bad), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    # A collision at minute granularity is a duplicate even with different seconds.
    dup = write_csv(isolated, "dup.csv", [VALID_ROW, "2026.01.02,17:07:05,1.1025,1.1029,1.1022,1.1027,22,0,0\n"])
    with pytest.raises(ValueError, match="subminute"):
        market_data.import_file(str(dup), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    exact = write_csv(isolated, "exact.csv", [VALID_ROW, VALID_ROW])
    with pytest.raises(ValueError, match="duplicates"):
        market_data.import_file(str(exact), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")


def test_non_finite_or_null_normalized_volume_is_rejected(isolated):
    inf_row = "2026.01.02,17:07:00,1.1025,1.1029,1.1022,1.1027,1e309,0,0\n"
    inf_file = write_csv(isolated, "inf_vol.csv", [inf_row])
    with pytest.raises(ValueError, match="invalid_volume"):
        market_data.import_file(str(inf_file), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    null_row = "2026.01.02,17:07:00,1.1025,1.1029,1.1022,1.1027,22,,0\n"
    null_file = write_csv(isolated, "null_vol.csv", [null_row])
    with pytest.raises(ValueError, match="invalid_volume"):
        market_data.import_file(str(null_file), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")


def test_negative_raw_volume_is_rejected(isolated):
    # A negative Volume must fail even when a positive TickVolume could back it up.
    row = "2026.01.02,17:07:00,1.1025,1.1029,1.1022,1.1027,22,-5,0\n"
    bad = write_csv(isolated, "neg_volume.csv", [row])
    with pytest.raises(ValueError, match="invalid_volume"):
        market_data.import_file(str(bad), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")


def test_negative_raw_tick_volume_is_rejected(isolated):
    # A negative TickVolume must fail instead of being silently zeroed.
    row = "2026.01.02,17:07:00,1.1025,1.1029,1.1022,1.1027,-5,0,0\n"
    bad = write_csv(isolated, "neg_tick.csv", [row])
    with pytest.raises(ValueError, match="invalid_volume"):
        market_data.import_file(str(bad), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")


def test_negative_both_volume_fields_are_rejected(isolated):
    row = "2026.01.02,17:07:00,1.1025,1.1029,1.1022,1.1027,-5,-5,0\n"
    bad = write_csv(isolated, "neg_both.csv", [row])
    with pytest.raises(ValueError, match="invalid_volume"):
        market_data.import_file(str(bad), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")


def test_zero_and_positive_volume_fallback_remains_valid(isolated):
    # Positive Volume wins over TickVolume; zero Volume falls back to a positive
    # TickVolume; zero/zero stays a valid zero-volume bar.
    rows = [
        "2026.01.02,17:07:00,1.1025,1.1029,1.1022,1.1027,5,100,0\n",
        "2026.01.02,17:08:00,1.1030,1.1034,1.1027,1.1032,22,0,0\n",
        "2026.01.02,17:09:00,1.1032,1.1036,1.1029,1.1034,0,0,0\n",
    ]
    good = write_csv(isolated, "fallback.csv", rows)
    result = market_data.import_file(str(good), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    assert result["rows_imported"] == 3
    assert [bar.volume for bar in market_data.load_bars("EURUSD")] == [100.0, 22.0, 0.0]


def test_malformed_metadata_is_rejected(isolated):
    with pytest.raises(ValueError, match="contract multiplier"):
        market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 0, "utc_aligned")
    with pytest.raises(ValueError, match="price precision"):
        market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", -1, 1, "utc_aligned")
    with pytest.raises(ValueError, match="asset class"):
        market_data.import_file(str(FIXTURE), "EURUSD", "", "USD", 5, 1, "utc_aligned")
    with pytest.raises(ValueError, match="profile"):
        market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "custom_session_anchor")
    with pytest.raises(ValueError, match="symbol"):
        market_data.import_file(str(FIXTURE), "EUR USD!", "forex", "USD", 5, 1, "utc_aligned")


def test_wrong_schema_is_rejected(isolated):
    wrong = write_csv(isolated, "wrong.csv", ["1.10,1.11,1.09,1.10\n"], header="A,B,C,D\n")
    with pytest.raises(ValueError, match="unsupported schema"):
        market_data.import_file(str(wrong), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")


def test_symbol_is_normalized_to_uppercase(isolated):
    market_data.import_file(str(FIXTURE), " eurusd ", "forex", "USD", 5, 1, "utc_aligned")
    assert len(market_data.load_bars("EURUSD")) == 12
    assert market_data.load_bars("eurusd") == []  # stored name is the normalized one
    metadata = repository.get_symbol("EURUSD")
    assert metadata is not None
    assert metadata["symbol"] == "EURUSD"


def test_failed_validation_preserves_published_dataset(isolated):
    first = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 100000, "utc_aligned")
    before = market_data.load_bars("EURUSD")
    bad_file = write_csv(isolated, "bad.csv", [VALID_ROW, INVALID_ROW])
    with pytest.raises(ValueError, match="invalid_ohlc"):
        market_data.import_file(str(bad_file), "EURUSD", "forex", "USD", 5, 100000, "utc_aligned")
    assert market_data.load_bars("EURUSD") == before
    assert repository.get_symbol("EURUSD")["first_timestamp"] == before[0].timestamp.isoformat()
    assert repository.get_symbol("EURUSD")["data_version"] == first["id"]


def test_reimport_publishes_new_version_and_retains_old(isolated):
    first = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    start = datetime(2026, 1, 2, 16, 55, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, 17, 7, tzinfo=timezone.utc)
    assert len(market_data.load_bars_range("EURUSD", start, end, first["id"])) == 12
    # Re-import a replacement dataset: the published 12 bars plus one new minute.
    extended = isolated / "extended.csv"
    extended.write_text(FIXTURE.read_text() + VALID_ROW)
    second = market_data.import_file(str(extended), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    assert second["id"] != first["id"]
    # The pointer moved, the old version stayed readable, and the new version is served.
    assert repository.get_symbol("EURUSD")["data_version"] == second["id"]
    assert len(market_data.load_bars_range("EURUSD", start, end, second["id"])) == 13
    assert len(market_data.load_bars_range("EURUSD", start, end, first["id"])) == 12
    assert len(market_data.load_bars("EURUSD")) == 13  # current published version


def test_load_bars_range_is_bounded_and_inclusive(isolated):
    result = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    start = datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, 17, 2, tzinfo=timezone.utc)
    bars = market_data.load_bars_range("EURUSD", start, end, result["id"])
    assert [bar.timestamp.minute for bar in bars] == [0, 1, 2]
    assert market_data.load_bars_range("EURUSD", start, end, result["id"]) is bars  # cached, no re-read


def test_legacy_dataset_stays_readable_after_versioned_reimport(isolated):
    # Simulate a pre-version publish: bars under the legacy path, no pointer set.
    legacy_dir = isolated / "ohlcv" / "EURUSD" / "1m" / "year=2026"
    legacy_dir.mkdir(parents=True)
    pl.DataFrame({
        "timestamp_utc": [datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)],
        "open": [1.10], "high": [1.11], "low": [1.09], "close": [1.105], "volume": [10.0],
    }).write_parquet(legacy_dir / "data.parquet")
    start = end = datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)
    assert len(market_data.load_bars_range("EURUSD", start, end, None)) == 1
    # A versioned re-import moves the pointer but never touches the legacy path,
    # so pre-version sessions keep replaying the retained dataset.
    result = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    assert repository.get_symbol("EURUSD")["data_version"] == result["id"]
    assert len(market_data.load_bars_range("EURUSD", start, end, None)) == 1
    assert len(market_data.load_bars_range("EURUSD", start, end, result["id"])) == 1
    assert (isolated / "ohlcv" / "EURUSD" / "1m" / "year=2026" / "data.parquet").exists()


def _csv_row(timestamp: datetime, price: float = 1.10) -> str:
    return (f"{timestamp:%Y.%m.%d},{timestamp:%H:%M:%S},{price:.4f},{price + 0.0002:.4f},"
            f"{price - 0.0002:.4f},{price:.4f},10,0,0\n")


def test_load_bars_before_reads_only_newest_partitions(isolated, monkeypatch):
    # 300 bars in 2025 and 300 in 2026: a limit of 300 is satisfied entirely by
    # the newest year partition, which must be the only one read.
    rows = [_csv_row(datetime(2025, 12, 31, hour, minute, tzinfo=timezone.utc))
            for hour in range(5) for minute in range(60)]
    rows += [_csv_row(datetime(2026, 1, 2, hour, minute, tzinfo=timezone.utc))
             for hour in range(5) for minute in range(60)]
    version = market_data.import_file(str(write_csv(isolated, "two_years.csv", rows)),
                                      "EURUSD", "forex", "USD", 5, 1, "utc_aligned")["id"]
    before = datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    scanned: list[str] = []
    real_scan = pl.scan_parquet

    def spy(path, *args, **kwargs):
        scanned.append(str(path))
        return real_scan(path, *args, **kwargs)

    monkeypatch.setattr(market_data.pl, "scan_parquet", spy)
    bars = market_data.load_bars_before("EURUSD", before, 300, version)
    assert len(bars) == 300
    assert bars[0].timestamp == datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)
    assert bars[-1].timestamp == datetime(2026, 1, 2, 4, 59, tzinfo=timezone.utc)
    assert all("year=2025" not in path for path in scanned)  # older partition never read


def test_load_bars_before_returns_last_n_across_gap_and_excludes_future(isolated):
    # Friday 2026-01-02 00:00-08:19 (500 bars) then Monday 2026-01-05 00:00-00:29 (30 bars).
    rows = [_csv_row(datetime(2026, 1, 2, hour, minute, tzinfo=timezone.utc))
            for hour in range(9) for minute in range(60)][:500]
    rows += [_csv_row(datetime(2026, 1, 5, 0, minute, tzinfo=timezone.utc)) for minute in range(30)]
    version = market_data.import_file(str(write_csv(isolated, "weekend.csv", rows)),
                                      "EURUSD", "forex", "USD", 5, 1, "utc_aligned")["id"]
    monday = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    bars = market_data.load_bars_before("EURUSD", monday, 500, version)
    assert len(bars) == 500
    assert bars[0].timestamp == datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)
    assert bars[-1].timestamp == datetime(2026, 1, 2, 8, 19, tzinfo=timezone.utc)
    assert all(bar.timestamp < monday for bar in bars)  # no session/future bars
    assert market_data.load_bars_before("EURUSD", monday, 500, version) is bars  # cached, no re-read
    assert market_data.load_bars_before("EURUSD", monday, 0, version) == []
    # A limit beyond available history returns everything that exists before `before`.
    tuesday = datetime(2026, 1, 6, 0, 0, tzinfo=timezone.utc)
    assert len(market_data.load_bars_before("EURUSD", tuesday, 2000, version)) == 530


def _spy_frame_conversions(monkeypatch) -> list[int]:
    """Count rows handed to `_frame_to_bars` per call, in call order."""
    seen: list[int] = []
    real = market_data._frame_to_bars

    def spy(frame):
        seen.append(frame.height)
        return real(frame)

    monkeypatch.setattr(market_data, "_frame_to_bars", spy)
    return seen


def test_load_bars_before_materializes_at_most_limit_bars_per_partition(isolated, monkeypatch):
    # 2025: 100 rows, 2026: 300 rows. A limit of 200 must be satisfied by the
    # newest partition's tail alone, converting at most 200 rows in one call.
    rows = [_csv_row(datetime(2025, 12, 31, hour, minute, tzinfo=timezone.utc))
            for hour in range(2) for minute in range(50)]
    rows += [_csv_row(datetime(2026, 1, 2, hour, minute, tzinfo=timezone.utc))
             for hour in range(5) for minute in range(60)]
    version = market_data.import_file(str(write_csv(isolated, "two_years.csv", rows)),
                                      "EURUSD", "forex", "USD", 5, 1, "utc_aligned")["id"]
    before = datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    seen = _spy_frame_conversions(monkeypatch)
    bars = market_data.load_bars_before("EURUSD", before, 200, version)
    assert len(bars) == 200
    assert seen == [200]  # one partition, exactly the limit, never the full 300
    assert bars[0].timestamp == datetime(2026, 1, 2, 1, 40, tzinfo=timezone.utc)
    assert bars[-1].timestamp == datetime(2026, 1, 2, 4, 59, tzinfo=timezone.utc)


def test_load_bars_before_tail_spills_across_years_chronologically(isolated, monkeypatch):
    # 2025: 100 rows, 2026: 60 rows. A limit of 80 takes the whole 2026 partition
    # and only the remaining 20 rows from the 2025 tail, never a full partition.
    rows = [_csv_row(datetime(2025, 12, 31, hour, minute, tzinfo=timezone.utc))
            for hour in range(2) for minute in range(50)]
    rows += [_csv_row(datetime(2026, 1, 2, hour, minute, tzinfo=timezone.utc))
             for hour in range(1) for minute in range(60)]
    version = market_data.import_file(str(write_csv(isolated, "two_years.csv", rows)),
                                      "EURUSD", "forex", "USD", 5, 1, "utc_aligned")["id"]
    before = datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    seen = _spy_frame_conversions(monkeypatch)
    bars = market_data.load_bars_before("EURUSD", before, 80, version)
    assert len(bars) == 80
    assert seen == [60, 20]  # newest partition first; the older tail is bounded too
    assert all(rows_seen <= 80 for rows_seen in seen)  # never more than the limit per partition
    # Chronological order across the year gap (2025-12-31 -> 2026-01-02).
    assert bars[0].timestamp == datetime(2025, 12, 31, 1, 30, tzinfo=timezone.utc)
    assert bars[-1].timestamp == datetime(2026, 1, 2, 0, 59, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Immutable-version signature cache: repeated reads of a pinned version must not
# rediscover partitions (glob + stat), while legacy data, missing versions, and
# invalidated symbols must always be rescanned.
# ---------------------------------------------------------------------------

def _counting_scan(monkeypatch) -> dict[str, int]:
    """Count real partition discovery passes (`_scan_partitions`) through the cache layer."""
    scans = {"n": 0}
    real_scan = market_data._scan_partitions

    def spy(symbol, version):
        scans["n"] += 1
        return real_scan(symbol, version)

    monkeypatch.setattr(market_data, "_scan_partitions", spy)
    return scans


def test_pinned_version_signature_is_cached_until_invalidated(isolated, monkeypatch):
    result = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    version = result["id"]
    scans = _counting_scan(monkeypatch)
    first = market_data.bars_signature("EURUSD", version)
    assert len(first) == 1  # the fixture spans a single year partition
    assert scans["n"] == 1
    assert market_data.bars_signature("EURUSD", version) == first
    assert scans["n"] == 1  # second call is served from the cache: no rediscovery


def test_repeated_pinned_version_reads_perform_no_rescans(isolated, monkeypatch):
    result = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    version = result["id"]
    start = datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, 17, 2, tzinfo=timezone.utc)
    scans = _counting_scan(monkeypatch)
    bars = market_data.load_bars_range("EURUSD", start, end, version)
    assert len(bars) == 3
    assert scans["n"] == 1  # one discovery pass for the cold read
    assert market_data.load_bars_range("EURUSD", start, end, version) is bars
    assert scans["n"] == 1  # warm bars cache hit: still no rediscovery
    before = datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    assert len(market_data.load_bars_before("EURUSD", before, 5, version)) == 5
    assert scans["n"] == 1  # a different read reuses the cached signature, no rescan


def test_invalidate_bars_symbol_only_clears_that_symbol(isolated, monkeypatch):
    market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    market_data.import_file(str(FIXTURE), "GBPUSD", "forex", "USD", 5, 1, "utc_aligned")
    eur_version = repository.get_symbol("EURUSD")["data_version"]
    gbp_version = repository.get_symbol("GBPUSD")["data_version"]
    scans = _counting_scan(monkeypatch)
    market_data.bars_signature("EURUSD", eur_version)
    market_data.bars_signature("GBPUSD", gbp_version)
    assert scans["n"] == 2
    market_data.invalidate_bars("EURUSD")
    assert market_data.bars_signature("EURUSD", eur_version)  # invalidated: rescanned
    assert scans["n"] == 3
    assert market_data.bars_signature("GBPUSD", gbp_version)  # untouched: still cached
    assert scans["n"] == 3


def test_invalidate_bars_none_clears_all_signatures(isolated, monkeypatch):
    result = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    scans = _counting_scan(monkeypatch)
    market_data.bars_signature("EURUSD", result["id"])
    market_data.bars_signature("EURUSD", result["id"])
    assert scans["n"] == 1
    market_data.invalidate_bars()
    market_data.bars_signature("EURUSD", result["id"])
    assert scans["n"] == 2  # global invalidation forces a rescan


def test_legacy_dataset_is_never_cached_and_detects_changes(isolated, monkeypatch):
    legacy_dir = isolated / "ohlcv" / "EURUSD" / "1m" / "year=2026"
    legacy_dir.mkdir(parents=True)

    def write_legacy(row_count):
        pl.DataFrame({
            "timestamp_utc": [datetime(2026, 1, 2, 17, minute, tzinfo=timezone.utc)
                              for minute in range(row_count)],
            "open": [1.10] * row_count, "high": [1.11] * row_count, "low": [1.09] * row_count,
            "close": [1.105] * row_count, "volume": [10.0] * row_count,
        }).write_parquet(legacy_dir / "data.parquet")

    write_legacy(1)
    scans = _counting_scan(monkeypatch)
    start = datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, 17, 1, tzinfo=timezone.utc)
    assert len(market_data.load_bars_range("EURUSD", start, end, None)) == 1
    assert market_data.bars_signature("EURUSD", None)
    assert scans["n"] == 2  # legacy scans are never cached: both calls rediscovered
    write_legacy(2)  # rewrite the legacy parquet with bars at 17:00 and 17:01
    assert len(market_data.load_bars_range("EURUSD", start, end, None)) == 2
    assert scans["n"] == 3  # the fresh scan detected the on-disk change


def test_invalidated_symbol_detects_on_disk_changes(isolated, monkeypatch):
    result = market_data.import_file(str(FIXTURE), "EURUSD", "forex", "USD", 5, 1, "utc_aligned")
    version = result["id"]
    start = datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, 17, 5, tzinfo=timezone.utc)
    assert len(market_data.load_bars_range("EURUSD", start, end, version)) == 6  # fixture: 17:00-17:06
    partition = isolated / "ohlcv" / "EURUSD" / "versions" / version / "1m" / "year=2026" / "data.parquet"
    pl.DataFrame({
        "timestamp_utc": [datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc),
                          datetime(2026, 1, 2, 17, 1, tzinfo=timezone.utc)],
        "open": [1.10, 1.10], "high": [1.11, 1.11], "low": [1.09, 1.09],
        "close": [1.105, 1.105], "volume": [10.0, 10.0],
    }).write_parquet(partition)
    market_data.invalidate_bars("EURUSD")
    assert len(market_data.load_bars_range("EURUSD", start, end, version)) == 2  # rescan saw the change


def test_missing_pinned_version_is_recomputed_until_published(isolated, monkeypatch):
    missing = "not-yet-published"
    scans = _counting_scan(monkeypatch)
    assert market_data.bars_signature("EURUSD", missing) == ()
    assert market_data.bars_signature("EURUSD", missing) == ()
    assert scans["n"] == 2  # an empty signature is never cached: every call rescans
    version_dir = isolated / "ohlcv" / "EURUSD" / "versions" / missing / "1m" / "year=2026"
    version_dir.mkdir(parents=True)
    pl.DataFrame({
        "timestamp_utc": [datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)],
        "open": [1.10], "high": [1.11], "low": [1.09], "close": [1.105], "volume": [10.0],
    }).write_parquet(version_dir / "data.parquet")
    signature = market_data.bars_signature("EURUSD", missing)
    assert len(signature) == 1  # later publication is not hidden
    assert scans["n"] == 3
    assert market_data.bars_signature("EURUSD", missing) == signature
    assert scans["n"] == 3  # the complete version is now cached


def test_signature_cache_is_bounded_lru(isolated, monkeypatch):
    monkeypatch.setattr(market_data, "_SIGNATURE_CACHE_MAX", 2)
    versions = []
    for symbol in ("EURUSD", "GBPUSD", "USDJPY"):
        result = market_data.import_file(str(FIXTURE), symbol, "forex", "USD", 5, 1, "utc_aligned")
        versions.append((symbol, result["id"]))
    for symbol, version in versions:
        market_data.bars_signature(symbol, version)
    assert len(market_data._SIGNATURE_CACHE) == 2  # capped
    first_symbol, first_version = versions[0]
    scans = _counting_scan(monkeypatch)
    assert market_data.bars_signature(first_symbol, first_version)  # evicted: rescanned
    assert scans["n"] == 1
    assert len(market_data._SIGNATURE_CACHE) == 2  # re-inserted without exceeding the cap

