export const LIQUIDATION_RESET_MESSAGE = 'Hard stop completed. Configure and save a new grid in Settings, then review and START it.'

export function engineRecovery(bot) {
  const state = bot?.trading_state
  const mode = bot?.pause_mode
  const engine = bot?.engine_status
  const unresolved = [state, mode].some((value) => ['LIQUIDATING', 'HALTED', 'LIQUIDATION_HALTED'].includes(value))
  const liquidated = !unresolved && (state === 'LIQUIDATED' || mode === 'LIQUIDATED')
  const canConfigureNewGrid = liquidated && bot?.can_reset_liquidation === true
    && bot?.status === 'Online' && ['IDLE', 'HALTED'].includes(engine)
    && !bot?.engine_fault && !bot?.order_cleanup_required
    && !bot?.unresolved_order_alarm && !bot?.ghost_order_alarm
  const locked = unresolved || liquidated
    || ['HALTED', 'ACCOUNT_CLEARED'].includes(state) || engine === 'HALTED'
  const badge = state === 'ACCOUNT_CLEARED' ? 'CLOSED'
    : unresolved || liquidated || engine === 'HALTED' || state === 'HALTED' ? 'HALTED'
      : bot?.engine_fault ? 'FAULT'
      : engine === 'RUNNING' ? 'ACTIVE' : 'IDLE'
  return { liquidated, canConfigureNewGrid, gridConfigurationLocked: locked && !canConfigureNewGrid, badge }
}
