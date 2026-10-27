import { useEffect, useRef } from 'react'
import { CandlestickSeries, LineSeries, ColorType, LineStyle, createChart, createSeriesMarkers } from 'lightweight-charts'

const EMA = [['fast', '#38bdf8', 'EMA20'], ['slow', '#c084fc', 'EMA50'], ['long', '#fbbf24', 'EMA200']]

export default function TrendChart({ snapshot }) {
  const host = useRef(null)
  const refs = useRef(null)

  useEffect(() => {
    const chart = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#080f1e' }, textColor: '#8092ad', fontSize: 11 },
      grid: { vertLines: { color: '#ffffff05' }, horzLines: { color: '#ffffff08' } },
      rightPriceScale: { borderColor: '#ffffff0c' },
      timeScale: { borderColor: '#ffffff0c', timeVisible: true, rightOffset: 4 },
      localization: { locale: 'en-US' },
    })
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: '#16c79a', downColor: '#f26a78', wickUpColor: '#16c79a', wickDownColor: '#f26a78', borderVisible: false,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })
    const lines = EMA.map(([, color, title]) => chart.addSeries(LineSeries, {
      color, title, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
    }))
    refs.current = { chart, candles, lines, markers: createSeriesMarkers(candles, []), priceLines: [], ready: false }
    return () => { refs.current = null; chart.remove() }
  }, [])

  useEffect(() => {
    const r = refs.current
    if (!r || !snapshot?.candles?.length) return
    const visible = r.chart.timeScale().getVisibleLogicalRange()
    r.candles.setData(snapshot.candles)
    const first = snapshot.candles[0].time
    EMA.forEach(([key], i) => r.lines[i].setData((snapshot.signal_history || [])
      .filter((signal) => signal.closed_at >= first)
      .map((signal) => ({ time: signal.closed_at, value: signal[key] }))))
    r.priceLines.forEach((line) => r.candles.removePriceLine(line))
    r.priceLines = []
    for (const [value, title, color] of [
      [snapshot.position?.entry, 'ENTRY', '#38bdf8'],
      [snapshot.position?.stop, 'TRAILING STOP', '#f26a78'],
    ]) {
      if (value != null && Number.isFinite(Number(value))) r.priceLines.push(r.candles.createPriceLine({
        price: Number(value), title, color, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true,
      }))
    }
    const markers = (snapshot.events || []).filter((e) => e.kind === 'fill' && e.t >= first - 14400)
      .map((fill) => ({
        time: Math.ceil(fill.t / 14400) * 14400,
        position: fill.side === 'buy' ? 'belowBar' : 'aboveBar',
        color: fill.side === 'buy' ? '#16c79a' : '#f26a78',
        shape: fill.side === 'buy' ? 'arrowUp' : 'arrowDown', text: fill.side.toUpperCase(),
      })).filter((m) => snapshot.candles.some((c) => c.time === m.time)).sort((a, b) => a.time - b.time)
    r.markers.setMarkers(markers)
    if (!r.ready) { r.chart.timeScale().fitContent(); r.ready = true }
    else if (visible) r.chart.timeScale().setVisibleLogicalRange(visible)
  }, [snapshot])

  return <div className="trend-chart-host" ref={host} aria-label="SOL four-hour candles and trend indicators" />
}
