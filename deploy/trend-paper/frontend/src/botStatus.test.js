import assert from 'node:assert/strict'
import test from 'node:test'
import { readBotStatus, SessionRequiredError } from './botStatus.js'

test('status polling and START preflight carry the admin cookie across origins', async (t) => {
  const signal = new AbortController().signal
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response(
    JSON.stringify({ status: 'Online', trading_environment: 'TESTNET', engine_status: 'IDLE' }),
  ))
  const result = await readBotStatus('http://localhost:8000/api/bot/status', { signal })
  assert.equal(result.engine_status, 'IDLE')
  assert.deepEqual(fetchMock.mock.calls[0].arguments, [
    'http://localhost:8000/api/bot/status', { credentials: 'include', cache: 'no-store', signal },
  ])
})

test('an expired session is distinguishable from an offline backend', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('', { status: 401 }))
  await assert.rejects(readBotStatus('/api/bot/status'), SessionRequiredError)
})

test('server errors and network failures retain their actual failure category', async (t) => {
  const fetchMock = t.mock.method(globalThis, 'fetch', async () => new Response('', { status: 502 }))
  await assert.rejects(readBotStatus('/api/bot/status'), (error) => {
    assert.equal(error instanceof SessionRequiredError, false)
    assert.match(error.message, /502/)
    return true
  })
  fetchMock.mock.mockImplementation(async () => { throw new TypeError('Network unavailable') })
  await assert.rejects(readBotStatus('/api/bot/status'), /Network unavailable/)
})
