import assert from 'node:assert/strict'
import test from 'node:test'
import { catastrophicStop, fixedGridStop, feeDefendedBounds, compressedAtrBounds, validateGridSpacing } from './gridSpacing.js'

test('tight ATR grids are widened for every rounded even/odd lane', () => {
  for (const center of [100, 81260, 110860]) {
    for (const levels of [2, 8, 9, 50, 100]) {
      const bounds = feeDefendedBounds(center, center * 0.001, levels)
      const actual = validateGridSpacing({ center, ceiling: bounds.ceiling, levels })
      assert.equal(actual.valid, true)
      assert.ok(actual.minimumGrossPercent >= 0.4 - 1e-12)
      assert.ok(bounds.lower > 0)
    }
  }
})

test('manual tight grids are blocked, wider grids are preserved', () => {
  assert.equal(validateGridSpacing({ center: 100, ceiling: 101, levels: 8 }).valid, false)
  assert.equal(validateGridSpacing({ center: 100, ceiling: 110, levels: 8 }).valid, true)
  assert.equal(feeDefendedBounds(100, 10, 8).ceiling, 110)
})

test('exact 0.4 percent tick boundary is accepted', () => {
  assert.equal(validateGridSpacing({ center: 100, ceiling: 100.4, levels: 2 }).valid, true)
  assert.equal(validateGridSpacing({ center: 100, ceiling: 100.39, levels: 2 }).valid, false)
})

test('production compression clears both the ATR target and fee floor', () => {
  for (const atr of [1, 300, 2000]) {
    const bounds = compressedAtrBounds(62772.01, atr, 5)
    const actual = validateGridSpacing({ center: 62772.01, ceiling: bounds.ceiling, levels: 5 })
    assert.ok(actual.minimumGrossPercent / 100 >= Math.max(0.004, 0.5 * atr / 62772.01) - 1e-12)
  }
  assert.throws(() => compressedAtrBounds(100, 1, 8))
})

test('invalid or excessive bounds never become submittable', () => {
  for (const input of [{ center: NaN, ceiling: 101, levels: 8 },
    { center: 100, ceiling: 101, levels: 1 }, { center: 100, ceiling: 200, levels: 8 }]) {
    assert.equal(validateGridSpacing(input).valid, false)
  }
  assert.throws(() => feeDefendedBounds(100, 1, 1))
})

test('catastrophic stop has a fixed fifteen percent gap rounded down', () => {
  assert.equal(catastrophicStop(100), 85)
  assert.equal(catastrophicStop(107269.52), 91179.09)
  assert.ok(Number.isNaN(catastrophicStop(-1)))
})

test('fixed form floor follows bounds and never lowers an active floor', () => {
  assert.equal(fixedGridStop(100, 120), 68)
  assert.equal(fixedGridStop(100, 120, 70), 70)
  assert.equal(fixedGridStop(100, 120, 60), 68)
  assert.ok(Number.isNaN(fixedGridStop(100, 200)))
})
