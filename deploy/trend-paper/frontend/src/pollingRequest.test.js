import assert from 'node:assert/strict'
import test from 'node:test'
import { createPollingRequest } from './pollingRequest.js'

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function harness(load) {
  const timers = new Map()
  let id = 0
  const events = []
  const request = createPollingRequest({ load, timeoutMs: 5000,
    setTimer(fn) { timers.set(++id, fn); return id },
    clearTimer(key) { timers.delete(key) },
    onStart: () => events.push('start'),
    onSuccess: (value) => events.push(['data', value]),
    onError: (error) => events.push(['error', error.message]),
    onFinish: () => events.push('finish'),
  })
  return { request, events, timers }
}

test('overlapping ticks share one request and finish after the data is committed', async () => {
  const pending = deferred()
  let calls = 0
  const h = harness(() => { calls++; return pending.promise })
  const first = h.request.run()
  assert.equal(h.request.run(), first)
  await Promise.resolve()
  assert.equal(calls, 1)
  assert.deepEqual(h.events, ['start'])
  pending.resolve({ orders: [1] })
  await first
  assert.deepEqual(h.events, ['start', ['data', { orders: [1] }], 'finish'])
  assert.equal(h.timers.size, 0)
})

test('failed refresh reports an error without publishing empty data and the next tick recovers', async () => {
  let failed = true
  const h = harness(() => { if (failed) throw new Error('Network down'); return { orders: [2] } })
  await h.request.run()
  assert.deepEqual(h.events, ['start', ['error', 'Network down'], 'finish'])
  failed = false
  await h.request.run()
  assert.deepEqual(h.events.slice(3), ['start', ['data', { orders: [2] }], 'finish'])
})

test('a timeout releases the busy state even if the transport ignores abort; late data is discarded', async () => {
  const pending = deferred()
  let signal
  const h = harness((value) => { signal = value; return pending.promise })
  const task = h.request.run()
  await Promise.resolve()
  h.timers.values().next().value()
  await task
  assert.equal(signal.aborted, true)
  assert.deepEqual(h.events, ['start', ['error', 'Request timed out.'], 'finish'])
  assert.equal(h.timers.size, 0)
  pending.resolve('obsolete snapshot')
  await Promise.resolve()
  assert.equal(h.events.length, 3)
})

test('environment cancellation retires old results without finishing the new request', async () => {
  const old = deferred(), next = deferred()
  let calls = 0
  const h = harness(() => (++calls === 1 ? old.promise : next.promise))
  const first = h.request.run()
  await Promise.resolve()
  h.request.cancel()
  const second = h.request.run()
  old.resolve('TESTNET')
  await first
  assert.deepEqual(h.events, ['start', 'finish', 'start'])
  assert.equal(h.request.run(), second)
  next.resolve('MAINNET')
  await second
  assert.deepEqual(h.events, ['start', 'finish', 'start', ['data', 'MAINNET'], 'finish'])
  assert.equal(h.timers.size, 0)
})

test('unmount aborts and settles outstanding work without updating component state', async () => {
  const pending = deferred()
  let signal
  const h = harness((value) => { signal = value; return pending.promise })
  const task = h.request.run()
  await Promise.resolve()
  h.request.dispose()
  await task
  assert.equal(signal.aborted, true)
  pending.resolve('late response')
  await Promise.resolve()
  await h.request.run()
  assert.deepEqual(h.events, ['start'])
  assert.equal(h.timers.size, 0)
})

test('disposing before the transport starts prevents a fetch after logout', async () => {
  let called = false
  const h = harness(() => { called = true })
  const task = h.request.run()
  h.request.dispose()
  await task
  assert.equal(called, false)
  assert.deepEqual(h.events, ['start'])
})
