export function number(value) {
  if (value == null || value === '') return null
  const result = Number(value)
  return Number.isFinite(result) ? result : null
}

export function money(value, signed = false) {
  const n = number(value)
  if (n == null) return '—'
  return `${signed && n > 0 ? '+' : ''}${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function freshSnapshot(snapshot, now, failed = false) {
  return Boolean(snapshot?.mode === 'PAPER' && snapshot?.strategy_id === 'sol-ema-20-50-200-atr3-v1'
    && snapshot?.symbol === 'SOL/USDT' && snapshot.market_fresh && !failed
    && now - Number(snapshot.quote?.observed_at) >= -1
    && now - Number(snapshot.quote?.observed_at) <= snapshot.quote_max_age_seconds
    && now - Number(snapshot.updated_at) <= 15)
}

export function signalChecks(signal) {
  if (!signal) return []
  return [
    { label: 'EMA20 above EMA50', passed: signal.fast > signal.slow },
    { label: 'Close above EMA200', passed: signal.close > signal.long },
    { label: 'Fresh bullish crossover', passed: Boolean(signal.ema_entry) },
  ]
}

export function recentFills(events = []) {
  return events.filter((event) => event.kind === 'fill').slice(0, 6).map((fill) => ({
    ...fill,
    profit: events.find((event) => event.kind === 'trade' && event.t === fill.t && event.reason === fill.reason)?.profit ?? null,
  }))
}

export function controlAvailability(snapshot, fresh) {
  return {
    start: Boolean(fresh && snapshot.history_complete && snapshot.signal && !snapshot.error && !snapshot.entries_enabled),
    pause: Boolean(snapshot?.entries_enabled),
    close: Boolean(fresh && number(snapshot?.position?.quantity) > 0),
  }
}
