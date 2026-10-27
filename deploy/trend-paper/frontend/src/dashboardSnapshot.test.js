import assert from 'node:assert/strict'
import test from 'node:test'
import { readDashboardSnapshot, DashboardReconcilingError, orderNeedsVerification } from './dashboardSnapshot.js'
import { SessionRequiredError } from './botStatus.js'

test('one authenticated request returns matching portfolio, ledger and valuation', async (t) => {
  const data = { snapshot_id: 'same-view', captured_at: '2026-10-18T13:31:23Z',
    bot: { grid_levels: 1 }, orders: { open_limits: [{ id: 'sell' }], filled_trades: [] } }
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify(data)))
  assert.deepEqual(await readDashboardSnapshot('/api/dashboard/snapshot'), data)
  assert.equal(fetch.mock.callCount(), 1)
  assert.equal(fetch.mock.calls[0].arguments[1].cache, 'no-store')
  assert.equal(fetch.mock.calls[0].arguments[1].credentials, 'include')
})

test('busy responses and expired sessions have distinct failure types', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response(
    JSON.stringify({ detail: { code: 'ENGINE_RECONCILING' } }), { status: 409 }))
  await assert.rejects(readDashboardSnapshot('/snapshot'), DashboardReconcilingError)
  fetch.mock.mockImplementation(async () => new Response('', { status: 401 }))
  await assert.rejects(readDashboardSnapshot('/snapshot'), SessionRequiredError)
})

test('mismatched row counts cannot be displayed as a coherent snapshot', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({
    snapshot_id: 'mixed', captured_at: '2026-10-18T13:31:23Z',
    bot: { grid_levels: 5 }, orders: { open_limits: [], filled_trades: [] },
  })))
  await assert.rejects(readDashboardSnapshot('/snapshot'), /inconsistent/)
})

test('crossed SELL and BUY rows require verification and are never declared filled', () => {
  const sell = { side: 'SELL', price: 81217.78, status: 'OPEN' }
  assert.equal(orderNeedsVerification(sell, 81496.29), true)
  assert.equal(sell.status, 'OPEN')
  assert.equal(orderNeedsVerification(sell, 81000), false)
  assert.equal(orderNeedsVerification({ ...sell, side: 'BUY' }, 81000), true)
  assert.equal(orderNeedsVerification({ ...sell, status: 'FILLED' }, 81496.29), false)
  assert.equal(orderNeedsVerification(sell, null), false)
})
