import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineSeries,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { clampFocusViewport, classifyChartSeriesUpdate, focusWithinLiveBounds, formatAdaptiveNumber, isTimestampWithinRange, type ChartSeriesBoundary } from "./helpers";
import type { ChartHistoryResponse, ReplayState } from "./types";

function chartTime(timestamp: string): UTCTimestamp {
  return Math.floor(new Date(timestamp).getTime() / 1000) as UTCTimestamp;
}

/** Zoom target for a closed trade. `window`, when present, is a bounded
 * historical chart window fetched for a trade that is no longer in the live
 * replay context; the chart renders that window instead of the live payload.
 */
type ChartFocus = { from: string; to: string; window?: ChartHistoryResponse };

const EMPTY_SERIES_BOUNDARY: ChartSeriesBoundary = {
  length: 0,
  first: null,
  last: null,
  penultimate: null,
};

export function ReplayChart({ replay, precision, focus, onClearFocus }: {
  replay: ReplayState;
  precision: number;
  focus: ChartFocus | null;
  onClearFocus: () => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const smaLine = useRef<ISeriesApi<"Line"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLines = useRef(new Map<string, { signature: string; lines: IPriceLine[] }>());
  const chartColors = useRef({ positive: "", negative: "" });
  const candleState = useRef({
    ...EMPTY_SERIES_BOUNDARY,
    open: null as number | null,
    high: null as number | null,
    low: null as number | null,
    close: null as number | null,
  });
  const smaState = useRef({ ...EMPTY_SERIES_BOUNDARY, value: null as number | null });
  const previousDataLength = useRef(0);
  const wasFocused = useRef(false);
  // The live visible range captured when a focus is applied, restored on
  // clear so a scrolled-back view is not dumped to the latest edge.
  const preFocusRange = useRef<{ from: Time; to: Time } | null>(null);
  const preFocusAtLatest = useRef(true);
  // Fresh replay payload for the focus effect, which must not re-run on
  // every step (re-applying the zoom per step would fight the data effect).
  const replayRef = useRef(replay);
  useEffect(() => {
    replayRef.current = replay;
  }, [replay]);

  useEffect(() => {
    if (!container.current) return;
    const styles = getComputedStyle(document.documentElement);
    const token = (name: string) => styles.getPropertyValue(name).trim();
    chartColors.current = {
      positive: token("--positive"),
      negative: token("--negative"),
    };
    const instance = createChart(container.current, {
      width: container.current.clientWidth,
      height: container.current.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: token("--chart-surface") },
        textColor: token("--text-secondary"),
        fontFamily: token("--font-mono"),
      },
      grid: {
        vertLines: { color: token("--chart-grid") },
        horzLines: { color: token("--chart-grid") },
      },
      rightPriceScale: { borderColor: token("--border") },
      timeScale: {
        borderColor: token("--border"),
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 4,
      },
      crosshair: {
        vertLine: { color: token("--chart-crosshair") },
        horzLine: { color: token("--chart-crosshair") },
      },
    });
    const candleSeries = instance.addSeries(CandlestickSeries, {
      upColor: token("--positive"),
      downColor: token("--negative"),
      wickUpColor: token("--positive"),
      wickDownColor: token("--negative"),
      borderVisible: false,
      priceFormat: { type: "price", precision, minMove: 10 ** -precision },
    });
    const movingAverage = instance.addSeries(LineSeries, {
      color: token("--indicator"),
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
    });

    chart.current = instance;
    candles.current = candleSeries;
    smaLine.current = movingAverage;
    markers.current = createSeriesMarkers(candleSeries, []);

    const observer = new ResizeObserver(() => {
      if (!container.current) return;
      instance.applyOptions({
        width: container.current.clientWidth,
        height: container.current.clientHeight,
      });
    });
    observer.observe(container.current);

    return () => {
      observer.disconnect();
      markers.current?.detach();
      instance.remove();
      chart.current = null;
      candles.current = null;
      smaLine.current = null;
      markers.current = null;
      priceLines.current.clear();
      candleState.current = { ...EMPTY_SERIES_BOUNDARY, open: null, high: null, low: null, close: null };
      smaState.current = { ...EMPTY_SERIES_BOUNDARY, value: null };
      previousDataLength.current = 0;
    };
  }, []);

  useEffect(() => {
    candles.current?.applyOptions({
      priceFormat: { type: "price", precision, minMove: 10 ** -precision },
    });
  }, [precision]);

  useEffect(() => {
    const instance = chart.current;
    const candleSeries = candles.current;
    const movingAverage = smaLine.current;
    if (!instance || !candleSeries || !movingAverage) return;

    const windowData = focus?.window;
    const bars = windowData ? windowData.displayed_bars : replay.displayed_bars;
    const sma = windowData?.indicators?.sma_close_35 ?? replay.indicators?.sma_close_35 ?? [];
    const logicalRange = instance.timeScale().getVisibleLogicalRange();
    const timeRange = instance.timeScale().getVisibleRange();
    const wasAtLatest = !logicalRange || logicalRange.to >= previousDataLength.current - 1.5;

    const lastBar = bars.at(-1);
    const nextCandleBoundary: ChartSeriesBoundary = {
      length: bars.length,
      first: bars.length > 0 ? chartTime(bars[0].timestamp) : null,
      last: lastBar ? chartTime(lastBar.timestamp) : null,
      penultimate: bars.length > 1 ? chartTime(bars[bars.length - 2].timestamp) : null,
    };
    const previousCandle = candleState.current;
    const candleTailChanged = lastBar !== undefined && (
      previousCandle.last !== nextCandleBoundary.last
      || previousCandle.open !== lastBar.open
      || previousCandle.high !== lastBar.high
      || previousCandle.low !== lastBar.low
      || previousCandle.close !== lastBar.close
    );
    const candleMutation = classifyChartSeriesUpdate(
      previousCandle,
      nextCandleBoundary,
      candleTailChanged,
    );
    if (candleMutation === "replace") {
      candleSeries.setData(bars.map((bar) => ({
        time: chartTime(bar.timestamp),
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })));
    } else if (candleMutation === "update" && lastBar) {
      candleSeries.update({
        time: chartTime(lastBar.timestamp),
        open: lastBar.open,
        high: lastBar.high,
        low: lastBar.low,
        close: lastBar.close,
      });
    }
    candleState.current = {
      ...nextCandleBoundary,
      open: lastBar?.open ?? null,
      high: lastBar?.high ?? null,
      low: lastBar?.low ?? null,
      close: lastBar?.close ?? null,
    };

    const lastSma = sma.at(-1);
    const nextSmaBoundary: ChartSeriesBoundary = {
      length: sma.length,
      first: sma.length > 0 ? chartTime(sma[0].time) : null,
      last: lastSma ? chartTime(lastSma.time) : null,
      penultimate: sma.length > 1 ? chartTime(sma[sma.length - 2].time) : null,
    };
    const previousSma = smaState.current;
    const smaMutation = classifyChartSeriesUpdate(
      previousSma,
      nextSmaBoundary,
      lastSma !== undefined && (
        previousSma.last !== nextSmaBoundary.last || previousSma.value !== lastSma.value
      ),
    );
    if (smaMutation === "replace") {
      movingAverage.setData(sma.map((point) => ({
        time: chartTime(point.time),
        value: point.value,
      })));
    } else if (smaMutation === "update" && lastSma) {
      movingAverage.update({ time: chartTime(lastSma.time), value: lastSma.value });
    }
    smaState.current = { ...nextSmaBoundary, value: lastSma?.value ?? null };

    if (candleMutation !== "none") {
      if (previousDataLength.current === 0) {
        instance.timeScale().fitContent();
      } else if (wasAtLatest) {
        instance.timeScale().scrollToRealTime();
      } else if (timeRange) {
        instance.timeScale().setVisibleRange(timeRange);
      }
    }
    previousDataLength.current = bars.length;
  }, [focus, replay.displayed_bars, replay.indicators?.sma_close_35]);

  const visibleFirstTimestamp = focus?.window?.displayed_bars[0]?.timestamp
    ?? replay.displayed_bars[0]?.timestamp;

  useEffect(() => {
    const markerPlugin = markers.current;
    if (!markerPlugin) return;
    const windowData = focus?.window;
    const bars = windowData ? windowData.displayed_bars : replay.displayed_bars;
    const fills = windowData ? windowData.fills : replay.fills;
    const trades = windowData ? [windowData.trade] : replay.trades;
    const firstTimestamp = bars[0]?.timestamp;
    const lastTimestamp = bars.at(-1)?.timestamp;
    const { positive, negative } = chartColors.current;
    const markerData: SeriesMarker<Time>[] = [
      ...trades
        .filter((trade) => isTimestampWithinRange(
          trade.entry_source_candle_time ?? trade.entry_time, firstTimestamp, lastTimestamp))
        .map((trade): SeriesMarker<Time> => ({
          time: chartTime(trade.entry_source_candle_time ?? trade.entry_time),
          position: trade.direction === "long" ? "belowBar" : "aboveBar",
          shape: trade.direction === "long" ? "arrowUp" : "arrowDown",
          color: trade.direction === "long" ? positive : negative,
          text: `${trade.direction === "long" ? "LONG" : "SHORT"} ${formatAdaptiveNumber(trade.initial_quantity)}`,
        })),
      ...fills
        .filter((fill) => fill.reason !== "entry"
          && isTimestampWithinRange(
            fill.source_candle_time ?? fill.timestamp, firstTimestamp, lastTimestamp))
        .map((fill): SeriesMarker<Time> => ({
          time: chartTime(fill.source_candle_time ?? fill.timestamp),
          position: "inBar",
          shape: "circle",
          color: fill.pnl >= 0 ? positive : negative,
          text: fill.reason.toUpperCase(),
        })),
    ].sort((left, right) => Number(left.time) - Number(right.time));
    markerPlugin.setMarkers(markerData);
  }, [focus, replay.fills, replay.trades, replay.visible_timeframe, visibleFirstTimestamp]);

  useEffect(() => {
    const candleSeries = candles.current;
    if (!candleSeries) return;
    const windowData = focus?.window;
    const trades = windowData ? [windowData.trade] : replay.trades;
    const desiredIds = new Set<string>();
    for (const trade of trades) {
      if (trade.status !== "open") continue;
      desiredIds.add(trade.id);
      const signature = `${trade.direction}:${trade.stop_price ?? ""}:${trade.target_price ?? ""}`;
      const installed = priceLines.current.get(trade.id);
      if (installed?.signature === signature) continue;
      if (installed) {
        for (const line of installed.lines) candleSeries.removePriceLine(line);
      }
      const lines: IPriceLine[] = [];
      if (trade.stop_price !== null) {
        lines.push(candleSeries.createPriceLine({
          price: trade.stop_price,
          color: chartColors.current.negative,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: `${trade.direction.toUpperCase()} stop`,
        }));
      }
      if (trade.target_price !== null) {
        lines.push(candleSeries.createPriceLine({
          price: trade.target_price,
          color: chartColors.current.positive,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: `${trade.direction.toUpperCase()} target`,
        }));
      }
      priceLines.current.set(trade.id, { signature, lines });
    }
    for (const [tradeId, installed] of priceLines.current) {
      if (desiredIds.has(tradeId)) continue;
      for (const line of installed.lines) candleSeries.removePriceLine(line);
      priceLines.current.delete(tradeId);
    }
  }, [focus, replay.trades]);

  // Chart focus: zoom to a closed trade's entry-to-exit region without
  // recreating the chart instance. A bounded server window clamps the
  // viewport to the returned bar bounds (the trade's full span may reach
  // past the window). The pre-focus live range is captured on the first
  // focus; clearing restores it when the user had scrolled back, or follows
  // the new latest edge when the view was pinned to the latest bar.
  useEffect(() => {
    const instance = chart.current;
    const candleSeries = candles.current;
    if (!instance || !candleSeries) return;
    if (focus) {
      if (!wasFocused.current) {
        wasFocused.current = true;
        const logicalRange = instance.timeScale().getVisibleLogicalRange();
        preFocusAtLatest.current = !logicalRange
          || logicalRange.to >= previousDataLength.current - 1.5;
        preFocusRange.current = instance.timeScale().getVisibleRange();
      }
      const windowBars = focus.window?.displayed_bars;
      if (windowBars && windowBars.length > 0) {
        const bounds = {
          first: chartTime(windowBars[0].timestamp),
          last: chartTime(windowBars.at(-1)!.timestamp),
        };
        const viewport = clampFocusViewport(
          chartTime(focus.from),
          chartTime(focus.to),
          bounds,
        );
        instance.timeScale().setVisibleRange({
          from: viewport.from as UTCTimestamp,
          to: viewport.to as UTCTimestamp,
        });
        return;
      }
      // Live zoom: only when the trade's span still lies inside the revealed
      // payload. While a window is loading (or failed) for a trade that has
      // slid out, keep the live view instead of zooming into empty space.
      const liveBars = replayRef.current.displayed_bars;
      if (!focusWithinLiveBounds(
        focus.from,
        focus.to,
        liveBars[0]?.timestamp,
        liveBars.at(-1)?.timestamp,
      )) return;
      const viewport = clampFocusViewport(
        chartTime(focus.from),
        chartTime(focus.to),
        null,
      );
      instance.timeScale().setVisibleRange({
        from: viewport.from as UTCTimestamp,
        to: viewport.to as UTCTimestamp,
      });
      return;
    }
    if (!wasFocused.current) return;
    wasFocused.current = false;
    const captured = preFocusRange.current;
    if (captured && preFocusAtLatest.current) {
      // The view was pinned to the latest edge; follow whatever the replay
      // advanced to while the focus was active.
      instance.timeScale().scrollToRealTime();
    } else if (captured) {
      instance.timeScale().setVisibleRange(captured);
    } else {
      const data = candleSeries.data();
      if (data.length >= 10) {
        instance.timeScale().setVisibleLogicalRange({ from: data.length - 60, to: data.length + 5 });
      } else {
        instance.timeScale().fitContent();
      }
    }
    preFocusRange.current = null;
  }, [focus]);

  return (
    <div className="chart-wrap">
      <div
        className="chart"
        ref={container}
        role="img"
        aria-label={`${replay.symbol} ${replay.visible_timeframe} candlestick chart with trade markers`}
      />
      {focus && (
        <button
          className="chart-focus-reset"
          type="button"
          onClick={onClearFocus}
          aria-label="Return chart to the latest replay area"
        >
          Back to latest
        </button>
      )}
    </div>
  );
}
