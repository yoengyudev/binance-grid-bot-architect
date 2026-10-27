import assert from 'node:assert/strict'
import test from 'node:test'
import { engineRecovery } from './engineRecovery.js'
import { engineStartError } from './engineStartError.js'

const completed = { status: 'Online', trading_state: 'LIQUIDATED', pause_mode: 'LIQUIDATED',
  engine_status: 'HALTED', can_reset_liquidation: true, engine_fault: null }

test('completed liquidation displays HALTED and opens only new-grid configuration', () => {
  assert.deepEqual(engineRecovery(completed), { liquidated: true,
    canConfigureNewGrid: true, gridConfigurationLocked: false, badge: 'HALTED' })
})

test('server permission is required; generic HALTED cannot open the grid form', () => {
  for (const bot of [{ ...completed, can_reset_liquidation: undefined },
    { ...completed, trading_state: 'ACTIVE', pause_mode: null },
    { ...completed, can_reset_liquidation: false }]) {
    const result = engineRecovery(bot)
    assert.equal(result.canConfigureNewGrid, false)
    assert.equal(result.gridConfigurationLocked, true)
    assert.equal(result.badge, 'HALTED')
  }
})

test('unresolved liquidation and cleanup or fault alarms stay locked', () => {
  for (const change of [{ trading_state: 'LIQUIDATING' }, { pause_mode: 'HALTED' },
    { pause_mode: 'LIQUIDATION_HALTED' },
    { engine_fault: 'timeout' }, { order_cleanup_required: true },
    { unresolved_order_alarm: 'unverified cancellation' }, { ghost_order_alarm: 'unknown order' },
    { status: 'Offline' }, { engine_status: 'RUNNING' }]) {
    assert.equal(engineRecovery({ ...completed, ...change }).canConfigureNewGrid, false)
    assert.equal(engineRecovery({ ...completed, ...change }).gridConfigurationLocked, true)
  }
})

test('a saved new grid returns to IDLE and normal START review', () => {
  assert.deepEqual(engineRecovery({ status: 'Online', trading_state: 'IDLE', engine_status: 'IDLE',
    pending_grid: { source: 'manual_liquidation_reset' }, can_reset_liquidation: false }), {
    liquidated: false, canConfigureNewGrid: false, gridConfigurationLocked: false, badge: 'IDLE',
  })
  assert.equal(engineRecovery({ engine_status: 'RUNNING' }).badge, 'ACTIVE')
})

test('completed-stop API rejection routes to settings; genuine faults do not', () => {
  assert.equal(engineStartError('Hard-stop liquidation is complete. Save a new grid in Settings, then review and START the saved grid.', 409).reanchor, true)
  assert.equal(engineStartError('An unresolved trading fault requires review before resetting liquidation.', 409).reanchor, false)
})
