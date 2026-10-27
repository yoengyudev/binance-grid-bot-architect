import assert from 'node:assert/strict'
import test from 'node:test'
import { spotFeedFor } from './tradingEnvironment.js'

test('both Spot feeds select their own REST and WebSocket environment', () => {
  const testnet = spotFeedFor('TESTNET')
  const mainnet = spotFeedFor('MAINNET')
  assert.equal(new URL(testnet.rest).hostname, 'testnet.binance.vision')
  assert.equal(new URL(testnet.websocket).hostname, 'stream.testnet.binance.vision')
  assert.equal(new URL(mainnet.rest).hostname, 'api.binance.com')
  assert.equal(new URL(mainnet.websocket).hostname, 'stream.binance.com')
  assert.ok(Object.isFrozen(testnet))
  assert.ok(Object.isFrozen(mainnet))
})

test('unknown or absent environments never fall back to a network', () => {
  for (const value of [null, undefined, '', 'testnet', 'MAINNET ', 'FUTURES', '__proto__', 'constructor']) {
    assert.equal(spotFeedFor(value), null)
  }
})
