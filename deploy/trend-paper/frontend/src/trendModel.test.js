import assert from 'node:assert/strict'
import test from 'node:test'
import { freshSnapshot, signalChecks, recentFills, controlAvailability, money } from './trendModel.js'

const snapshot = () => ({ mode: 'PAPER', strategy_id: 'sol-ema-20-50-200-atr3-v1', symbol: 'SOL/USDT',
  market_fresh: true, quote: { observed_at: 100 }, updated_at: 100, quote_max_age_seconds: 10,
  history_complete: true, signal: {}, entries_enabled: false, error: null, position: { quantity: '.1' } })

test('cached/future/wrong-mode snapshots cannot enable paper entry or close', () => {
  for (const [s, now, failed] of [[snapshot(), 111, false], [snapshot(), 100, true],
    [{ ...snapshot(), mode: 'MAINNET' }, 100, false], [{ ...snapshot(), symbol: 'BTC/USDT' }, 100, false],
    [snapshot(), 95, false]]) {
    const fresh = freshSnapshot(s, now, failed)
    assert.equal(fresh, false)
    assert.equal(controlAvailability(s, fresh).start, false)
    assert.equal(controlAvailability(s, fresh).close, false)
  }
})
test('starting requires complete healthy indicators; pausing remains possible with stale quotes', () => {
  assert.equal(controlAvailability(snapshot(), true).start, true)
  assert.equal(controlAvailability({ ...snapshot(), history_complete: false }, true).start, false)
  assert.equal(controlAvailability({ ...snapshot(), error: 'gap' }, true).start, false)
  assert.equal(controlAvailability({ ...snapshot(), entries_enabled: true }, false).pause, true)
})
test('bullish state alone does not display a fresh crossover', () => {
  assert.deepEqual(signalChecks({ fast: 102, slow: 100, close: 101, long: 99, ema_entry: false }).map((x) => x.passed), [true, true, false])
})
test('BUY inventory is not displayed as realized profit; SELL uses its matched trade', () => {
  const fills = recentFills([{ id: 3, kind: 'trade', t: 100, reason: 'ema_exit', profit: '-.2' },
    { id: 2, kind: 'fill', t: 100, reason: 'ema_exit', side: 'sell' },
    { id: 1, kind: 'fill', t: 90, reason: 'ema_cross', side: 'buy' }])
  assert.equal(fills[0].profit, '-.2')
  assert.equal(fills[1].profit, null)
  assert.equal(money(null), '—')
  assert.equal(money(0), '0.00')
})
