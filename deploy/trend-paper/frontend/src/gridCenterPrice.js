import { spotFeedFor } from './tradingEnvironment.js'
import { isFreshMarketEvent } from './marketFreshness.js'

const MAX_QUOTE_AGE_MS = 15_000

export function freshCenterPrice(quote, environment, now = Date.now()) {
  if (!spotFeedFor(environment) || quote?.environment !== environment) return null
  if (!(Number.isFinite(quote.price) && quote.price > 0
    && Number.isFinite(quote.receivedAt) && Number.isFinite(now))) return null
  const age = now - quote.receivedAt
  if (age < 0 || age >= MAX_QUOTE_AGE_MS) return null
  if (Object.hasOwn(quote, 'eventAt') && !isFreshMarketEvent(quote.eventAt, now)) return null
  const ticks = Math.round(quote.price * 100)
  return Number.isSafeInteger(ticks) && ticks > 0 ? ticks / 100 : null
}

export function resolveGridCenter({ manualProvided, manualPrice, quote, environment, now = Date.now() }) {
  if (manualProvided) {
    return Number.isFinite(manualPrice) && manualPrice > 0
      ? { price: manualPrice, source: 'manual', error: '' }
      : { price: null, source: null, error: 'Enter a valid Center Price, or clear it to use the live price.' }
  }
  const price = freshCenterPrice(quote, environment, now)
  return price === null
    ? { price: null, source: null, error: 'Waiting for a fresh live price. You can also enter Center Price manually.' }
    : { price, source: 'live', error: '' }
}
