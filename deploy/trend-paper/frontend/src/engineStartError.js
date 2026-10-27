export function engineStartError(detail, status) {
  const message = typeof detail === 'string' ? detail
    : typeof detail?.message === 'string' ? detail.message
      : `Engine start failed (${status}).`
  return {
    message,
    reanchor: detail?.code === 'GRID_STOP_CONFLICT'
      || message.includes('Hard-stop liquidation is complete.')
      || message.includes('Trailing stop would remove requested BUY levels')
      || message.includes('re-anchor first'),
  }
}
