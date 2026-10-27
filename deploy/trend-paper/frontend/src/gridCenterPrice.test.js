import assert from 'node:assert/strict'
import test from 'node:test'
import { freshCenterPrice, resolveGridCenter } from './gridCenterPrice.js'

const now = 1_000_000
const quote = { price: 81599.637, environment: 'TESTNET', receivedAt: now - 1000 }
const input = { manualProvided: false, quote, environment: 'TESTNET', now }

test('an empty center uses a same-environment fresh snapshot rounded to the price tick', () => {
  assert.deepEqual(resolveGridCenter(input), { price: 81599.64, source: 'live', error: '' })
})

test('manual values take priority even if the live feed is missing, stale, or different', () => {
  for (const live of [null, quote, { ...quote, receivedAt: 0 }, { ...quote, environment: 'MAINNET' }]) {
    assert.deepEqual(resolveGridCenter({ ...input, quote: live, manualProvided: true, manualPrice: 81000 }),
      { price: 81000, source: 'manual', error: '' })
  }
})

test('an invalid nonempty manual field never silently falls back to a live price', () => {
  for (const manualPrice of [NaN, 0, -1, Infinity]) {
    const result = resolveGridCenter({ ...input, manualProvided: true, manualPrice })
    assert.equal(result.price, null)
    assert.match(result.error, /valid Center Price/)
  }
})

test('stale, future-dated, missing, invalid, and wrong-environment quotes are rejected', () => {
  for (const live of [null, { ...quote, receivedAt: now - 15000 }, { ...quote, receivedAt: now + 1 },
    { ...quote, price: NaN }, { ...quote, price: 0 }, { ...quote, price: Number.MAX_VALUE },
    { ...quote, receivedAt: undefined }, { ...quote, environment: 'MAINNET' }]) {
    assert.equal(freshCenterPrice(live, 'TESTNET', now), null)
    assert.equal(resolveGridCenter({ ...input, quote: live }).price, null)
  }
  assert.equal(freshCenterPrice(quote, null, now), null)
})

test('freshness is rechecked at click time even before a delayed watchdog runs', () => {
  assert.notEqual(freshCenterPrice(quote, 'TESTNET', quote.receivedAt + 14999), null)
  assert.equal(freshCenterPrice(quote, 'TESTNET', quote.receivedAt + 15000), null)
})

test('later ticks cannot mutate a center already captured for review', () => {
  const selected = resolveGridCenter(input)
  const later = resolveGridCenter({ ...input, quote: { ...quote, price: 82000 } })
  assert.equal(selected.price, 81599.64)
  assert.equal(later.price, 82000)
  assert.equal(quote.price, 81599.637)
})

test('recent receipt does not extend the lifetime of an old exchange event', () => {
  assert.equal(freshCenterPrice({ ...quote, eventAt: now - 16_000 }, 'TESTNET', now), null)
  assert.notEqual(freshCenterPrice({ ...quote, eventAt: now - 10_000 }, 'TESTNET', now), null)
  assert.equal(freshCenterPrice({ ...quote, eventAt: undefined }, 'TESTNET', now), null)
})
