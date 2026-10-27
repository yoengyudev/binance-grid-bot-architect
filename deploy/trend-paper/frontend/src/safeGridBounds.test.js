import assert from 'node:assert/strict'
import test from 'node:test'
import { validateSafeGridBounds } from './safeGridBounds.js'

const savedGrid = { center: 85067.41, ceiling: 85401.37, stop: 85060.70,
  activeStop: 85060.65, requireActiveStop: true }

test('the reported saved grid is blocked below the retained stop', () => {
  const result = validateSafeGridBounds(savedGrid)
  assert.equal(result.valid, false)
  assert.equal(result.lowerBoundUnsafe, true)
  assert.ok(Math.abs(result.projectedLowerBound - 84733.45) < 1e-6)
})

test('equal or same-tick lower bounds are blocked; a higher tick is accepted', () => {
  const input = { center: 100, ceiling: 110, stop: 80, activeStop: 90, requireActiveStop: true }
  for (const ceiling of [110, 109.996]) {
    assert.equal(validateSafeGridBounds({ ...input, stop: 90, ceiling }).valid, false)
  }
  assert.equal(validateSafeGridBounds({ ...input, stop: 90, ceiling: 109.99 }).valid, true)
})

test('typed stop and newly raised active floors both constrain the lower bound', () => {
  const input = { center: 100, ceiling: 105, stop: 94, activeStop: 90, requireActiveStop: true }
  assert.equal(validateSafeGridBounds(input).valid, true)
  assert.equal(validateSafeGridBounds({ ...input, stop: 95 }).lowerBoundUnsafe, true)
  assert.equal(validateSafeGridBounds({ ...input, activeStop: 96 }).valid, false)
  assert.equal(validateSafeGridBounds({ ...input, stop: 89 }).stopBelowFloor, true)
})

test('missing active risk state fails closed but fresh-grid setup uses its entered stop', () => {
  const input = { center: 100, ceiling: 110, stop: 80 }
  assert.equal(validateSafeGridBounds({ ...input, requireActiveStop: true }).activeStopUnavailable, true)
  assert.equal(validateSafeGridBounds(input).valid, true)
})

test('invalid, collapsed, and unsafe numeric prices cannot produce a submittable grid', () => {
  for (const change of [{ center: NaN }, { ceiling: Infinity }, { ceiling: 0 }, { ceiling: 85000 }, { stop: 0 }]) {
    assert.equal(validateSafeGridBounds({ ...savedGrid, ...change }).valid, false)
  }
  assert.equal(validateSafeGridBounds({ center: 1e308, ceiling: 1.5e308, stop: 1 }).valid, false)
})
