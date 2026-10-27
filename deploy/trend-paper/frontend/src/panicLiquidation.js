import { SessionRequiredError } from './botStatus.js'

export const PANIC_COMPLETE_MESSAGE = 'Bot BTC is verified flat. Engine halted. Save a new grid before START.'

export function panicFailureMessage(failure, savedResult) {
  if (savedResult?.state === 'complete') return ''
  if (savedResult?.state === 'failed') return savedResult.message
  const message = failure.message || 'Cannot reach the bot API.'
  return failure.requestRejected || failure instanceof SessionRequiredError ? message
    : `${message} Checking the saved liquidation result. Do not submit another exit.`
}

function isZero(value) {
  return (typeof value === 'number' || (typeof value === 'string' && value.trim() !== ''))
    && Number(value) === 0
}

export function panicLiquidationStatus(bot, previousBot) {
  // Only a newly fetched status can resolve a submitted exit, never a cached result.
  if (!bot || bot === previousBot || bot.status !== 'Online') return { state: 'pending' }
  const liquidation = bot.liquidation
  if (liquidation?.phase === 'failed') {
    return { state: 'failed', message: liquidation.failure_reason
      || bot.engine_fault || 'Liquidation halted before completion. BTC may remain; review the recovery controls in Settings.' }
  }
  if (liquidation?.phase === 'dust_acknowledged') {
    return { state: 'failed', message: 'Remaining BTC was recorded as unmonitored dust. It was not sold.' }
  }
  if (liquidation?.phase === 'complete' && isZero(liquidation.residual_base)
      && isZero(bot.wallet?.btc_held) && bot.engine_status === 'HALTED'
      && bot.pause_mode === 'LIQUIDATED' && bot.trading_state === 'LIQUIDATED'
      && bot.can_reset_liquidation === true && !bot.engine_fault
      && !bot.order_cleanup_required && !bot.unresolved_order_alarm && !bot.ghost_order_alarm) {
    return { state: 'complete', message: PANIC_COMPLETE_MESSAGE }
  }
  return { state: 'pending' }
}

export async function requestPanicLiquidation(url, { confirmed = false } = {}) {
  if (confirmed !== true) throw new Error('Confirm the panic liquidation before submitting.')
  // Submit once. A failed connection does not prove that Binance rejected the sale.
  const response = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmed: true }),
    signal: AbortSignal.timeout(8_000),
  })
  if (response.status === 401) throw new SessionRequiredError()
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const message = typeof data?.detail === 'string' ? data.detail : data?.detail?.message
    const failure = new Error(message || (response.status === 504
      ? 'The server response timed out (504).'
      : `Panic liquidation could not confirm completion (${response.status}).`))
    failure.requestRejected = [400, 403, 404, 405, 422].includes(response.status)
    throw failure
  }
  if (data?.completed !== true || data?.phase !== 'complete'
      || data?.engine_status !== 'HALTED' || data?.mode !== 'LIQUIDATED'
      || !isZero(data?.residual_base)) {
    throw new Error('Liquidation is not confirmed flat. Check the engine state and liquidation alarms.')
  }
  return data
}
