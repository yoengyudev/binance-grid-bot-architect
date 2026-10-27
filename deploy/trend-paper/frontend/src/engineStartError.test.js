import assert from 'node:assert/strict'
import test from 'node:test'
import { engineStartError } from './engineStartError.js'

test('stop conflicts show backend prices and provide re-anchor recovery', () => {
  const failure = engineStartError({
    code: 'GRID_STOP_CONFLICT', message: 'Lowest BUY 76000 is below hard stop 79789.70.',
  }, 409)
  assert.equal(failure.message, 'Lowest BUY 76000 is below hard stop 79789.70.')
  assert.equal(failure.reanchor, true)
})

test('existing servers retain actionable recovery before backend deployment', () => {
  assert.equal(engineStartError('Trailing stop would remove requested BUY levels from the exact grid.', 409).reanchor, true)
  assert.equal(engineStartError('Market price is outside the saved grid; re-anchor first.', 409).reanchor, true)
  assert.equal(engineStartError('A prior stop still needs Binance order cleanup.', 409).reanchor, false)
  assert.deepEqual(engineStartError(null, 502), { message: 'Engine start failed (502).', reanchor: false })
})
