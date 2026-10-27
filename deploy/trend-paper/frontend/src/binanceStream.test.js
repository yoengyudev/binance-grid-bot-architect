import assert from 'node:assert/strict'
import test from 'node:test'
import { connectBinanceStream } from './binanceStream.js'
import { isFreshMarketEvent } from './marketFreshness.js'

function harness(t) {
  const originalWindow = globalThis.window
  const originalSocket = globalThis.WebSocket
  const originalRandom = Math.random
  let now = 0
  let nextTimer = 0
  const timers = new Map()
  const sockets = []
  const states = []
  let constructorFails = false
  let disconnect

  globalThis.window = {
    setTimeout(callback, delay) {
      const id = ++nextTimer
      timers.set(id, { at: now + delay, callback })
      return id
    },
    clearTimeout(id) { timers.delete(id) },
  }
  globalThis.WebSocket = class {
    constructor(url) {
      if (constructorFails) throw new Error('Cannot connect')
      this.url = url
      this.closed = false
      sockets.push(this)
    }
    close() { this.closed = true; this.onclose?.() }
    message(data = 'valid') { this.onmessage?.({ data }) }
  }
  Math.random = () => 0.5
  t.after(() => {
    disconnect?.()
    globalThis.window = originalWindow
    globalThis.WebSocket = originalSocket
    Math.random = originalRandom
  })

  return {
    sockets, states, timers,
    failConstructor() { constructorFails = true },
    nextDelay() { return Math.min(...[...timers.values()].map((timer) => timer.at - now)) },
    advance(ms) {
      const end = now + ms
      while (true) {
        const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0]
        if (!next || next[1].at > end) break
        now = next[1].at
        timers.delete(next[0])
        next[1].callback()
      }
      now = end
    },
    start(onMessage = (event) => event.data === 'valid') {
      const stop = connectBinanceStream('wss://example.test/btcusdt@ticker', {
        onMessage,
        onStateChange: (state) => states.push(state),
      })
      disconnect = stop
      return stop
    },
  }
}

test('replayed market events cannot keep the transport watchdog LIVE', (t) => {
  const h = harness(t)
  let clock = 1_800_000_020_000
  h.start((event) => isFreshMarketEvent(JSON.parse(event.data).E, clock))
  h.sockets[0].message(JSON.stringify({ E: clock }))
  assert.equal(h.states.at(-1), 'Live')
  h.advance(14_000)
  clock += 14_000
  h.sockets[0].message(JSON.stringify({ E: clock - 7_200_000 }))
  h.advance(1_000)
  assert.equal(h.states.at(-1), 'Reconnecting')
  assert.equal(h.sockets[0].closed, true)
})

test('silent and stalled handshakes become stale at 15s and reconnect without waiting for close', (t) => {
  const h = harness(t)
  h.start()
  assert.deepEqual(h.states, ['Connecting'])
  h.advance(14_999)
  assert.equal(h.sockets[0].closed, false)
  h.advance(1)
  assert.equal(h.states.at(-1), 'Reconnecting')
  assert.equal(h.sockets[0].closed, true)
  h.advance(750)
  assert.equal(h.sockets.length, 2)
  h.sockets[1].message()
  assert.equal(h.states.at(-1), 'Live')
})

test('valid messages refresh watchdog, invalid messages do not, retired sockets cannot revive freshness', (t) => {
  const h = harness(t)
  h.start()
  const oldMessageHandler = h.sockets[0].onmessage
  h.advance(10_000)
  h.sockets[0].message()
  h.advance(14_000)
  h.sockets[0].message('invalid')
  h.advance(1_000)
  assert.equal(h.states.at(-1), 'Reconnecting')
  oldMessageHandler({ data: 'valid' })
  assert.equal(h.states.at(-1), 'Reconnecting')
})

test('failures use capped exponential jitter; only valid data resets retries', (t) => {
  const h = harness(t)
  h.start()
  for (const delay of [750, 1_500, 3_000, 6_000, 12_000, 22_500, 22_500]) {
    const socket = h.sockets.at(-1)
    const oldCloseHandler = socket.onclose
    socket.onerror()
    oldCloseHandler() // A queued close after error must not schedule a duplicate.
    assert.equal(h.nextDelay(), delay)
    assert.equal(h.timers.size, 1)
    const count = h.sockets.length
    h.advance(delay)
    assert.equal(h.sockets.length, count + 1)
  }
  h.sockets.at(-1).message()
  h.sockets.at(-1).onclose()
  assert.equal(h.nextDelay(), 750)
})

test('constructor failures back off and cleanup cancels scheduled reconnect', (t) => {
  const h = harness(t)
  h.failConstructor()
  const stop = h.start()
  assert.equal(h.states.at(-1), 'Reconnecting')
  assert.equal(h.nextDelay(), 750)
  h.advance(750)
  assert.equal(h.nextDelay(), 1_500)
  stop()
  assert.equal(h.timers.size, 0)
  h.advance(60_000)
  assert.equal(h.sockets.length, 0)
})

test('unmount closes the socket, clears watchdog and ignores queued events', (t) => {
  const h = harness(t)
  const stop = h.start()
  const socket = h.sockets[0]
  const oldMessageHandler = socket.onmessage
  stop()
  oldMessageHandler({ data: 'valid' })
  h.advance(60_000)
  assert.equal(socket.closed, true)
  assert.equal(h.timers.size, 0)
  assert.equal(h.sockets.length, 1)
  assert.deepEqual(h.states, ['Connecting'])
})
