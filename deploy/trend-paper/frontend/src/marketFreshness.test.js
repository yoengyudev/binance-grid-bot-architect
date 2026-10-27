import assert from 'node:assert/strict'
import test from 'node:test'
import { isFreshKline, isFreshMarketEvent } from './marketFreshness.js'

const now = 1_800_000_020_000
test('only current exchange events can certify a ticker as live', () => {
  assert.equal(isFreshMarketEvent(now, now), true)
  assert.equal(isFreshMarketEvent(now - 14_000, now), true)
  for (const value of [now - 16_000, now - 7_200_000, now + 6_000, null, undefined, '123', NaN]) {
    assert.equal(isFreshMarketEvent(value, now), false, String(value))
  }
})

test('fresh arrival/event cannot revive a historical kline', () => {
  const start = Math.floor(now / 60_000) * 60_000
  assert.equal(isFreshKline({ E: now, k: { t: start } }, '1m', now), true)
  assert.equal(isFreshKline({ E: now, k: { t: start - 7_200_000 } }, '1m', now), false)
  assert.equal(isFreshKline({ E: now - 7_200_000, k: { t: start } }, '1m', now), false)
  assert.equal(isFreshKline({ E: now, k: { t: start + 1 } }, '1m', now), false)
})

test('hourly candles may be nearly an hour old, but their events must be fresh', () => {
  const start = Math.floor(now / 3_600_000) * 3_600_000
  assert.equal(isFreshKline({ E: now, k: { t: start } }, '1h', now), true)
  assert.equal(isFreshKline({ E: now, k: { t: start - 3_600_000 } }, '1h', now), false)
})

test('allow a just-closed candle at the interval boundary', () => {
  const boundary = Math.floor(now / 60_000) * 60_000
  assert.equal(isFreshKline({ E: boundary + 100, k: { t: boundary - 60_000 } }, '1m', boundary + 100), true)
  assert.equal(isFreshKline({ E: boundary + 16_000, k: { t: boundary - 60_000 } }, '1m', boundary + 16_000), false)
})
