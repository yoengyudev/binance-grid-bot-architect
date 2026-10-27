import assert from 'node:assert/strict'
import test from 'node:test'
import { PANIC_COMPLETE_MESSAGE, panicFailureMessage, panicLiquidationStatus, requestPanicLiquidation } from './panicLiquidation.js'
import { SessionRequiredError } from './botStatus.js'

const completed = { completed: true, phase: 'complete', mode: 'LIQUIDATED',
  engine_status: 'HALTED', residual_base: '0', sold_base: '0.00058' }

test('no request is sent without an explicit true confirmation', async () => {
  let calls = 0
  const original = globalThis.fetch
  globalThis.fetch = async () => { calls++; throw new Error('should not fetch') }
  try {
    for (const options of [undefined, {}, { confirmed: false }, { confirmed: 'true' }]) {
      await assert.rejects(requestPanicLiquidation('/panic', options), /Confirm/)
    }
    assert.equal(calls, 0)
  } finally { globalThis.fetch = original }
})

test('confirmed request posts once with credentials and accepts only a verified flat halt', async () => {
  const original = globalThis.fetch
  let calls = 0
  globalThis.fetch = async (url, options) => {
    calls++
    assert.equal(url, '/api/admin/force_liquidate')
    assert.equal(options.method, 'POST')
    assert.equal(options.credentials, 'include')
    assert.equal(options.headers['Content-Type'], 'application/json')
    assert.deepEqual(JSON.parse(options.body), { confirmed: true })
    assert.ok(options.signal instanceof AbortSignal)
    return { ok: true, status: 200, json: async () => completed }
  }
  try {
    assert.deepEqual(await requestPanicLiquidation('/api/admin/force_liquidate', { confirmed: true }), completed)
    assert.equal(calls, 1)
  } finally { globalThis.fetch = original }
})

test('pending, failed, incomplete, or non-flat responses cannot be shown as success', async () => {
  const original = globalThis.fetch
  try {
    for (const change of [{ completed: false }, { phase: 'confirming' }, { mode: 'LIQUIDATING' },
      { engine_status: 'RUNNING' }, { residual_base: '0.00001' }, { residual_base: null },
      { residual_base: 'unknown' }, { residual_base: '' }, { residual_base: ' ' }, { residual_base: false }]) {
      globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ...completed, ...change }) })
      await assert.rejects(requestPanicLiquidation('/panic', { confirmed: true }), /not confirmed flat/)
    }
  } finally { globalThis.fetch = original }
})

test('an expired admin session requires login', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async () => ({ ok: false, status: 401 })
  try {
    await assert.rejects(requestPanicLiquidation('/panic', { confirmed: true }), SessionRequiredError)
  } finally { globalThis.fetch = original }
})

test('liquidation alarms are surfaced without retrying the sale', async () => {
  const original = globalThis.fetch
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    return { ok: false, status: 409, json: async () => ({ detail: { message: 'BTC may remain; review the alarm.' } }) }
  }
  try {
    await assert.rejects(requestPanicLiquidation('/panic', { confirmed: true }), /BTC may remain/)
    assert.equal(calls, 1)
  } finally { globalThis.fetch = original }
})

test('a lost response never causes an automatic duplicate market exit', async () => {
  const original = globalThis.fetch
  let calls = 0
  globalThis.fetch = async () => { calls++; throw new Error('network timeout') }
  try {
    await assert.rejects(requestPanicLiquidation('/panic', { confirmed: true }), /network timeout/)
    assert.equal(calls, 1)
  } finally { globalThis.fetch = original }
})

const verifiedStatus = {
  status: 'Online', engine_status: 'HALTED', pause_mode: 'LIQUIDATED',
  trading_state: 'LIQUIDATED', can_reset_liquidation: true,
  wallet: { btc_held: 0 }, liquidation: { phase: 'complete', residual_base: '0' },
}

test('a 504 is resolved by a later verified status without resubmitting the sale', async () => {
  const original = globalThis.fetch
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    return { ok: false, status: 504, json: async () => { throw new Error('HTML gateway response') } }
  }
  try {
    await assert.rejects(requestPanicLiquidation('/panic', { confirmed: true }), (failure) => {
      assert.match(panicFailureMessage(failure), /timed out.*Checking/)
      assert.equal(failure.requestRejected, false)
      const result = panicLiquidationStatus(verifiedStatus)
      assert.deepEqual(result, { state: 'complete', message: PANIC_COMPLETE_MESSAGE })
      assert.equal(panicFailureMessage(failure, result), '')
      return true
    })
    assert.equal(calls, 1)
  } finally { globalThis.fetch = original }
})

test('cancelled orders or a zero balance alone never confirm liquidation completion', () => {
  for (const phase of [undefined, 'canceling', 'submitting', 'confirming']) {
    assert.deepEqual(panicLiquidationStatus({ ...verifiedStatus,
      liquidation: { phase, residual_base: '0' }, grid_levels: 0 }), { state: 'pending' })
  }
})

test('cached completion from before the request cannot resolve a new submission', () => {
  assert.deepEqual(panicLiquidationStatus(verifiedStatus, verifiedStatus), { state: 'pending' })
  assert.equal(panicLiquidationStatus({ ...verifiedStatus }, verifiedStatus).state, 'complete')
})

test('unverified status, remaining BTC, and active alarms cannot produce success', () => {
  for (const change of [
    { status: 'Offline' }, { engine_status: 'RUNNING' }, { pause_mode: 'LIQUIDATING' },
    { trading_state: 'HALTED' }, { can_reset_liquidation: false },
    { engine_fault: 'Unconfirmed sale' }, { order_cleanup_required: true },
    { unresolved_order_alarm: 'Cancel pending' }, { ghost_order_alarm: 'Unknown order' },
    { wallet: { btc_held: '0.00001' } }, { wallet: null },
    ...[null, false, '', ' ', 'unknown', '0.00001'].map((residual) => ({
      liquidation: { phase: 'complete', residual_base: residual },
    })),
  ]) assert.equal(panicLiquidationStatus({ ...verifiedStatus, ...change }).state, 'pending')
})

test('terminal failures preserve their explanation when the POST subsequently times out', () => {
  const failure = new Error('The server response timed out (504).')
  const failed = panicLiquidationStatus({ ...verifiedStatus, liquidation: {
    phase: 'failed', residual_base: '0.00001', failure_reason: 'BTC is below MARKET minimums.',
  } })
  assert.deepEqual(failed, { state: 'failed', message: 'BTC is below MARKET minimums.' })
  assert.equal(panicFailureMessage(failure, failed), failed.message)
  const dust = panicLiquidationStatus({ ...verifiedStatus,
    liquidation: { phase: 'dust_acknowledged', residual_base: '0.00001' } })
  assert.equal(dust.state, 'failed')
  assert.match(dust.message, /not sold/)
})

test('authentication and explicitly rejected submissions do not claim an exit is running', async () => {
  assert.doesNotMatch(panicFailureMessage(new SessionRequiredError()), /Checking/)
  const original = globalThis.fetch
  globalThis.fetch = async () => ({ ok: false, status: 403, json: async () => ({ detail: 'Untrusted origin' }) })
  try {
    await assert.rejects(requestPanicLiquidation('/panic', { confirmed: true }), (failure) => {
      assert.equal(failure.requestRejected, true)
      assert.equal(panicFailureMessage(failure), 'Untrusted origin')
      return true
    })
  } finally { globalThis.fetch = original }
})
