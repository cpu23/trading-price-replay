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
import { clampFocusViewport, focusWithinLiveBounds, formatAdaptiveNumber, isTimestampWithinRange } from "./helpers";
import type { ChartHistoryResponse, ReplayState } from "./types";

function chartTime(timestamp: string): UTCTimestamp {
  return Math.floor(new Date(timestamp).getTime() / 1000) as UTCTimestamp;
}

/** Zoom target for a closed trade. `window`, when present, is a bounded
 * historical chart window fetched for a trade that is no longer in the live
 * replay context; the chart renders that window instead of the live payload.
 */
type ChartFocus = { from: string; to: string; window?: ChartHistoryResponse };


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
  const priceLines = useRef<IPriceLine[]>([]);
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
      priceLines.current = [];
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

    // The chart renders either the live replay payload, or — while a
    // historical window is focused — that window's bounded payload. The
    // chart instance is never recreated.
    const windowData = focus?.window;
    const bars = windowData ? windowData.displayed_bars : replay.displayed_bars;
    const fills = windowData ? windowData.fills : replay.fills;
    const trades = windowData ? [windowData.trade] : replay.trades;
    const sma = windowData?.indicators?.sma_close_35 ?? replay.indicators?.sma_close_35 ?? [];

    const logicalRange = instance.timeScale().getVisibleLogicalRange();
    const timeRange = instance.timeScale().getVisibleRange();
    const wasAtLatest = !logicalRange || logicalRange.to >= previousDataLength.current - 1.5;
    const candleData = bars.map((bar) => ({
      time: chartTime(bar.timestamp),
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));
    const firstTimestamp = bars[0]?.timestamp;
    const lastTimestamp = bars[bars.length - 1]?.timestamp;
    candleSeries.setData(candleData);

    // Markers anchor to the source candle, not the execution timestamp: a
    // market entry executes at the source candle's reveal (one minute after
    // that candle opens), while the chart keys candles by their open.
    const markerData: SeriesMarker<Time>[] = [
      ...trades
        .filter((trade) => isTimestampWithinRange(
          trade.entry_source_candle_time ?? trade.entry_time, firstTimestamp, lastTimestamp))
        .map((trade): SeriesMarker<Time> => ({
          time: chartTime(trade.entry_source_candle_time ?? trade.entry_time),
          position: trade.direction === "long" ? "belowBar" : "aboveBar",
          shape: trade.direction === "long" ? "arrowUp" : "arrowDown",
          color: getComputedStyle(document.documentElement).getPropertyValue(
            trade.direction === "long" ? "--positive" : "--negative",
          ).trim(),
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
          color: getComputedStyle(document.documentElement).getPropertyValue(
            fill.pnl >= 0 ? "--positive" : "--negative",
          ).trim(),
          text: fill.reason.toUpperCase(),
        })),
    ].sort((left, right) => Number(left.time) - Number(right.time));
    markers.current?.setMarkers(markerData);

    movingAverage.setData(sma.map((point) => ({ time: chartTime(point.time), value: point.value })));

    for (const line of priceLines.current) candleSeries.removePriceLine(line);
    const styles = getComputedStyle(document.documentElement);
    priceLines.current = trades
      .filter((trade) => trade.status === "open")
      .flatMap((trade) => {
        const lines: IPriceLine[] = [];
        if (trade.stop_price !== null) {
          lines.push(candleSeries.createPriceLine({
            price: trade.stop_price,
            color: styles.getPropertyValue("--negative").trim(),
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: `${trade.direction.toUpperCase()} stop`,
          }));
        }
        if (trade.target_price !== null) {
          lines.push(candleSeries.createPriceLine({
            price: trade.target_price,
            color: styles.getPropertyValue("--positive").trim(),
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: `${trade.direction.toUpperCase()} target`,
          }));
        }
        return lines;
      });

    if (previousDataLength.current === 0) {
      instance.timeScale().fitContent();
    } else if (wasAtLatest) {
      instance.timeScale().scrollToRealTime();
    } else if (timeRange) {
      instance.timeScale().setVisibleRange(timeRange);
    }
    previousDataLength.current = candleData.length;
  }, [replay.displayed_bars, replay.fills, replay.indicators?.sma_close_35, replay.trades, focus]);

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
