// Receipt time alone must never certify old/replayed market events as LIVE.
export const MARKET_EVENT_MAX_AGE_MS = 15_000
const FUTURE_TOLERANCE_MS = 5_000
const INTERVAL_MS = { '1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000 }

export function isFreshMarketEvent(timestamp, now = Date.now()) {
  return Number.isSafeInteger(timestamp) && timestamp > 0
    && timestamp >= now - MARKET_EVENT_MAX_AGE_MS
    && timestamp <= now + FUTURE_TOLERANCE_MS
}

export function isFreshKline(payload, timeframe, now = Date.now()) {
  const duration = INTERVAL_MS[timeframe]
  const start = payload?.k?.t
  return Boolean(duration && isFreshMarketEvent(payload?.E, now)
    && Number.isSafeInteger(start) && start > 0 && start % duration === 0
    && start <= payload.E && payload.E < start + duration + MARKET_EVENT_MAX_AGE_MS
    && start <= now + FUTURE_TOLERANCE_MS && now < start + duration + MARKET_EVENT_MAX_AGE_MS)
}
