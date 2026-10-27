import { useEffect, useRef, useState } from 'react'
import { CandlestickSeries, ColorType, createChart, LineStyle, TickMarkType } from 'lightweight-charts'
import { spotFeedFor } from './tradingEnvironment.js'
import { connectBinanceStream } from './binanceStream.js'
import { isFreshKline } from './marketFreshness.js'
import { formatChartTime, formatTime } from './timeFormat.js'

const TIMEFRAMES = ['1m', '5m', '15m', '1h']

function candleFromValues(time, open, high, low, close) {
  const candle = {
    time: Math.floor(Number(time) / 1000),
    open: Number(open), high: Number(high), low: Number(low), close: Number(close),
  }
  return Number.isSafeInteger(candle.time)
    && [candle.open, candle.high, candle.low, candle.close].every((value) => Number.isFinite(value) && value > 0)
    && candle.high >= Math.max(candle.open, candle.close)
    && candle.low <= Math.min(candle.open, candle.close)
    ? candle : null
}

export default function GridTerminalChart({ environment, gridLevels = [] }) {
  const containerRef = useRef(null)
  const seriesRef = useRef(null)
  const priceLinesRef = useRef([])
  const [timeframe, setTimeframe] = useState('1m')
  const [historyError, setHistoryError] = useState('')
  const [connection, setConnection] = useState(() => spotFeedFor(environment) ? 'Connecting' : 'Environment unavailable')
  const [stale, setStale] = useState(false)

  useEffect(() => {
    const feed = spotFeedFor(environment)
    if (!feed) return undefined
    const container = containerRef.current
    if (!container) return undefined

    setConnection('Connecting')
    setHistoryError('')
    let active = true
    let historyReady = false
    let lastCandleTime = 0
    const pendingCandles = new Map()
    const request = new AbortController()
    const historyTimeout = window.setTimeout(() => request.abort(), 10_000)

    const chart = createChart(container, {
      autoSize: true,
      width: container.clientWidth,
      height: container.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: '#020617' },
        textColor: '#94a3b8',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: 12,
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.035)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.035)' },
      },
      rightPriceScale: { borderColor: 'rgba(255, 255, 255, 0.05)' },
      localization: { locale: 'en-US', timeFormatter: formatChartTime },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.05)', timeVisible: true, secondsVisible: false, rightOffset: 4,
        tickMarkFormatter: (time, type) => typeof time === 'number'
          && (type === TickMarkType.Time || type === TickMarkType.TimeWithSeconds)
          ? formatTime(time * 1000, { seconds: type === TickMarkType.TimeWithSeconds })
          : null,
      },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#10b981', downColor: '#ef4444', borderVisible: false,
      wickUpColor: '#10b981', wickDownColor: '#ef4444',
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })
    seriesRef.current = series

    function applyCandle(candle) {
      if (candle.time < lastCandleTime) return false
      series.update(candle)
      lastCandleTime = candle.time
      return true
    }

    const disconnect = connectBinanceStream(`${feed.websocket}/btcusdt@kline_${timeframe}`, {
      onMessage: (event) => {
        if (!active) return
        let payload
        try {
          payload = JSON.parse(event.data)
        } catch {
          return
        }
        if (payload?.e !== 'kline' || payload?.s !== 'BTCUSDT' || payload?.k?.i !== timeframe) return
        if (!isFreshKline(payload, timeframe)) return false
        const kline = payload.k
        const candle = candleFromValues(kline.t, kline.o, kline.h, kline.l, kline.c)
        if (!candle) return
        if (historyReady) return applyCandle(candle)
        else pendingCandles.set(candle.time, candle)
        return true
      },
      onStateChange: (state) => {
        if (!active) return
        setConnection(state)
        setStale(state === 'Reconnecting')
      },
    })

    async function loadHistory() {
      try {
        const response = await fetch(`${feed.rest}/klines?symbol=BTCUSDT&interval=${timeframe}&limit=240`, { signal: request.signal })
        if (!response.ok) throw new Error(`${environment} candle request failed (${response.status}).`)
        const rows = await response.json()
        if (!Array.isArray(rows)) throw new Error(`${environment} returned invalid candle history.`)
        const candles = [...new Map(rows
          .filter(Array.isArray)
          .map((row) => candleFromValues(row[0], row[1], row[2], row[3], row[4]))
          .filter(Boolean)
          .map((candle) => [candle.time, candle])).values()]
          .sort((left, right) => left.time - right.time)
        if (candles.length === 0) throw new Error(`${environment} returned no candle history.`)
        if (!active) return
        series.setData(candles)
        lastCandleTime = candles[candles.length - 1].time
        chart.timeScale().fitContent()
        setHistoryError('')
      } catch {
        if (active) {
          setHistoryError(`Candle history unavailable. Live ${environment} candles will continue when connected.`)
        }
      } finally {
        window.clearTimeout(historyTimeout)
        if (active) {
          historyReady = true
          for (const candle of [...pendingCandles.values()].sort((left, right) => left.time - right.time)) {
            applyCandle(candle)
          }
          pendingCandles.clear()
        }
      }
    }

    loadHistory()
    return () => {
      active = false
      request.abort()
      window.clearTimeout(historyTimeout)
      disconnect()
      series.setData([])
      seriesRef.current = null
      priceLinesRef.current = []
      chart.remove()
    }
  }, [timeframe, environment])

  useEffect(() => {
    const series = seriesRef.current
    if (!series) return
    for (const line of priceLinesRef.current) series.removePriceLine(line)
    priceLinesRef.current = gridLevels
      .filter((level) => Number.isFinite(Number(level.price)) && Number(level.price) > 0
        && (level.type === 'BUY' || level.type === 'SELL'))
      .map((level) => series.createPriceLine({
        price: Number(level.price),
        color: level.type === 'BUY' ? '#10b981' : '#ef4444',
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: level.type,
      }))
  }, [gridLevels, timeframe, environment])

  return (
    <section className="flex h-full min-h-0 w-full flex-col bg-slate-950 p-3" aria-label={`BTC/USDT ${environment || 'unknown environment'} candlestick chart`}>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex items-center gap-3">
          <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Price &amp; Grid</h3>
          <span className="font-mono text-[9px] text-slate-400">BTC/USDT · {environment || 'UNKNOWN'}</span>
        </div>
        <div className="flex items-center gap-3">
          <div role="group" aria-label="Chart timeframe" aria-describedby="chart-timeframe-disclaimer" className="flex gap-0.5 rounded bg-slate-900 p-0.5">
            {TIMEFRAMES.map((option) => (
              <button key={option} type="button" onClick={() => setTimeframe(option)} aria-pressed={timeframe === option}
                className={`rounded px-2 py-1 font-mono text-[10px] tabular-nums transition-colors focus-visible:outline focus-visible:outline-1 focus-visible:outline-slate-400 ${timeframe === option ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:bg-white/5 hover:text-slate-300'}`}>
                {option}
              </button>
            ))}
          </div>
          <span className={`text-[9px] font-semibold uppercase tracking-wider ${connection === 'Live' ? 'text-emerald-400' : 'text-slate-400'}`}>{connection}</span>
        </div>
      </div>
      <p id="chart-timeframe-disclaimer" className="mb-2 text-[9px] leading-4 text-slate-400">Timeframe is for visualization only. Trading strategy uses fixed 1h ATR.</p>
      {historyError && <p className="mb-2 text-[11px] text-red-400" role="status">{historyError}</p>}
      <div className="relative min-h-0 min-w-0 w-full flex-1">
        <div ref={containerRef} className="absolute inset-0 bg-slate-950" />
        {stale && <p className="absolute bottom-3 right-3 z-10 rounded border border-amber-500/30 bg-slate-950/95 px-3 py-2 text-[10px] text-amber-400" role="status" aria-live="polite">Stale Data / Reconnecting...</p>}
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[9px] text-slate-400">
        <span>Dashed lines: <span className="text-emerald-400">BUY</span> / <span className="text-red-400">SELL</span> open bot limits</span>
        <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer" className="hover:text-slate-300">Charting by TradingView</a>
      </div>
    </section>
  )
}
