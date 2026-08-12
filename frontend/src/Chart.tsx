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
import { formatAdaptiveNumber, isTimestampWithinRange } from "./helpers";
import type { ReplayState } from "./types";

function chartTime(timestamp: string): UTCTimestamp {
  return Math.floor(new Date(timestamp).getTime() / 1000) as UTCTimestamp;
}

export function ReplayChart({ replay, precision }: { replay: ReplayState; precision: number }) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const smaLine = useRef<ISeriesApi<"Line"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const previousDataLength = useRef(0);

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

    const logicalRange = instance.timeScale().getVisibleLogicalRange();
    const timeRange = instance.timeScale().getVisibleRange();
    const wasAtLatest = !logicalRange || logicalRange.to >= previousDataLength.current - 1.5;
    const candleData = replay.displayed_bars.map((bar) => ({
      time: chartTime(bar.timestamp),
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));
    const firstTimestamp = replay.displayed_bars[0]?.timestamp;
    const lastTimestamp = replay.displayed_bars[replay.displayed_bars.length - 1]?.timestamp;
    candleSeries.setData(candleData);

    const markerData: SeriesMarker<Time>[] = [
      ...replay.trades
        .filter((trade) => isTimestampWithinRange(trade.entry_time, firstTimestamp, lastTimestamp))
        .map((trade): SeriesMarker<Time> => ({
          time: chartTime(trade.entry_time),
          position: trade.direction === "long" ? "belowBar" : "aboveBar",
          shape: trade.direction === "long" ? "arrowUp" : "arrowDown",
          color: getComputedStyle(document.documentElement).getPropertyValue(
            trade.direction === "long" ? "--positive" : "--negative",
          ).trim(),
          text: `${trade.direction === "long" ? "LONG" : "SHORT"} ${formatAdaptiveNumber(trade.initial_quantity)}`,
        })),
      ...replay.fills
        .filter((fill) => fill.reason !== "entry"
          && isTimestampWithinRange(fill.timestamp, firstTimestamp, lastTimestamp))
        .map((fill): SeriesMarker<Time> => ({
          time: chartTime(fill.timestamp),
          position: "inBar",
          shape: "circle",
          color: getComputedStyle(document.documentElement).getPropertyValue(
            fill.pnl >= 0 ? "--positive" : "--negative",
          ).trim(),
          text: fill.reason.toUpperCase(),
        })),
    ].sort((left, right) => Number(left.time) - Number(right.time));
    markers.current?.setMarkers(markerData);

    const sma = replay.indicators.sma_close_35 ?? [];
    movingAverage.setData(sma.map((point) => ({ time: chartTime(point.time), value: point.value })));

    for (const line of priceLines.current) candleSeries.removePriceLine(line);
    const styles = getComputedStyle(document.documentElement);
    priceLines.current = replay.trades
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
  }, [replay.displayed_bars, replay.fills, replay.indicators.sma_close_35, replay.trades]);

  return (
    <div
      className="chart"
      ref={container}
      role="img"
      aria-label={`${replay.symbol} ${replay.visible_timeframe} candlestick chart with trade markers`}
    />
  );
}
