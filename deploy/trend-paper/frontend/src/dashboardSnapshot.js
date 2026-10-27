import { SessionRequiredError } from './botStatus.js'

export class DashboardReconcilingError extends Error {
  constructor() {
    super('Engine is reconciling; showing the last complete dashboard snapshot.')
    this.name = 'DashboardReconcilingError'
  }
}

export async function readDashboardSnapshot(url, { signal } = {}) {
  const response = await fetch(url, { credentials: 'include', cache: 'no-store', signal })
  if (response.status === 401) throw new SessionRequiredError()
  if (response.status === 409) {
    const data = await response.json()
    if (data?.detail?.code === 'ENGINE_RECONCILING') throw new DashboardReconcilingError()
  }
  if (!response.ok) throw new Error(`Dashboard snapshot request failed (${response.status}).`)
  const data = await response.json()
  if (!data?.snapshot_id || !Number.isFinite(Date.parse(data.captured_at)) || !data.bot
    || !Array.isArray(data.orders?.open_limits) || !Array.isArray(data.orders?.filled_trades)
    || data.bot.grid_levels !== data.orders.open_limits.length
    || (data.mark_price != null && (!Number.isFinite(Number(data.mark_price)) || Number(data.mark_price) <= 0))
    || (data.mark_price_at != null && !Number.isFinite(Date.parse(data.mark_price_at)))) {
    throw new Error('Dashboard snapshot is inconsistent or invalid.')
  }
  return data
}

// A last trade crossing a limit suggests a fill; only the engine may confirm it.
export function orderNeedsVerification(order, livePrice) {
  const price = Number(order.price)
  if (!['OPEN', 'PARTIALLY_FILLED'].includes(order.status)
    || !Number.isFinite(price) || price <= 0 || !Number.isFinite(livePrice) || livePrice <= 0) return false
  return order.side === 'SELL' ? livePrice >= price : order.side === 'BUY' && livePrice <= price
}
