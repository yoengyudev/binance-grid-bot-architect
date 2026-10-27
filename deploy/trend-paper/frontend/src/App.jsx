import { useEffect, useMemo, useRef, useState } from 'react'
import GridTerminalChart from './GridTerminalChart.jsx'
import { spotFeedFor } from './tradingEnvironment.js'
import { engineStartError } from './engineStartError.js'
import { connectBinanceStream } from './binanceStream.js'
import { isFreshMarketEvent } from './marketFreshness.js'
import { readBotStatus, SessionRequiredError } from './botStatus.js'
import { readDashboardSnapshot, DashboardReconcilingError, orderNeedsVerification } from './dashboardSnapshot.js'
import { formatDateTime, formatTime } from './timeFormat.js'
import { validateSafeGridBounds } from './safeGridBounds.js'
import { catastrophicStop, fixedGridStop, compressedAtrBounds, validateGridSpacing } from './gridSpacing.js'
import { freshCenterPrice, resolveGridCenter } from './gridCenterPrice.js'
import { engineRecovery, LIQUIDATION_RESET_MESSAGE } from './engineRecovery.js'
import { PANIC_COMPLETE_MESSAGE, panicFailureMessage, panicLiquidationStatus, requestPanicLiquidation } from './panicLiquidation.js'
import { createPollingRequest } from './pollingRequest.js'
import { ActionContent, DashboardSkeleton, InlineSpinner, Skeleton } from './DashboardLoading.jsx'
import { dashboardLayout } from './dashboardLayout.js'

const API_BASE = import.meta.env.DEV
  ? `${window.location.protocol}//${window.location.hostname}:8000`
  : window.location.origin
const STATUS_URL = `${API_BASE}/api/bot/status`
const SNAPSHOT_URL = `${API_BASE}/api/dashboard/snapshot`
const LOGIN_URL = `${API_BASE}/api/auth/login`
const AUTH_ME_URL = `${API_BASE}/api/auth/me`
const REFRESH_URL = `${API_BASE}/api/auth/refresh`
const LOGOUT_URL = `${API_BASE}/api/auth/logout`
const PAUSE_URL = `${API_BASE}/api/bot/pause`
const PREPARE_RESET_URL = `${API_BASE}/api/bot/prepare-reset`
const ENGINE_START_URL = `${API_BASE}/api/engine/start`
const ENGINE_STOP_URL = `${API_BASE}/api/engine/stop`
const PANIC_LIQUIDATE_URL = `${API_BASE}/api/admin/force_liquidate`
const ACKNOWLEDGE_DUST_URL = `${API_BASE}/api/admin/acknowledge-dust`
const STOP_LOSS_URL = `${API_BASE}/api/bot/stop-loss`
const RECENTER_URL = `${API_BASE}/api/bot/grid/recenter`
const FACTORY_RESET_URL = `${API_BASE}/api/admin/factory-reset`
const WALLET_BALANCE_URL = `${API_BASE}/api/wallet-balance`
const REFRESH_INTERVAL_MS = 10_000
const BALANCE_REFRESH_INTERVAL_MS = 20_000
const AUTH_REFRESH_INTERVAL_MS = 10 * 60_000
const REQUEST_TIMEOUT_MS = 8_000
const MIN_NOTIONAL = 7.0
const REANCHOR_DRAFT_KEY = 'gridmaster.reanchorDraft.v1'
const RESET_LOCK_MESSAGE = 'Safety Lock: Queue a new Re-anchor Grid in Settings to unlock.'

function loadReanchorDraft(environment) {
  const empty = { centerPrice: '', ceilingPrice: '', stopLossPrice: '', allocatedCapital: '', gridLevels: '' }
  if (!spotFeedFor(environment)) return empty
  try {
    const saved = JSON.parse(window.localStorage.getItem(`${REANCHOR_DRAFT_KEY}.${environment}`))
    if (!saved || typeof saved !== 'object') return empty
    return Object.fromEntries(
      Object.keys(empty).map((key) => [key, typeof saved[key] === 'string' ? saved[key] : '']),
    )
  } catch {
    return empty
  }
}

function formatPrice(value) {
  if (value == null) return '—'
  const number = Number(value)
  return Number.isFinite(number)
    ? `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(number)} USDT`
    : '—'
}

function formatUsdtTwoDecimals(value) {
  if (value == null) return '—'
  const number = Number(value)
  return Number.isFinite(number)
    ? `${new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(number)} USDT`
    : '—'
}

function formatBtc(value) {
  const number = Number(value)
  return value == null || !Number.isFinite(number)
    ? '—'
    : `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 }).format(number)} BTC`
}

function formatCostPerBtc(value) {
  const formatted = formatPrice(value)
  return formatted === '—' ? formatted : `${formatted}/BTC`
}

function formatPnl(value) {
  const number = Number(value)
  return value == null || !Number.isFinite(number)
    ? '—'
    : `${number > 0 ? '+' : ''}${new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(number)} USDT`
}

function formatExchangeAuditTime(value) {
  if (!value) return 'Not yet verified'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Unavailable' : formatDateTime(date)
}

function formatLedgerNumber(value, decimals) {
  if (value == null) return '—'
  const number = Number(value)
  return Number.isFinite(number)
    ? new Intl.NumberFormat('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(number)
    : '—'
}

function formatOrderDistance(orderPrice, marketPrice) {
  const price = Number(orderPrice)
  if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(marketPrice) || marketPrice <= 0) return '—'
  const percent = Number((((price - marketPrice) / marketPrice) * 100).toFixed(2))
  return `${percent > 0 ? '+' : ''}${percent.toFixed(2)}%`
}

function compareOrderPriceDescending(left, right) {
  const leftPrice = Number(left.price)
  const rightPrice = Number(right.price)
  return (Number.isFinite(rightPrice) ? rightPrice : -Infinity)
    - (Number.isFinite(leftPrice) ? leftPrice : -Infinity)
}

function parseDecimalInput(value, allowGrouping = false) {
  const text = value.trim()
  const pattern = allowGrouping
    ? /^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$/
    : /^\d+(?:\.\d+)?$/
  return pattern.test(text) ? Number(text.replaceAll(',', '')) : NaN
}

// Place inside a relative heading row so the tooltip fits the drawer width.
function HelpTooltip({ id, label, children }) {
  const [open, setOpen] = useState(false)
  return (
    <span className="inline-flex" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button type="button" aria-label={`${label} help`} aria-describedby={open ? id : undefined}
        onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onClick={() => setOpen(true)}
        onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); setOpen(false) } }}
        className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-[11px] font-medium text-slate-400 transition hover:bg-white/5 hover:text-slate-200 focus-visible:outline focus-visible:outline-1 focus-visible:outline-slate-500">
        ?
      </button>
      {open && <span id={id} role="tooltip" className="absolute inset-x-0 top-full z-30 mt-2 rounded-md border border-white/5 bg-slate-800 p-3 text-xs font-normal normal-case leading-5 tracking-normal text-slate-300 shadow-xl">{children}</span>}
    </span>
  )
}

function WarningBanner({ children }) {
  return (
    <div role="status" className="flex items-start gap-2.5 rounded-md border border-amber-500/30 bg-transparent px-3 py-2.5 text-[11px] leading-5 text-amber-200/80">
      <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500/80">
        <path d="m10 2 8 15H2L10 2Z" strokeLinejoin="round" /><path d="M10 7v4m0 2v.5" strokeLinecap="round" />
      </svg>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

function GridMetric({ label, children, tone = 'text-slate-200' }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-2">
      <dt className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</dt>
      <dd className={`ml-auto text-right font-mono text-xs tabular-nums ${tone}`}>{children}</dd>
    </div>
  )
}

function TerminalMetric({ label, children, tone = 'text-slate-200', description }) {
  return (
    <div className="min-w-0" title={description}>
      <dt className="min-w-0 whitespace-normal text-xs font-semibold uppercase tracking-wider text-slate-400 [overflow-wrap:anywhere]">{label}</dt>
      <dd className={`mt-1 min-w-0 whitespace-normal font-mono text-sm leading-5 tabular-nums [overflow-wrap:anywhere] ${tone}`}>{children}{description && <span className="sr-only"> {description}</span>}</dd>
    </div>
  )
}

function AllocationSummary({ exposure }) {
  return (
    <details className="relative min-w-0 w-full text-xs sm:w-auto" aria-label="Total bot exposure">
      <summary className={`flex cursor-pointer list-none flex-wrap items-center gap-x-2 gap-y-1 rounded px-1 py-1 marker:content-none hover:bg-white/5 focus-visible:outline focus-visible:outline-1 focus-visible:outline-slate-400 ${exposure.exceeds_allocation ? 'text-amber-300' : 'text-slate-300'}`}>
        <span className="font-semibold uppercase tracking-wider text-slate-400">Budget</span>
        <span className="font-mono tabular-nums">{formatLedgerNumber(exposure.potential_cost_quote, 2)} / {formatPrice(exposure.configured_allocation_quote)}</span>
        {exposure.exceeds_allocation && <span>Above allocation</span>}
        <span className="text-slate-400" aria-hidden="true">ⓘ</span>
        <span className="sr-only">Show cost breakdown</span>
      </summary>
      <div className="absolute bottom-full right-0 z-30 mb-2 w-[28rem] max-w-[calc(100vw-3rem)] rounded-md border border-white/10 bg-slate-900 p-4 shadow-2xl shadow-black/50">
        <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-300">Allocation cost breakdown</h3>
        <dl className="grid min-w-0 grid-cols-2 gap-4">
          <TerminalMetric label="Configured allocation">{formatPrice(exposure.configured_allocation_quote)}</TerminalMetric>
          <TerminalMetric label="Carried BTC cost">{formatPrice(exposure.carried_cost_quote)}</TerminalMetric>
          <TerminalMetric label="Open BUY commitments">{formatPrice(exposure.open_buy_commitment_quote)}</TerminalMetric>
          <TerminalMetric label="Position + BUY cost" tone={exposure.exceeds_allocation ? 'text-amber-300' : 'text-slate-200'}>{formatPrice(exposure.potential_cost_quote)}</TerminalMetric>
        </dl>
        <p className="mt-3 leading-5 text-slate-300">{exposure.allocation_is_total_cap ? 'Total cost cap active.' : 'Existing campaign uses legacy sizing. New grids enforce a total cost cap including carried BTC.'}</p>
        <p className="mt-2 leading-5 text-slate-400">Purchase cost + remaining BUY orders, not current market value. Excludes future fees, price appreciation and reserved wallet BTC.</p>
      </div>
    </details>
  )
}

// The wallet metric provides the positioning context so this tooltip stays in
// the viewport on mobile and is never clipped by the scrolling ticker strip.
function DustReserveInfo({ quantity }) {
  const [open, setOpen] = useState(false)
  return (
    <span className="inline-flex" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button type="button" aria-label="Unmonitored BTC dust reserve" aria-describedby={open ? 'wallet-dust-info' : undefined}
        onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onClick={() => setOpen(true)}
        onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); setOpen(false) } }}
        className="inline-flex h-5 w-5 items-center justify-center rounded text-amber-500/80 transition hover:text-amber-300 focus-visible:outline focus-visible:outline-1 focus-visible:outline-amber-500/50">
        <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-3.5 w-3.5">
          <path d="m10 2 8 15H2L10 2Z" strokeLinejoin="round" /><path d="M10 7v4m0 2v.5" strokeLinecap="round" />
        </svg>
      </button>
      {open && <span className="absolute left-0 top-full z-50 w-64 max-w-[calc(100vw-2rem)] pt-2 lg:left-auto lg:right-0">
        <span id="wallet-dust-info" role="tooltip" className="block rounded-md border border-amber-500/30 bg-slate-900 p-3 text-left text-[11px] font-normal normal-case leading-5 tracking-normal text-slate-300 shadow-xl">
          Unmonitored dust reserve: <span className="font-mono text-amber-300">{formatBtc(quantity)}</span> remains in Spot, with its cost recorded. It is excluded from this grid and its stop monitoring.
        </span>
      </span>}
    </span>
  )
}

function OrderTable({ orders, marketPrice, livePrice, caption, fills = false }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[300px] border-collapse text-right">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-slate-900/60 text-xs font-semibold uppercase tracking-wider text-slate-400">
          <tr>
            <th scope="col" className="py-2 pl-3 pr-1 text-left">Price / USDT</th>
            <th scope="col" className="px-1 py-2">Size / BTC</th>
            <th scope="col" className="px-1 py-2" title={fills ? undefined : 'Distance from the engine mark price shown above, at the snapshot time.'}>{fills ? 'Side' : 'Δ Mkt'}</th>
            <th scope="col" className="py-2 pl-1 pr-3">Time</th>
          </tr>
        </thead>
        <tbody className="font-mono text-sm tabular-nums text-slate-300">
          {orders.map((order) => <tr key={order.id} title={`${order.side} · ${order.status.replaceAll('_', ' ')} · ${formatDateTime(order.timestamp)}`}
            className="even:bg-white/[0.025] hover:bg-white/5">
            <td className={`whitespace-nowrap py-1.5 pl-3 pr-1 text-left ${order.side === 'BUY' ? 'text-emerald-300' : 'text-red-300'}`}
              title={fills && order.price_source === 'order' ? 'Recorded order price; exact fill price unavailable' : undefined}>{formatLedgerNumber(order.price, 2)}</td>
            <td className="whitespace-nowrap px-1 py-1.5 text-slate-200">{formatLedgerNumber(order.amount, 8)}</td>
            <td className="whitespace-nowrap px-1 py-1.5">{fills ? <span className={order.side === 'BUY' ? 'text-emerald-300' : 'text-red-300'}>{order.side}</span> : formatOrderDistance(order.price, marketPrice)}</td>
            <td className="whitespace-nowrap py-1.5 pl-1 pr-3 text-xs text-slate-400">
              <time title={formatDateTime(order.timestamp)}>{formatTime(order.timestamp)}</time>
              {order.status !== (fills ? 'FILLED' : 'OPEN') && <span className="block text-[11px] text-amber-300">{order.status.replaceAll('_', ' ')}</span>}
              {!fills && orderNeedsVerification(order, livePrice) && <span className="block text-[11px] text-amber-300">Awaiting verification</span>}
            </td>
          </tr>)}
        </tbody>
      </table>
    </div>
  )
}

function LiveTicker({ environment, onPriceChange }) {
  const [price, setPrice] = useState(null)
  const [connected, setConnected] = useState(false)
  const [stale, setStale] = useState(false)
  const [direction, setDirection] = useState(null)
  const previousPrice = useRef(null)

  useEffect(() => {
    const feed = spotFeedFor(environment)
    if (!feed) return undefined
    let active = true
    let flashTimer

    const disconnect = connectBinanceStream(`${feed.websocket}/btcusdt@ticker`, {
      onMessage: (event) => {
        if (!active) return
        let ticker
        try {
          ticker = JSON.parse(event.data)
        } catch {
          return
        }
        if (ticker?.e !== '24hrTicker' || ticker?.s !== 'BTCUSDT') return
        if (!isFreshMarketEvent(ticker.E)) return false
        const nextPrice = Number(ticker.c)
        if (!Number.isFinite(nextPrice) || nextPrice <= 0) return

        const lastPrice = previousPrice.current
        previousPrice.current = nextPrice
        setPrice(nextPrice)
        onPriceChange({ price: nextPrice, environment, receivedAt: Date.now(), eventAt: ticker.E })
        setConnected(true)
        if (lastPrice !== null && nextPrice !== lastPrice) {
          setDirection(nextPrice > lastPrice ? 'up' : 'down')
          window.clearTimeout(flashTimer)
          flashTimer = window.setTimeout(() => {
            if (active) setDirection(null)
          }, 650)
        }
        return true
      },
      onStateChange: (state) => {
        if (!active) return
        setConnected(state === 'Live')
        setStale(state === 'Reconnecting')
        if (state === 'Live') return
        onPriceChange(null)
        previousPrice.current = null
        setDirection(null)
        window.clearTimeout(flashTimer)
      },
    })

    return () => {
      active = false
      window.clearTimeout(flashTimer)
      disconnect()
    }
  }, [environment, onPriceChange])

  const priceColor = !connected ? 'text-slate-400'
    : direction === 'up' ? 'text-emerald-400'
      : direction === 'down' ? 'text-red-400' : 'text-white'
  const priceTransition = direction ? 'duration-150' : 'duration-700'

  return (
    <div className="shrink-0" aria-label={`Binance Spot ${environment || 'unknown environment'} BTC/USDT price`}>
      <div className="flex items-center gap-1.5 whitespace-nowrap text-xs font-semibold uppercase tracking-wider text-slate-400">
        <span>BTC/USDT · {environment || 'UNKNOWN'}</span>
        <span className={`h-1.5 w-1.5 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-400'}`} aria-hidden="true" />
        <span>{connected && price !== null ? 'Live' : price === null ? 'Connecting' : 'Reconnecting'}</span>
      </div>
      <p className={`mt-1 whitespace-nowrap font-mono text-sm font-semibold tabular-nums transition-colors ${priceTransition} ${priceColor}`}>
        {formatUsdtTwoDecimals(price)}
      </p>
      {stale && <p className="text-[10px] font-semibold text-amber-400" role="status" aria-live="polite">Stale Data / Reconnecting...</p>}
    </div>
  )
}

function App() {
  const [bot, setBot] = useState(null)
  const confirmedEnvironmentRef = useRef(null)
  const tradingEnvironment = spotFeedFor(bot?.trading_environment) ? bot.trading_environment : null
  const [liveMarketQuote, setLiveMarketQuote] = useState(null)
  const liveMarketPrice = freshCenterPrice(liveMarketQuote, tradingEnvironment)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState(null)
  const [liveOrders, setLiveOrders] = useState(null)
  const [ordersLoading, setOrdersLoading] = useState(true)
  const [ordersError, setOrdersError] = useState('')
  const [ordersUpdatedAt, setOrdersUpdatedAt] = useState(null)
  const [reconciling, setReconciling] = useState(false)
  const snapshotRefreshRef = useRef(null)
  const [balanceLoading, setBalanceLoading] = useState(false)
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [authChecking, setAuthChecking] = useState(true)
  const [pauseBusy, setPauseBusy] = useState(false)
  const [engineBusy, setEngineBusy] = useState(false)
  const [engineError, setEngineError] = useState('')
  const [engineNeedsReanchor, setEngineNeedsReanchor] = useState(false)
  const [autoCoverInventory, setAutoCoverInventory] = useState(false)
  const [pendingStartGrid, setPendingStartGrid] = useState(null)
  const [isStopEngineModalOpen, setIsStopEngineModalOpen] = useState(false)
  const [isPanicModalOpen, setIsPanicModalOpen] = useState(false)
  const [panicBusy, setPanicBusy] = useState(false)
  const [panicAwaitingConfirmation, setPanicAwaitingConfirmation] = useState(false)
  const [panicError, setPanicError] = useState('')
  const [panicMessage, setPanicMessage] = useState('')
  const [dustConfirmation, setDustConfirmation] = useState(null)
  const [dustBusy, setDustBusy] = useState(false)
  const [dustError, setDustError] = useState('')
  const dustModalRef = useRef(null)
  const dustTriggerRef = useRef(null)
  const panicInFlightRef = useRef(false)
  const panicBaselineRef = useRef(null)
  const panicResolutionRef = useRef(null)
  const panicTriggerRef = useRef(null)
  const panicModalRef = useRef(null)
  const panicFeedbackRef = useRef(null)
  const [controlError, setControlError] = useState('')
  const [hardStopInput, setHardStopInput] = useState('')
  const [hardStopBusy, setHardStopBusy] = useState(false)
  const [hardStopMessage, setHardStopMessage] = useState('')
  const [hardStopError, setHardStopError] = useState('')
  const [resetBusy, setResetBusy] = useState(false)
  const [clearOrdersBusy, setClearOrdersBusy] = useState(false)
  const [clearOrdersError, setClearOrdersError] = useState('')
  const [clearOrdersMessage, setClearOrdersMessage] = useState('')
  const [isClearOrdersModalOpen, setIsClearOrdersModalOpen] = useState(false)
  const [resetMessage, setResetMessage] = useState('')
  const [resetError, setResetError] = useState(null)
  const [isResetModalOpen, setIsResetModalOpen] = useState(false)
  const [isAdminDrawerOpen, setIsAdminDrawerOpen] = useState(false)
  const resetTriggerRef = useRef(null)
  const resetModalRef = useRef(null)
  const [statusRefreshKey, setStatusRefreshKey] = useState(0)
  const [initialReanchorDraft] = useState(() => loadReanchorDraft(null))
  const [draftEnvironment, setDraftEnvironment] = useState(null)
  const [centerPrice, setCenterPrice] = useState(initialReanchorDraft.centerPrice)
  const [ceilingPrice, setCeilingPrice] = useState(initialReanchorDraft.ceilingPrice)
  const [stopLossPrice, setStopLossPrice] = useState(initialReanchorDraft.stopLossPrice)
  const [allocatedCapital, setAllocatedCapital] = useState(initialReanchorDraft.allocatedCapital)
  const [availableUsdt, setAvailableUsdt] = useState(null)
  const [balanceUnavailable, setBalanceUnavailable] = useState(false)
  const [gridLevels, setGridLevels] = useState(initialReanchorDraft.gridLevels)
  const [recenterBusy, setRecenterBusy] = useState(false)
  const [recenterMessage, setRecenterMessage] = useState('')
  const [recenterFailed, setRecenterFailed] = useState(false)
  const [gridCalculationFeedback, setGridCalculationFeedback] = useState(null)
  const recenterFeedbackRef = useRef(null)
  const [showLogin, setShowLogin] = useState(false)
  const [password, setPassword] = useState('')
  const [loginError, setLoginError] = useState('')
  const [loggingIn, setLoggingIn] = useState(false)
  const [lockingSession, setLockingSession] = useState(false)

  useEffect(() => {
    if (recenterMessage) recenterFeedbackRef.current?.scrollIntoView({ block: 'nearest' })
  }, [recenterMessage])

  useEffect(() => {
    if (isPanicModalOpen && panicBusy) panicModalRef.current?.focus()
  }, [isPanicModalOpen, panicBusy])

  useEffect(() => {
    if (!panicAwaitingConfirmation) return
    const result = panicLiquidationStatus(bot, panicBaselineRef.current)
    if (result.state === 'pending') return
    panicResolutionRef.current = result
    setPanicAwaitingConfirmation(false)
    if (result.state === 'failed') {
      setPanicError(result.message)
      return
    }
    setPanicError('')
    setPanicMessage(result.message)
    setIsPanicModalOpen(false)
    setPendingStartGrid(null)
    setEngineError('')
    window.requestAnimationFrame(() => panicFeedbackRef.current?.focus())
  }, [bot, panicAwaitingConfirmation])

  useEffect(() => {
    if (!tradingEnvironment || draftEnvironment !== tradingEnvironment) return
    const draftKey = `${REANCHOR_DRAFT_KEY}.${tradingEnvironment}`
    const draft = { centerPrice, ceilingPrice, stopLossPrice, allocatedCapital, gridLevels }
    try {
      if (Object.values(draft).some((value) => value !== '')) {
        window.localStorage.setItem(draftKey, JSON.stringify(draft))
      } else {
        window.localStorage.removeItem(draftKey)
      }
    } catch {
      // Private browsing or storage restrictions should not block the form.
    }
  }, [centerPrice, ceilingPrice, stopLossPrice, allocatedCapital, gridLevels, tradingEnvironment, draftEnvironment])

  useEffect(() => {
    if (!isAuthenticated) {
      let retired = false
      queueMicrotask(() => {
        if (retired) return
        confirmedEnvironmentRef.current = null
        setBot(null)
        setLiveOrders(null)
        setLiveMarketQuote(null)
        setAvailableUsdt(null)
        setUpdatedAt(null)
        setOrdersUpdatedAt(null)
        setPendingStartGrid(null)
        setDraftEnvironment(null)
        setError('')
        setOrdersError('')
        setLoading(false)
        setOrdersLoading(false)
        setBalanceLoading(false)
      })
      return () => { retired = true }
    }

    function expireSession() {
      setIsAuthenticated(false)
      setBot(null)
      setLiveOrders(null)
      setAvailableUsdt(null)
    }


    let retryTimer
    const statusRequest = createPollingRequest({
      timeoutMs: REQUEST_TIMEOUT_MS,
      onStart: () => { window.clearTimeout(retryTimer); setLoading(true) },
      load: async (signal) => {
        const snapshot = await readDashboardSnapshot(SNAPSHOT_URL, { signal })
        const data = snapshot.bot
        if (!spotFeedFor(data?.trading_environment)) throw new Error('Invalid backend trading environment.')
        return snapshot
      },
      onSuccess: (snapshot) => {
        const data = snapshot.bot
        if (confirmedEnvironmentRef.current !== data.trading_environment) {
          confirmedEnvironmentRef.current = data.trading_environment
          // Never carry account rows, prices, or drafts into another environment.
          setLiveOrders(null)
          setOrdersError('')
          setOrdersUpdatedAt(null)
          setLiveMarketQuote(null)
          setAvailableUsdt(null)
          setBalanceUnavailable(false)
          const draft = loadReanchorDraft(data.trading_environment)
          setCenterPrice(draft.centerPrice)
          setCeilingPrice(draft.ceilingPrice)
          setStopLossPrice(draft.stopLossPrice)
          setAllocatedCapital(draft.allocatedCapital)
          setGridLevels(draft.gridLevels)
          setDraftEnvironment(data.trading_environment)
          setPendingStartGrid(null)
        }
        // React batches this single response: portfolio, mark and rows share an ID.
        setBot({ ...data, ...snapshot.control_state, dashboard_snapshot: { id: snapshot.snapshot_id,
          captured_at: snapshot.captured_at, mark_price: snapshot.mark_price,
          mark_price_at: snapshot.mark_price_at } })
        setLiveOrders({ ...snapshot.orders, snapshot_id: snapshot.snapshot_id })
        setOrdersUpdatedAt(new Date(snapshot.captured_at))
        setOrdersError('')
        setOrdersLoading(false)
        setReconciling(snapshot.reconciling === true)
        if (snapshot.reconciling) retryTimer = window.setTimeout(() => statusRequest.run(), 1000)
        setError('')
        setUpdatedAt(new Date(snapshot.captured_at))
      },
      onError: (failure) => {
        if (failure instanceof SessionRequiredError) expireSession()
        else if (failure instanceof DashboardReconcilingError) {
          setReconciling(true)
          retryTimer = window.setTimeout(() => statusRequest.run(), 1000)
        } else {
          setError(failure.message || 'Could not refresh bot status.')
          setOrdersError('Dashboard refresh failed; showing the last complete snapshot.')
          setOrdersLoading(false)
        }
      },
      onFinish: () => setLoading(false),
    })

    function refresh() { statusRequest.run() }
    snapshotRefreshRef.current = refresh
    refresh()
    const timer = window.setInterval(refresh, REFRESH_INTERVAL_MS)
    return () => {
      window.clearInterval(timer)
      window.clearTimeout(retryTimer)
      snapshotRefreshRef.current = null
      statusRequest.dispose()
    }
  }, [statusRefreshKey, isAuthenticated])

  useEffect(() => {
    if (!isAuthenticated || !tradingEnvironment) {
      return undefined
    }
    const request = createPollingRequest({
      timeoutMs: REQUEST_TIMEOUT_MS,
      onStart: () => setBalanceLoading(true),
      load: async (signal) => {
        const response = await fetch(WALLET_BALANCE_URL, { credentials: 'include', cache: 'no-store', signal })
        if (response.status === 401) throw new SessionRequiredError()
        if (!response.ok) throw new Error('Wallet balance request failed.')
        const data = await response.json()
        if (data.available_usdt == null) throw new Error('Missing wallet balance.')
        const free = Number(data.available_usdt)
        if (!Number.isFinite(free) || free < 0) throw new Error('Invalid wallet balance.')
        return free
      },
      onSuccess: (free) => {
        if (confirmedEnvironmentRef.current !== tradingEnvironment) return
        setAvailableUsdt(free)
        setBalanceUnavailable(false)
      },
      onError: (failure) => {
        if (confirmedEnvironmentRef.current !== tradingEnvironment) return
        if (failure instanceof SessionRequiredError) {
          setAvailableUsdt(null)
          setIsAuthenticated(false)
        } else setBalanceUnavailable(true)
      },
      onFinish: () => setBalanceLoading(false),
    })
    request.run()
    const timer = window.setInterval(request.run, BALANCE_REFRESH_INTERVAL_MS)
    return () => { window.clearInterval(timer); request.dispose() }
  }, [isAuthenticated, tradingEnvironment])


  useEffect(() => {
    if (!isAuthenticated) return undefined
    let mounted = true
    async function refreshSession() {
      try {
        const response = await fetch(REFRESH_URL, {
          method: 'POST',
          credentials: 'include',
        })
        if (mounted && response.status === 401) setIsAuthenticated(false)
      } catch {
        // A temporary connection loss does not revoke the existing cookie.
      }
    }
    refreshSession()
    const timer = window.setInterval(refreshSession, AUTH_REFRESH_INTERVAL_MS)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [isAuthenticated])

  useEffect(() => {
    let mounted = true
    let currentRequest
    async function checkSession() {
      currentRequest?.abort()
      const request = new AbortController()
      currentRequest = request
      const timeout = window.setTimeout(() => request.abort(), REQUEST_TIMEOUT_MS)
      try {
        const response = await fetch(AUTH_ME_URL, { credentials: 'include', signal: request.signal })
        if (mounted && currentRequest === request) {
          if (response.ok) setIsAuthenticated(true)
          else if (response.status === 401 || response.status === 403) setIsAuthenticated(false)
        }
      } catch {
        // A transport outage does not revoke a previously verified session.
      } finally {
        window.clearTimeout(timeout)
        if (mounted && currentRequest === request) setAuthChecking(false)
      }
    }
    checkSession()
    const timer = window.setInterval(checkSession, 60_000)
    return () => { mounted = false; window.clearInterval(timer); currentRequest?.abort() }
  }, [])

  async function handleLogin(event) {
    event.preventDefault()
    setLoggingIn(true)
    setLoginError('')
    try {
      const response = await fetch(LOGIN_URL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      })
      if (response.status === 401) throw new Error('Incorrect password.')
      if (response.status === 503) throw new Error('Admin login is not configured on this bot.')
      if (!response.ok) throw new Error('Login failed. Please try again.')
      const data = await response.json()
      if (data.authenticated !== true) {
        throw new Error('The login response was invalid.')
      }
      const session = await fetch(AUTH_ME_URL, { credentials: 'include' })
      if (!session.ok) {
        throw new Error('The browser did not save the secure session. Allow cookies and use the configured HTTPS dashboard address.')
      }
      setAvailableUsdt(null)
      setBalanceUnavailable(false)
      setIsAuthenticated(true)
      setPassword('')
      setShowLogin(false)
    } catch (loginFailure) {
      setLoginError(loginFailure.message || 'Cannot reach the bot API.')
    } finally {
      setLoggingIn(false)
    }
  }

  async function lockSession() {
    if (lockingSession) return
    setLockingSession(true)
    try {
      const response = await fetch(LOGOUT_URL, { method: 'POST', credentials: 'include' })
      if (!response.ok) throw new Error('Could not lock the admin session.')
      setIsAuthenticated(false)
      setAvailableUsdt(null)
      setBalanceUnavailable(false)
      setControlError('')
    } catch (failure) {
      setControlError(failure.message || 'Could not reach the bot API to lock this session.')
    } finally {
      setLockingSession(false)
    }
  }

  async function togglePause() {
    if (!bot || pauseBusy || panicBusy) return
    setPauseBusy(true)
    setControlError('')
    try {
      const response = await fetch(PAUSE_URL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active: bot.pause_mode !== 'PAUSED_MANUAL' }),
      })
      if (response.status === 401) {
        setIsAuthenticated(false)
        throw new Error('Admin session expired. Please sign in again.')
      }
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Safety pause update failed.')
      setBot((current) => current ? { ...current, safety_pause: data.safety_pause, pause_mode: data.mode } : current)
    } catch (failure) {
      setControlError(failure.message || 'Cannot reach the bot API.')
    } finally {
      setPauseBusy(false)
    }
  }

  function openReanchorSettings() {
    setIsAdminDrawerOpen(true)
    window.requestAnimationFrame(() => {
      document.getElementById('reanchor-grid-control')?.scrollIntoView({ block: 'start' })
      document.getElementById('center-price')?.focus({ preventScroll: true })
    })
  }

  async function requestEngineStart() {
    if (!isAuthenticated || engineBusy || panicBusy) return
    setEngineBusy(true)
    setEngineError('')
    setEngineNeedsReanchor(false)
    try {
      const latest = await readBotStatus(STATUS_URL)
      if (!spotFeedFor(latest?.trading_environment)) throw new Error('Invalid backend trading environment.')
      setBot(latest)
      const recovery = engineRecovery(latest)
      if (recovery.liquidated) {
        if (recovery.canConfigureNewGrid) {
          setEngineNeedsReanchor(true)
          openReanchorSettings()
          return
        }
        throw new Error('Hard-stop recovery remains locked. Review liquidation and order-cleanup alarms before configuring a new grid.')
      }
      if (latest.pause_mode === 'PAUSED_RESET' || latest.trading_state === 'PAUSED_RESET') {
        setEngineNeedsReanchor(true)
        openReanchorSettings()
        throw new Error(RESET_LOCK_MESSAGE)
      }
      if (latest.pending_grid) {
        setPendingStartGrid(latest.pending_grid)
        return
      }
      if (!latest.has_grid_run) {
        setEngineNeedsReanchor(true)
        openReanchorSettings()
        throw new Error('Save your grid settings in Settings, then review and START the saved grid.')
      }
      await changeEngineStatus('start', null, true)
    } catch (failure) {
      if (failure instanceof SessionRequiredError) {
        setIsAuthenticated(false)
        setBot(null)
        setLoginError(failure.message)
        setShowLogin(true)
      }
      setEngineError(failure.message || 'Could not verify the saved grid before START.')
    } finally {
      setEngineBusy(false)
    }
  }

  async function acknowledgeDust() {
    if (!isAuthenticated || !dustConfirmation || dustBusy) return
    setDustBusy(true)
    setDustError('')
    try {
      const response = await fetch(ACKNOWLEDGE_DUST_URL, {
        method: 'POST', credentials: 'include', signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true, recovery_token: dustConfirmation.recovery_token }),
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : `Dust recovery failed (${response.status}).`)
      setDustConfirmation(null)
      setBot(await readBotStatus(STATUS_URL))
      setStatusRefreshKey((value) => value + 1)
      openReanchorSettings()
    } catch (failure) {
      setDustError(failure.name === 'TimeoutError'
        ? 'Recovery response timed out. Refresh status before trying again; nothing will be submitted automatically.'
        : failure.message || 'Could not verify dust recovery. Refresh status.')
    } finally {
      setDustBusy(false)
    }
  }

  function closeDustModal() {
    if (dustBusy) return
    setDustConfirmation(null)
    window.requestAnimationFrame(() => dustTriggerRef.current?.focus())
  }

  function handleDustModalKeyDown(event) {
    if (event.key === 'Escape') { event.stopPropagation(); closeDustModal() }
    if (event.key !== 'Tab') return
    const buttons = [...dustModalRef.current.querySelectorAll('button:not(:disabled)')]
    if (!buttons.length) { event.preventDefault(); return }
    const first = buttons[0], last = buttons[buttons.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }

  async function changeEngineStatus(action, pendingGridToken = null, preflightComplete = false) {
    if (panicBusy) return
    if (!isAuthenticated || (!bot && !preflightComplete) || (engineBusy && !preflightComplete)) return
    setEngineBusy(true)
    setEngineError('')
    setEngineNeedsReanchor(false)
    try {
      const response = await fetch(action === 'start' ? ENGINE_START_URL : ENGINE_STOP_URL, {
        method: 'POST', credentials: 'include',
        ...(action === 'stop' ? { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) } : {}),
        ...(action === 'start' ? {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ auto_cover_inventory: autoCoverInventory,
            ...(pendingGridToken ? { pending_grid_token: pendingGridToken } : {}) }),
        } : {}),
      })
      if (response.status === 401) {
        setIsAuthenticated(false)
        throw new Error('Admin session expired. Please sign in again.')
      }
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        if (action === 'start') {
          const detailMessage = typeof data?.detail === 'string' ? data.detail : data?.detail?.message
          const failure = detailMessage?.includes('Cannot start while safety mode is PAUSED_RESET')
            ? { message: RESET_LOCK_MESSAGE, reanchor: true }
            : engineStartError(data?.detail, response.status)
          setEngineNeedsReanchor(failure.reanchor)
          throw new Error(failure.message)
        }
        throw new Error(data?.detail || `Engine ${action} failed (${response.status}).`)
      }
      setBot((current) => current ? {
        ...current, engine_status: data.engine_status,
        trading_state: action === 'stop' && data.engine_status === 'IDLE' ? 'IDLE' : current.trading_state,
      } : current)
      setIsStopEngineModalOpen(false)
      setBot(await readBotStatus(STATUS_URL))
      setPendingStartGrid(null)
      setStatusRefreshKey((value) => value + 1)
    } catch (failure) {
      setEngineError(failure.name === 'TimeoutError'
        ? 'STOP response timed out. Refresh status to check whether the engine halted before trying again.'
        : failure.message || 'Cannot reach the trading engine.')
    } finally {
      setEngineBusy(false)
    }
  }

  async function handleHardStopUpdate(event) {
    event.preventDefault()
    if (!bot || hardStopBusy || hardStopLocked || panicBusy) return
    const price = parseDecimalInput(hardStopInput, true)
    const currentStop = Number(bot.current_hard_stop_loss)
    if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(currentStop) || price <= currentStop) {
      setHardStopError('Enter a stop above the current hard stop.')
      return
    }
    setHardStopBusy(true)
    setHardStopError('')
    setHardStopMessage('')
    try {
      const response = await fetch(STOP_LOSS_URL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ price }),
      })
      if (response.status === 401) {
        setIsAuthenticated(false)
        throw new Error('Admin session expired. Please sign in again.')
      }
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Hard stop update failed.')
      setBot((current) => current ? { ...current, current_hard_stop_loss: data.current_hard_stop_loss } : current)
      setHardStopInput('')
      setHardStopMessage(`Hard stop raised to ${formatUsdtTwoDecimals(data.current_hard_stop_loss)}. Existing grid orders remain in place.`)
      setStatusRefreshKey((value) => value + 1)
    } catch (failure) {
      setHardStopError(failure.message || 'Cannot reach the bot API.')
    } finally {
      setHardStopBusy(false)
    }
  }

  async function handleFactoryReset() {
    if (!isResetModalOpen || !isAuthenticated || !bot || resetBusy || panicBusy) return
    setResetBusy(true)
    setResetError(null)
    try {
      const response = await fetch(FACTORY_RESET_URL, {
        method: 'POST', credentials: 'include',
      })
      if (response.status === 401) {
        setIsAuthenticated(false)
        throw new Error('Admin session expired. Please sign in again.')
      }
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : null
        throw new Error(detail || `Database reset was rejected (${response.status}).`)
      }
      setResetMessage('Trade history cleared. Bot is halted until you start a new grid.')
      setResetError(null)
      setIsResetModalOpen(false)
      window.requestAnimationFrame(() => resetTriggerRef.current?.focus())
      setStatusRefreshKey((value) => value + 1)
    } catch (failure) {
      setResetError(failure.message || 'Could not reach the bot API.')
    } finally {
      setResetBusy(false)
    }
  }

  async function handleClearBotOrders() {
    if (!isClearOrdersModalOpen || !isAuthenticated || !bot || clearOrdersBusy || panicBusy) return
    setClearOrdersBusy(true)
    setClearOrdersError('')
    try {
      const response = await fetch(PREPARE_RESET_URL, {
        method: 'POST', credentials: 'include',
      })
      if (response.status === 401) {
        setIsAuthenticated(false)
        throw new Error('Admin session expired. Please sign in again.')
      }
      const data = await response.json().catch(() => null)
      if (!response.ok) throw new Error(data?.detail || `Order cleanup failed (${response.status}).`)
      setBot((current) => current ? {
        ...current, safety_pause: data.safety_pause, pause_mode: data.mode,
        trading_state: data.mode,
      } : current)
      const remaining = Number(data.remaining_exchange_orders) || 0
      const held = Number(data.bot_btc_held) || 0
      setClearOrdersMessage(data.factory_reset_ready
        ? 'Bot orders cleared and reconciled. Database reset is ready.'
        : `Bot orders cleared. ${remaining} other BTC/USDT order(s) and ${formatBtc(held)} bot-tracked BTC remain. Database reset requires both to be zero.`)
      setIsClearOrdersModalOpen(false)
      setStatusRefreshKey((value) => value + 1)
    } catch (failure) {
      setClearOrdersError(failure.message || 'Could not reach the bot API.')
    } finally {
      setClearOrdersBusy(false)
    }
  }

  function openPanicModal() {
    if (panicUnavailable) return
    setPanicError('')
    setPanicMessage('')
    setIsStopEngineModalOpen(false)
    setIsResetModalOpen(false)
    setIsClearOrdersModalOpen(false)
    setPendingStartGrid(null)
    setIsPanicModalOpen(true)
  }

  function closePanicModal() {
    if (panicInFlightRef.current) return
    setIsPanicModalOpen(false)
    setPanicError('')
    window.requestAnimationFrame(() => panicTriggerRef.current?.focus())
  }

  function handlePanicModalKeyDown(event) {
    if (event.key === 'Escape' && !panicBusy) closePanicModal()
    if (event.key !== 'Tab') return
    const buttons = [...panicModalRef.current.querySelectorAll('button:not(:disabled)')]
    if (!buttons.length) {
      event.preventDefault()
      panicModalRef.current.focus()
      return
    }
    const target = event.shiftKey ? buttons.at(-1) : buttons[0]
    if (document.activeElement === panicModalRef.current
        || (event.shiftKey && document.activeElement === buttons[0])
        || (!event.shiftKey && document.activeElement === buttons.at(-1))) {
      event.preventDefault()
      target.focus()
    }
  }

  async function handlePanicLiquidate() {
    if (!isPanicModalOpen || !isAuthenticated || panicUnavailable || panicInFlightRef.current) return
    panicInFlightRef.current = true
    panicBaselineRef.current = bot
    panicResolutionRef.current = null
    setPanicAwaitingConfirmation(true)
    setPanicBusy(true)
    setPanicError('')
    setStatusRefreshKey((value) => value + 1)
    try {
      const result = await requestPanicLiquidation(PANIC_LIQUIDATE_URL, { confirmed: true })
      setBot((current) => current ? {
        ...current, engine_status: result.engine_status, pause_mode: result.mode,
        trading_state: result.mode, safety_pause: 'Active',
      } : current)
      panicResolutionRef.current = { state: 'complete', message: PANIC_COMPLETE_MESSAGE }
      setPanicAwaitingConfirmation(false)
      setPanicError('')
      setPanicMessage(PANIC_COMPLETE_MESSAGE)
      setIsPanicModalOpen(false)
      setPendingStartGrid(null)
      setEngineError('')
      window.requestAnimationFrame(() => panicFeedbackRef.current?.focus())
    } catch (failure) {
      if (failure instanceof SessionRequiredError) setIsAuthenticated(false)
      // A status poll may have already verified success while the POST timed out.
      setPanicError(panicFailureMessage(failure, panicResolutionRef.current))
      if (failure.requestRejected || failure instanceof SessionRequiredError) setPanicAwaitingConfirmation(false)
    } finally {
      panicInFlightRef.current = false
      setPanicBusy(false)
      setStatusRefreshKey((value) => value + 1)
    }
  }

  function closeResetModal() {
    if (resetBusy) return
    setIsResetModalOpen(false)
    setResetError(null)
    window.requestAnimationFrame(() => resetTriggerRef.current?.focus())
  }

  function handleResetModalKeyDown(event) {
    if (event.key === 'Escape' && !resetBusy) {
      closeResetModal()
    } else if (event.key === 'Tab') {
      const buttons = [...resetModalRef.current.querySelectorAll('button:not(:disabled)')]
      const first = buttons[0]
      const last = buttons[buttons.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
  }

  async function handleRecenter(event) {
    event.preventDefault()
    if (recenterBusy || gridConfigurationLocked) return
    setRecenterFailed(true)
    const center = parseDecimalInput(centerPrice, true)
    const ceiling = parseDecimalInput(ceilingPrice, true)
    const stopLoss = fixedGridStop(center, ceiling, activeHardStop)
    const capital = parseDecimalInput(allocatedCapital, true)
    const levels = parseDecimalInput(gridLevels)
    if (exceedsTotalCap) {
      setRecenterMessage('Total allocation must cover existing bot BTC cost plus the new BUY budget. Review the exposure estimate below.')
      return
    }
    if (![center, ceiling, stopLoss].every((value) => Number.isFinite(value) && value > 0)) {
      setRecenterMessage('Enter valid positive prices in all three fields.')
      return
    }
    if (!(ceiling > center && center > stopLoss)) {
      setRecenterMessage('Prices must satisfy Ceiling > Center > Stop Loss.')
      return
    }
    const validation = validateSafeGridBounds({ center, ceiling, stop: stopLoss,
      activeStop: activeHardStop, requireActiveStop })
    if (!validation.valid) {
      setRecenterMessage(validation.message)
      return
    }
    if (!Number.isFinite(capital) || capital <= 0 || !Number.isSafeInteger(levels) || levels < 2 || levels > 100) {
      setRecenterMessage('Enter valid allocated capital and 2–100 total grid levels.')
      return
    }
    const requiredCapital = levels * MIN_NOTIONAL * 1.15
    const spacing = validateGridSpacing({ center, ceiling, levels })
    if (!spacing.valid) {
      setRecenterMessage(spacing.message)
      return
    }
    if (capital < requiredCapital) {
      setRecenterMessage(`Minimum investment required for ${levels} levels is ${formatUsdtTwoDecimals(requiredCapital)}.`)
      return
    }
    const widthPercentage = ((ceiling - center) / center) * 100
    const stopLossPercentage = ((center - stopLoss) / center) * 100
    setRecenterBusy(true)
    setGridCalculationFeedback(null)
    setRecenterMessage('')
    try {
      const response = await fetch(RECENTER_URL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ center_price: center, width_percentage: widthPercentage, stop_loss_percentage: stopLossPercentage, allocated_capital: capital, grid_levels: levels }),
      })
      if (response.status === 401) {
        setIsAuthenticated(false)
        throw new Error('Admin session expired. Please sign in again.')
      }
      const data = await response.json()
      if (!response.ok) {
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Grid re-anchor was rejected.')
      }
      if (engineStatus === 'IDLE') {
        setBot((current) => current ? { ...current, has_grid_run: true } : current)
      }
      setEngineError('')
      setEngineNeedsReanchor(false)
      setRecenterFailed(false)
      setRecenterMessage(`${!engineRunning ? 'Grid settings saved. Review and START the saved grid to trade' : 'Reset queued'}. Target bounds: ${formatPrice(data.lower_bound)} to ${formatPrice(data.upper_bound)}. Hard stop: ${formatPrice(stopLoss)}.`)
      setStatusRefreshKey((value) => value + 1)
      setCenterPrice('')
      setCeilingPrice('')
      setStopLossPrice('')
      setAllocatedCapital('')
      setGridLevels('')
    } catch (failure) {
      setRecenterMessage(failure.message || 'Could not reach the bot API.')
    } finally {
      setRecenterBusy(false)
    }
  }

  const hasSnapshot = isAuthenticated && bot !== null
  const isOnline = hasSnapshot && bot.status === 'Online' && !error
  const isMonitorOnly = bot?.trading_state === 'STOPPED'
  const isStandby = !isMonitorOnly && (bot?.has_grid_run === false
    || (bot?.has_grid_run == null && bot?.trading_state === 'IDLE'))
  const isIdle = isMonitorOnly || isStandby
  const engineStatus = bot?.engine_status ?? (isStandby || isMonitorOnly ? 'IDLE' : 'RUNNING')
  const engineRunning = hasSnapshot && bot.status === 'Online' && engineStatus === 'RUNNING'
  const showUnprotectedExposure = hasSnapshot && ['IDLE', 'HALTED'].includes(engineStatus)
    && bot?.has_unprotected_exposure === true
  const engineFault = hasSnapshot && !!bot?.engine_fault
  const atrAvailable = hasSnapshot && bot?.atr_value != null && bot?.atr_percentage != null
    && Number.isFinite(Number(bot.atr_value)) && Number.isFinite(Number(bot.atr_percentage))
  const imbalanceRatio = Number(bot?.imbalance_ratio)
  const imbalanceAvailable = hasSnapshot && bot?.bid_volume != null && bot?.ask_volume != null
    && bot?.imbalance_ratio != null && Number.isFinite(imbalanceRatio) && imbalanceRatio > 0
    && Number.isFinite(Number(bot.bid_volume)) && Number.isFinite(Number(bot.ask_volume))
  const pressureLabel = !imbalanceAvailable ? 'Order book: unavailable'
    : imbalanceRatio > 1.2 ? `Buy Pressure: ${imbalanceRatio.toFixed(2)}x (Bids heavier)`
      : imbalanceRatio < 0.8 ? `Sell Pressure: ${(1 / imbalanceRatio).toFixed(2)}x (Asks heavier)`
        : 'Balanced (Neutral)'
  const pressureColor = !imbalanceAvailable ? 'text-slate-400'
    : imbalanceRatio > 1.2 ? 'text-emerald-400'
      : imbalanceRatio < 0.8 ? 'text-red-400' : 'text-gray-100'
  const recovery = engineRecovery(bot)
  const dustRecovery = bot?.dust_recovery
  const dustReserveQuantity = (bot?.dust_reserves || []).filter((row) => row.status === 'UNMONITORED' && row.asset === 'BTC')
    .reduce((total, row) => total + Number(row.quantity), 0)
  const isLiquidated = recovery.liquidated
  const canConfigureNewGrid = isOnline && recovery.canConfigureNewGrid
  const gridConfigurationLocked = panicBusy || dustBusy || recovery.gridConfigurationLocked
  const panicUnavailable = !isOnline || isMonitorOnly || isStandby || panicBusy || panicAwaitingConfirmation || dustBusy
    || ['failed', 'dust_acknowledged'].includes(bot?.liquidation?.phase)
    || engineBusy || resetBusy || clearOrdersBusy || recenterBusy || hardStopBusy || pauseBusy
    || [bot?.pause_mode, bot?.trading_state].some((mode) =>
      ['LIQUIDATING', 'LIQUIDATED', 'LIQUIDATION_HALTED', 'ACCOUNT_CLEARED'].includes(mode))
  const isAccountCleared = bot?.trading_state === 'ACCOUNT_CLEARED'
  const hardStopLocked = ['LIQUIDATING', 'LIQUIDATED', 'HALTED', 'LIQUIDATION_HALTED', 'ACCOUNT_CLEARED'].includes(bot?.trading_state)
    || ['HALTED', 'LIQUIDATION_HALTED'].includes(engineStatus)
  const isSizingPaused = bot?.pause_mode === 'PAUSED_SIZING'
  const isCapExhausted = bot?.allocation_state === 'CAP_EXHAUSTED'
  const isResetPaused = bot?.pause_mode === 'PAUSED_RESET' || bot?.trading_state === 'PAUSED_RESET'
  const connectionLabel = authChecking ? 'Checking session' : !isAuthenticated ? 'Sign in required' : error ? 'Status sync unavailable' : loading && !bot ? 'Connecting' : isOnline ? (isStandby ? 'Bot Standby' : isMonitorOnly ? 'Monitor Online' : 'Online') : 'Offline'
  const connectionShortLabel = authChecking ? 'Checking' : !isAuthenticated ? 'Sign in' : error ? 'Cached' : loading && !bot ? 'Connecting' : isOnline ? 'Online' : 'Offline'
  const connectionColor = error || !isAuthenticated ? 'text-amber-400' : isOnline ? 'text-emerald-400' : 'text-red-400'
  const connectionDot = error || !isAuthenticated ? 'bg-amber-400' : isOnline ? 'bg-emerald-400' : 'bg-red-400'
  const macroFilter = bot?.macro_buy_filter
  const macroBuyPaused = engineRunning && (bot?.macro_buy_paused === true || macroFilter?.buy_allowed === false)
  const macroBuyMessage = macroFilter?.available === false ? 'BUYs paused — macro data unavailable'
    : macroFilter?.buy_allowed === false ? 'BUYs paused — daily close at or below SMA200'
      : 'BUYs paused — awaiting engine reconciliation'
  const showMacroStatus = engineRunning && !hardStopLocked && !isResetPaused && macroFilter != null
  const pauseLabel = isStandby ? 'Idle — no grid' : isMonitorOnly ? 'Bot stopped' : hasSnapshot ? (isAccountCleared ? 'Account BTC sold' : isLiquidated ? 'Liquidated — new grid required' : hardStopLocked ? 'Engine halted' : isResetPaused ? 'Orders cleared for reset' : !engineRunning ? 'Engine idle' : isSizingPaused ? 'Sizing pause' : isCapExhausted ? 'Capital exhausted — BUYs paused' : macroBuyPaused ? 'BUYs paused' : bot.safety_pause) : '—'
  const wallet = hasSnapshot ? bot.wallet : null
  const pnl = wallet?.unrealized_pnl
  const realizedPnl = hasSnapshot ? bot?.realized_pnl : null
  // Only a missing first snapshot can replace the layout. Resource refreshes
  // and mutation flags never participate in this cold-start gate.
  const initialLoading = bot === null && (authChecking || (isAuthenticated && !error))
  const syncing = hasSnapshot && (loading || ordersLoading || balanceLoading || reconciling)
  const openLimits = liveOrders?.open_limits ?? []
  const openSellOrders = openLimits.filter((order) => order.side === 'SELL').sort(compareOrderPriceDescending)
  const openBuyOrders = openLimits.filter((order) => order.side === 'BUY').sort(compareOrderPriceDescending)
  const recentFills = liveOrders?.filled_trades ?? []
  const snapshotInfo = bot?.dashboard_snapshot
  const snapshotAligned = snapshotInfo?.id != null && snapshotInfo.id === liveOrders?.snapshot_id
  const snapshotPrice = snapshotAligned && snapshotInfo?.mark_price != null ? Number(snapshotInfo.mark_price) : null
  const crossedOrderKey = openLimits.filter((order) => orderNeedsVerification(order, liveMarketPrice)).map((order) => order.id).sort().join(',')
  const exposure = bot?.exposure
  useEffect(() => {
    if (!crossedOrderKey || !isAuthenticated) return undefined
    snapshotRefreshRef.current?.()
    const timer = window.setInterval(() => snapshotRefreshRef.current?.(), 1000)
    return () => window.clearInterval(timer)
  }, [crossedOrderKey, isAuthenticated])
  useEffect(() => {
    if (hasSnapshot && !snapshotAligned) snapshotRefreshRef.current?.()
  }, [hasSnapshot, snapshotAligned])
  const chartGridLevels = useMemo(() => (liveOrders?.open_limits ?? [])
    .filter((order) => order.status !== 'PENDING_CONFIRMATION')
    .map((order) => ({ price: Number(order.price), type: order.side })), [liveOrders])
  const previewCenter = parseDecimalInput(centerPrice, true)
  const previewCeiling = parseDecimalInput(ceilingPrice, true)
  const previewStopLoss = fixedGridStop(previewCenter, previewCeiling,
    bot?.has_grid_run && !isLiquidated ? bot?.current_hard_stop_loss : null)
  const previewCapital = parseDecimalInput(allocatedCapital, true)
  const previewLevels = parseDecimalInput(gridLevels)
  const validLevels = Number.isSafeInteger(previewLevels) && previewLevels >= 2 && previewLevels <= 100
  const validAllocation = Number.isFinite(previewCapital) && previewCapital > 0 && validLevels
  const estimatedOrderSize = validAllocation ? previewCapital / previewLevels : null
  const positionCost = exposure?.position_cost_quote
  const buyBudgetRatio = exposure?.configured_allocation_quote > 0
    ? exposure.new_buy_budget_quote / exposure.configured_allocation_quote : 0.5
  const proposedTotalCost = positionCost != null && validAllocation
    ? positionCost > 0 ? positionCost + previewCapital * buyBudgetRatio : previewCapital : null
  const exceedsTotalCap = proposedTotalCost != null && proposedTotalCost > previewCapital + 0.000001
  const requiredNewQuote = validAllocation ? positionCost > 0 ? previewCapital * buyBudgetRatio : previewCapital : null
  const allocationExceedsBalance = availableUsdt !== null && requiredNewQuote != null && requiredNewQuote > availableUsdt
  const requiredCapital = validLevels ? previewLevels * MIN_NOTIONAL * 1.15 : null
  const capitalBelowRequired = validLevels && Number.isFinite(previewCapital)
    && previewCapital < requiredCapital
  const hasPreviewCenter = Number.isFinite(previewCenter) && previewCenter > 0
  const atrValue = bot?.atr_value == null ? NaN : Number(bot.atr_value)
  const hasPreviewCeiling = Number.isFinite(previewCeiling) && previewCeiling > 0
  const hasPreviewStop = Number.isFinite(previewStopLoss) && previewStopLoss > 0
  const previewWidthPercentage = hasPreviewCenter && hasPreviewCeiling
    ? ((previewCeiling - previewCenter) / previewCenter) * 100 : null
  const projectedLowerBound = hasPreviewCenter && hasPreviewCeiling
    ? 2 * previewCenter - previewCeiling : null
  const previewStopDistance = hasPreviewCenter && hasPreviewStop
    ? ((previewCenter - previewStopLoss) / previewCenter) * 100 : null
  const requireActiveStop = !!bot?.has_grid_run && !isLiquidated
  const activeHardStop = requireActiveStop
    ? Number(bot?.current_hard_stop_loss ?? bot?.hard_stop) : null
  const safeBounds = validateSafeGridBounds({ center: previewCenter, ceiling: previewCeiling,
    stop: previewStopLoss, activeStop: activeHardStop, requireActiveStop })
  const spacingPreview = validateGridSpacing({ center: previewCenter, ceiling: previewCeiling, levels: previewLevels })
  const showBoundsError = safeBounds.lowerBoundUnsafe || safeBounds.stopBelowFloor
    || safeBounds.activeStopUnavailable || [centerPrice, ceilingPrice, stopLossPrice].every((value) => value.trim())
  const gridEditorBlockedReason = !isAuthenticated ? 'Sign in to edit the grid.'
    : !isOnline ? 'Waiting for the bot connection.'
      : panicBusy ? 'Wait for panic liquidation to finish.'
        : dustBusy ? 'Wait for dust recovery to finish.'
          : gridConfigurationLocked ? 'Complete the recovery step shown above.'
            : isMonitorOnly ? 'Grid editing is unavailable in monitor mode.'
              : isAccountCleared ? 'This grid is closed. Follow the new-grid instructions above.'
                : recenterBusy ? 'Wait for the grid settings to finish saving.' : ''
  const centerSelection = resolveGridCenter({ manualProvided: centerPrice.trim() !== '',
    manualPrice: previewCenter, quote: liveMarketQuote, environment: tradingEnvironment })
  const liveCenterDisabledReason = gridEditorBlockedReason
    || (liveMarketPrice === null ? 'Waiting for a fresh live price.' : '')
  const calculateGridDisabledReason = gridEditorBlockedReason
    || (!validLevels ? 'Enter Grid Levels as a whole number from 2 to 100.' : '')
    || (safeBounds.activeStopUnavailable ? 'Waiting for the current hard-stop data.' : '')
    || centerSelection.error
    || (!(Number.isFinite(atrValue) && atrValue > 0) ? 'Waiting for 1h ATR data.' : '')
  const canCalculateGrid = !calculateGridDisabledReason

  function useLiveCenterPrice() {
    if (gridEditorBlockedReason) return
    const price = freshCenterPrice(liveMarketQuote, tradingEnvironment)
    if (price === null) {
      setGridCalculationFeedback({ error: true, message: 'Live price is unavailable or stale. Wait for reconnection or enter Center Price manually.' })
      return
    }
    setCenterPrice(price.toFixed(2))
    setRecenterMessage('')
    setGridCalculationFeedback({ error: false, message: `Center captured from Binance ${tradingEnvironment} at ${formatTime(liveMarketQuote.receivedAt)}. Calculate the grid and review the bounds before saving. The center will stay fixed.` })
  }

  function calculateGrid() {
    if (!canCalculateGrid) return
    // Check the quote again at click time; a background tab can delay watchdogs.
    const selected = resolveGridCenter({ manualProvided: centerPrice.trim() !== '',
      manualPrice: previewCenter, quote: liveMarketQuote, environment: tradingEnvironment })
    if (selected.error) {
      setGridCalculationFeedback({ error: true, message: selected.error })
      return
    }
    const center = selected.price

    let ceiling
    try {
      ceiling = compressedAtrBounds(center, atrValue, previewLevels).ceiling
    } catch (failure) {
      setGridCalculationFeedback({ error: true, message: failure.message })
      return
    }
    // Match the lower bound projected from the rounded ceiling in this form.
    const lowerBound = 2 * center - ceiling
    const stop = Math.max(catastrophicStop(lowerBound), Number(activeHardStop) || 0)
    const validation = validateSafeGridBounds({
      center, ceiling, stop, activeStop: activeHardStop, requireActiveStop,
    })

    setRecenterMessage('')
    if (!validation.valid) {
      setGridCalculationFeedback({
        error: true,
        message: validation.stopBelowFloor
          ? 'The ATR buffer would lower the active hard stop. This run’s stop cannot be lowered; the draft was not changed.'
          : `${validation.message} The draft was not changed.`,
      })
      return
    }

    if (selected.source === 'live') setCenterPrice(center.toFixed(2))
    setCeilingPrice(ceiling.toFixed(2))
    setStopLossPrice(stop.toFixed(2))
    setGridCalculationFeedback({
      error: false,
      message: `${selected.source === 'live' ? `Live center captured at ${formatTime(liveMarketQuote.receivedAt)}.` : 'Your entered center was used.'} Draft calculated with 0.5× closed 1h ATR spacing, at least 0.4% gross per level, and a fixed stop 15% below the lower bound, retaining any higher active floor. Review before saving.`,
    })
  }

  if (initialLoading) return <DashboardSkeleton />

  return (
    <div className="flex h-dvh w-full flex-col overflow-hidden bg-slate-950 font-sans text-slate-200 antialiased">
      <header className={dashboardLayout.header} aria-label="Terminal top bar">
        <div className={dashboardLayout.ticker}>
          <div className="hidden shrink-0 2xl:block">
            <p className="text-[11px] font-semibold tracking-wide text-slate-200">GRID MASTER</p>
            <p className="mt-1 text-xs font-semibold uppercase tracking-wider text-slate-400">Spot execution</p>
          </div>
          <div className="flex min-w-0 flex-1 items-center gap-5 overflow-x-auto [scrollbar-width:thin]" aria-label="Global market and account metrics">
            <LiveTicker key={tradingEnvironment || 'unknown'} environment={tradingEnvironment} onPriceChange={setLiveMarketQuote} />
            <dl className="flex shrink-0 items-center gap-5">
              <TerminalMetric label="Unrealized P&L" tone={Number(pnl) > 0 ? 'text-emerald-400' : Number(pnl) < 0 ? 'text-red-400' : 'text-slate-200'}
                description={snapshotPrice != null ? `Calculated using engine mark ${formatLedgerNumber(snapshotPrice, 2)} USDT at ${snapshotInfo.mark_price_at ? formatTime(snapshotInfo.mark_price_at) : 'time unavailable'}. The live ticker may be newer.` : undefined}>{formatPnl(pnl)}</TerminalMetric>
              <TerminalMetric label="Realized · Total" tone={Number(realizedPnl?.total) > 0 ? 'text-emerald-400' : Number(realizedPnl?.total) < 0 ? 'text-red-400' : 'text-slate-200'}>{formatPnl(realizedPnl?.total)}</TerminalMetric>
              <TerminalMetric label="Realized · Today" tone={Number(realizedPnl?.today) > 0 ? 'text-emerald-400' : Number(realizedPnl?.today) < 0 ? 'text-red-400' : 'text-slate-200'}>{formatPnl(realizedPnl?.today)}</TerminalMetric>
            </dl>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <span role="status" aria-label={syncing ? 'Syncing dashboard data' : 'Dashboard sync idle'} title={syncing ? 'Syncing dashboard data' : 'Dashboard sync idle'} className="inline-flex h-8 w-6 items-center justify-center text-slate-400">
              {syncing && <InlineSpinner />}
            </span>
            <button type="button" onClick={() => document.getElementById('order-ledger')?.scrollIntoView({ block: 'start' })} aria-label="Jump to order ledger" aria-controls="order-ledger" className="rounded p-2 text-slate-400 hover:bg-white/5 hover:text-slate-200 focus-visible:outline focus-visible:outline-1 focus-visible:outline-slate-400 lg:hidden">☷</button>
            <button type="button" onClick={() => setIsAdminDrawerOpen(true)} aria-label="Open settings and admin controls" aria-controls="admin-drawer" aria-expanded={isAdminDrawerOpen} className="rounded p-2 text-slate-400 transition hover:bg-white/5 hover:text-slate-200 focus-visible:outline focus-visible:outline-1 focus-visible:outline-slate-400">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z" /><path strokeLinecap="round" strokeLinejoin="round" d="M19.4 15a1.8 1.8 0 0 0 .36 2l-1.8 1.8a1.8 1.8 0 0 0-2-.36l-.57.3a1.8 1.8 0 0 0-1 1.66V21h-2.8v-.6a1.8 1.8 0 0 0-1-1.66l-.57-.3a1.8 1.8 0 0 0-2 .36l-1.8-1.8a1.8 1.8 0 0 0 .36-2l-.3-.57a1.8 1.8 0 0 0-1.66-1H4v-2.8h.6a1.8 1.8 0 0 0 1.66-1l.3-.57a1.8 1.8 0 0 0-.36-2l1.8-1.8a1.8 1.8 0 0 0 2 .36l.57-.3a1.8 1.8 0 0 0 1-1.66V3h2.8v.6a1.8 1.8 0 0 0 1 1.66l.57.3a1.8 1.8 0 0 0 2-.36l1.8 1.8a1.8 1.8 0 0 0-.36 2l.3.57a1.8 1.8 0 0 0 1.66 1h.6v2.8h-.6a1.8 1.8 0 0 0-1.66 1Z" /></svg>
            </button>
          </div>
        </div>
        <div className={dashboardLayout.controls}>
          <div className="relative min-w-0" aria-label="Spot wallet">
            <div className="flex min-h-5 items-center gap-1">
              <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">Wallet · Free USDT</p>
              {hasSnapshot && dustReserveQuantity > 0 && <DustReserveInfo quantity={dustReserveQuantity} />}
            </div>
            <p className="break-words font-mono text-sm tabular-nums text-slate-100" title={balanceUnavailable ? 'Wallet refresh failed. Showing the last confirmed free USDT balance.' : 'Free USDT in the Binance Spot wallet'}>{authChecking ? 'Checking…' : !isAuthenticated ? 'Locked' : availableUsdt !== null ? formatUsdtTwoDecimals(availableUsdt) : balanceUnavailable ? 'Unavailable' : 'Loading…'}{isAuthenticated && balanceUnavailable && availableUsdt !== null && <span className="ml-1 text-[10px] text-amber-400">cached</span>}</p>
          </div>
          <div className="ml-auto flex shrink-0 flex-col gap-1" aria-label="Master engine control">
            <div className="flex items-center justify-end gap-2">
              <span className={`flex items-center gap-1 text-xs font-semibold uppercase tracking-wider ${connectionColor}`} role="status" aria-live="polite" aria-label={connectionLabel} title={connectionLabel}>
                <span className={`h-1 w-1 rounded-full ${connectionDot}`} aria-hidden="true" />
                <span>{connectionShortLabel}</span>
              </span>
              <span title={engineFault ? `Engine fault: ${bot.engine_fault}. Review the bot logs before restarting.` : undefined}
                className={`font-mono text-[10px] ${engineRunning ? 'text-emerald-400' : engineFault || recovery.badge === 'HALTED' ? 'text-red-400' : 'text-slate-400'}`}
                role="status" aria-live="polite">{recovery.badge}</span>
              <button type="button" role="switch" aria-checked={engineRunning} aria-busy={engineBusy}
                disabled={authChecking || engineBusy || panicBusy || (isAuthenticated && (!isOnline || isMonitorOnly || isAccountCleared))}
                onClick={() => {
                  if (!isAuthenticated) { setLoginError(''); setShowLogin(true) }
                  else if (engineRunning) { setEngineError(''); setIsStopEngineModalOpen(true) }
                  else requestEngineStart()
                }}
                aria-label={!isAuthenticated ? 'Sign in to manage trading' : engineRunning ? 'Stop trading engine' : canConfigureNewGrid ? 'Configure new grid after liquidation' : 'Start trading engine'}
                title={engineBusy ? 'Waiting for engine confirmation' : !isAuthenticated ? 'Sign in to manage trading' : engineRunning ? 'Review and confirm STOP' : canConfigureNewGrid ? 'Configure a new grid' : 'Review and confirm START'}
                className={`relative inline-flex h-6 w-10 items-center rounded-full transition-colors focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-2 focus-visible:outline-slate-400 disabled:cursor-not-allowed disabled:opacity-40 ${engineRunning ? 'bg-emerald-500/25 hover:bg-emerald-500/35' : 'bg-slate-700 hover:bg-slate-600'}`}>
                {engineBusy ? <InlineSpinner className="mx-auto text-slate-200" /> : <span aria-hidden="true" className={`h-4 w-4 rounded-full transition-transform ${engineRunning ? 'translate-x-5 bg-emerald-400' : 'translate-x-1 bg-slate-400'}`} />}
              </button>
            </div>
            <label className="flex cursor-pointer items-center justify-end gap-1.5 whitespace-nowrap text-[9px] text-slate-400 hover:text-slate-300">
              <input type="checkbox" checked={autoCoverInventory}
                onChange={(event) => setAutoCoverInventory(event.target.checked)}
                disabled={engineBusy || engineRunning || isMonitorOnly}
                className="h-3 w-3 cursor-pointer accent-slate-400 disabled:cursor-not-allowed" />
              Auto-cover existing BTC
            </label>
          </div>

        </div>
      </header>
      {error && <p className="flex-none border-b border-amber-500/20 px-3 py-2 text-xs text-amber-200" role="alert">{error} {hasSnapshot ? `Showing the last confirmed snapshot${updatedAt ? ` from ${formatTime(updatedAt)}` : ''}. Trading controls are disabled until status sync recovers.` : 'Waiting for the first verified snapshot; retrying automatically.'}</p>}
      {hasSnapshot && dustRecovery && <div className="z-10 flex flex-none flex-wrap items-center justify-between gap-2 border-b border-amber-500/20 px-3 py-2 text-xs text-amber-200" role="alert">
        <p>Trading halted: <span className="font-mono">{formatBtc(dustRecovery.quantity)}</span> remains below Binance market-order minimums. Review dust recovery in Settings.</p>
        <button type="button" onClick={openReanchorSettings} className="rounded border border-amber-500/30 px-2 py-1 text-[11px] font-semibold hover:bg-amber-500/5">Review Dust Recovery</button>
      </div>}
      {showUnprotectedExposure && <div className="z-10 flex-none border-b border-amber-500/20 px-3 py-2 text-xs text-amber-200" role="alert">
        Unprotected exposure: <span className="font-mono">{formatBtc(bot.unprotected_btc_exposure)}</span> held without risk monitoring. The engine is {engineStatus}; hard-stop monitoring is inactive.
      </div>}
      {hasSnapshot && isLiquidated && <div className="z-10 flex flex-none flex-wrap items-center justify-between gap-2 border-b border-white/5 bg-slate-900/50 px-3 py-2 text-xs text-slate-300" role="status">
        <p>{canConfigureNewGrid ? dustReserveQuantity > 0 ? 'Previous grid closed; the dust reserve stays recorded separately. Configure and save a new grid, then review and START.' : LIQUIDATION_RESET_MESSAGE : 'Hard-stop recovery is locked. Review liquidation and order-cleanup alarms before configuring a new grid.'}</p>
        {canConfigureNewGrid && <button type="button" onClick={openReanchorSettings} className="rounded bg-slate-700 px-2 py-1 text-[11px] font-semibold text-slate-100 hover:bg-slate-600">Configure New Grid</button>}
      </div>}
      {engineError && <div className="fixed left-1/2 top-20 z-50 max-w-[min(90vw,32rem)] -translate-x-1/2 rounded-md border border-red-500/30 bg-slate-900 px-4 py-3 text-xs text-red-300 shadow-xl" role="alert">
        <p>{engineError}</p>
        {engineNeedsReanchor && <button type="button" onClick={() => {
          setEngineError('')
          openReanchorSettings()
        }} className="mt-3 rounded bg-white/5 px-3 py-2 font-semibold hover:bg-white/10">Review grid settings</button>}
      </div>}

      <main className={dashboardLayout.workspace} aria-label="Trading workspace">
        <section id="main-stage-chart" className={dashboardLayout.chartColumn} aria-label="Main stage chart">
          {hasSnapshot && (reconciling || !snapshotAligned) && <p role="status" className="mx-3 mt-2 flex-none rounded border border-amber-500/30 px-3 py-2 text-xs text-amber-200">{reconciling ? 'Engine reconciling — showing the last complete snapshot.' : 'Updating the portfolio and order ledger together.'} Live ticker prices can move ahead of confirmed orders.</p>}
          {showMacroStatus && <details aria-label="SMA200 BUY gate" className={`relative z-20 mx-3 mt-2 flex-none rounded border text-xs ${macroBuyPaused ? 'border-amber-500/30 text-amber-200' : 'border-white/5 text-slate-300'}`}>
            <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-x-3 gap-y-1 px-3 py-1.5 marker:content-none hover:bg-white/5 focus-visible:outline focus-visible:outline-1 focus-visible:outline-slate-400">
              <span role="status" className="font-semibold">{macroBuyPaused ? macroBuyMessage : 'SMA200 BUY gate: approved'}</span>
              <span className="text-slate-400">Refresh <span className="font-mono">{macroFilter.last_success_at ? formatTime(macroFilter.last_success_at) : 'Unavailable'}</span>{macroFilter.refreshing ? ' · Refreshing' : ''} <span aria-hidden="true">ⓘ</span><span className="sr-only"> Show macro filter details</span></span>
            </summary>
            <div className="absolute inset-x-0 top-full z-30 mt-1 rounded-md border border-white/10 bg-slate-900 px-3 py-3 leading-5 shadow-xl">
              <p className="text-slate-400">Last successful refresh: <span className="font-mono">{macroFilter.last_success_at ? formatExchangeAuditTime(macroFilter.last_success_at) : 'Not yet available'}</span></p>
              {macroBuyPaused && <p className="mt-1 text-slate-300">The macro filter retains existing SELLs and hard-stop monitoring. BUYs resume only after valid trend approval and engine checks.</p>}
              {macroFilter.error && <p className="mt-1 break-words text-slate-400">Reason: {macroFilter.error}</p>}
              {macroFilter.candle_day && <p className="mt-1 text-slate-400">Completed UTC day: <span className="font-mono">{macroFilter.candle_day}</span> · Yahoo close: <span className="font-mono">{formatLedgerNumber(macroFilter.close, 2)} USD</span> · SMA200: <span className="font-mono">{formatLedgerNumber(macroFilter.sma200, 2)} USD</span></p>}
            </div>
          </details>}
          <div className={dashboardLayout.chart}><GridTerminalChart key={tradingEnvironment || 'unknown'} environment={tradingEnvironment} gridLevels={chartGridLevels} /></div>
          <section className="m-3 mt-0 min-w-0 max-w-full flex-none rounded-md border border-white/5 bg-slate-900 shadow-sm shadow-black/10" aria-label="Active grid and market context">
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-white/5 px-3 py-2">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Grid parameters</h2>
              {exposure && <AllocationSummary exposure={exposure} />}
            </div>
            <dl className={dashboardLayout.parameters} aria-label="Engine telemetry">
              <TerminalMetric label="Lower bound">{hasSnapshot ? formatPrice(bot.configured_lower_bound) : '—'}</TerminalMetric>
              <TerminalMetric label="Upper bound">{hasSnapshot ? formatPrice(bot.configured_upper_bound) : '—'}</TerminalMetric>
              <TerminalMetric label="Hard stop" tone="text-amber-400">{hasSnapshot ? formatUsdtTwoDecimals(bot.current_hard_stop_loss) : '—'}</TerminalMetric>
              <TerminalMetric label="High water">{hasSnapshot ? formatUsdtTwoDecimals(bot.high_water_mark) : '—'}</TerminalMetric>
              <TerminalMetric label="Open levels">{hasSnapshot ? bot.grid_levels ?? '—' : '—'}</TerminalMetric>
              <TerminalMetric label="Bot BTC">{formatBtc(wallet?.btc_held)}</TerminalMetric>
              <TerminalMetric label="Avg cost / BTC">{formatCostPerBtc(wallet?.average_cost)}</TerminalMetric>
              <TerminalMetric label="Protection">{pauseLabel}</TerminalMetric>
            </dl>
            <dl className={dashboardLayout.marketIntel} aria-label="Market intelligence">
              <TerminalMetric label="1h ATR">{atrAvailable ? `${formatLedgerNumber(bot.atr_value, 2)} USDT · ${formatLedgerNumber(bot.atr_percentage, 2)}%` : 'Unavailable'}</TerminalMetric>
              <TerminalMetric label="Order book pressure" tone={pressureColor}>{pressureLabel}</TerminalMetric>
              <TerminalMetric label="Top 50 · Bid / Ask BTC">{imbalanceAvailable ? `${formatLedgerNumber(bot.bid_volume, 4)} / ${formatLedgerNumber(bot.ask_volume, 4)}` : 'Unavailable'}</TerminalMetric>
            </dl>
            <p className="border-t border-white/5 px-3 py-2 text-[10px] text-slate-400" role="status" title="Ledger rows stored in SQLite. This time records the last successful reconciliation or full Binance open-order audit.">
              Ledger: SQLite · Last Binance Sync: <span className="font-mono">{formatExchangeAuditTime(bot?.last_exchange_audit_time)}</span>{snapshotInfo && <> · Snapshot: <span className="font-mono">{formatExchangeAuditTime(snapshotInfo.captured_at)}</span></>}
            </p>
          </section>
          {bot?.status === 'Offline' && <p className="absolute left-3 top-12 rounded border border-white/5 bg-slate-900/95 px-3 py-2 text-xs text-slate-300 shadow-xl" role="alert">The API is reachable, but no trading bot is attached.</p>}
          {isIdle && <p className="absolute left-3 top-12 rounded border border-white/5 bg-slate-900/95 px-3 py-2 text-xs text-slate-300 shadow-xl" role="status">{isStandby ? 'Bot standby: no active grid. Manual BTC holdings are excluded.' : 'Automated trading stopped. Manual BTC holdings are excluded.'}</p>}
        </section>

        <aside id="order-ledger" className={dashboardLayout.ledger} aria-label="Live order ledger">
          <div className="flex flex-none items-center justify-between gap-2 border-b border-white/5 bg-slate-900 px-3 py-2.5">
            <div><p className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">Bot-tracked orders</p><h2 className="mt-0.5 text-[11px] font-semibold uppercase tracking-wider text-slate-300">Live Order Ledger</h2></div>
            <button type="button" onClick={() => document.getElementById('main-stage-chart')?.scrollIntoView({ block: 'start' })} aria-label="Back to chart" className="rounded px-2 py-1 text-slate-400 hover:bg-white/5 lg:hidden">↑ Chart</button>
          </div>
          {snapshotInfo && <p className="flex-none border-b border-white/5 px-3 py-1.5 text-[11px] text-slate-400" title="Distance from the engine mark at this snapshot. Crossed rows await exchange verification; a price crossing does not confirm a fill.">Δ MKT uses engine mark <span className="font-mono">{snapshotPrice != null ? formatLedgerNumber(snapshotPrice, 2) : '—'} USDT</span> · <span className="font-mono">{snapshotInfo.mark_price_at ? formatTime(snapshotInfo.mark_price_at) : 'unknown time'}</span></p>}
          <div className={`${dashboardLayout.ledgerRows} ${liveOrders !== null && ordersLoading ? 'opacity-80' : ''}`} aria-busy={isAuthenticated && ordersLoading}>
            {!isAuthenticated ? <p className="p-3 text-xs leading-5 text-slate-400">Sign in through Settings to view open orders and recent fills.</p>
              : ordersLoading && liveOrders === null ? <div role="status" aria-label="Loading first order ledger" className="space-y-1 p-3"><Skeleton className="h-7 w-full" /><Skeleton className="h-7 w-full" /><Skeleton className="h-7 w-full" /></div>
              : ordersError && liveOrders === null ? <p className="p-3 text-xs text-red-400" role="alert">{ordersError}</p> : <>
                {ordersError && <p className="px-3 py-2 text-[11px] text-amber-300" role="alert">{ordersError} Showing last confirmed orders{ordersUpdatedAt ? ` from ${formatTime(ordersUpdatedAt)}` : ''}.</p>}
                {[
                  ['SELL limits', openSellOrders, 'text-red-400'],
                  ['BUY limits', openBuyOrders, 'text-emerald-400'],
                ].map(([heading, orders, color]) => <section key={heading} className="pb-3" aria-label={heading}>
                  <h3 className={`flex items-center justify-between px-3 py-2.5 text-[10px] font-semibold uppercase tracking-wider ${color}`}><span>{heading}</span><span className="font-mono">{orders.length}</span></h3>
                  {heading === 'BUY limits' && macroBuyPaused && <p className="px-3 pb-2 text-xs text-amber-300" role="status">{macroBuyMessage}</p>}
                  {orders.length === 0 ? <p className="px-3 py-2 text-[11px] text-slate-400">No open orders.</p> : <OrderTable orders={orders} marketPrice={snapshotPrice} livePrice={liveMarketPrice} caption={`${heading} · last engine-confirmed orders`} />}
                </section>)}
                <details className="border-t border-white/5">
                  <summary className="cursor-pointer px-3 py-3 text-[10px] font-semibold uppercase tracking-wider text-slate-400">Recent Fills · <span className="font-mono">{recentFills.length}</span></summary>
                  {recentFills.length === 0 ? <p className="px-3 py-2 text-[11px] text-slate-400">No recent fills.</p> : <OrderTable orders={recentFills} caption="Recent bot fills" fills />}
                </details>
              </>}
          </div>
          <p className="flex-none border-t border-white/5 px-3 py-2 text-[9px] text-slate-400">Ledger sync <span className="font-mono">{ordersUpdatedAt ? formatTime(ordersUpdatedAt) : '—'} · every 10s</span></p>
        </aside>
      </main>


      {isAdminDrawerOpen && <button type="button" onClick={() => setIsAdminDrawerOpen(false)} aria-label="Close admin controls" className="fixed inset-0 z-30 bg-black/60" />}
      {isAdminDrawerOpen && <aside id="admin-drawer" role="dialog" aria-modal="true" aria-label="Admin controls" className="fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col overflow-hidden bg-slate-950 shadow-2xl shadow-black/50">
          <div className="flex flex-none items-center justify-between gap-3 border-b border-white/5 px-5 py-4">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Settings / Admin</p>
            <button type="button" onClick={() => setIsAdminDrawerOpen(false)} aria-label="Close admin controls" className="rounded-lg bg-white/5 px-3 py-2 text-sm text-slate-200 hover:bg-white/10">Close ×</button>
          </div>
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain px-4 py-5 [scrollbar-color:#334155_transparent] [scrollbar-width:thin]">
          {isAuthenticated && <button type="button" onClick={lockSession} disabled={lockingSession} aria-busy={lockingSession} className="w-full rounded-lg bg-white/5 px-3 py-2 text-xs font-semibold text-slate-200 hover:bg-white/10 disabled:opacity-50"><ActionContent busy={lockingSession}>{lockingSession ? 'Locking…' : 'Lock Admin Session'}</ActionContent></button>}
          <div className="flex items-center justify-between pb-1">
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">01 / {isIdle ? 'Account' : 'Execution'}</p>
              <h1 className="mt-2 text-base font-semibold tracking-tight text-slate-100">{isStandby ? 'Bot standby' : isMonitorOnly ? 'Manual mode' : 'Controls'}</h1>
            </div>
            {authChecking ? <Skeleton className="h-6 w-20 rounded-full" /> : (
              <span className={`rounded-full bg-white/5 px-2.5 py-1 text-[10px] font-semibold tracking-wider ${isAuthenticated ? 'text-slate-300' : 'text-slate-400'}`}>
                {isAuthenticated ? 'UNLOCKED' : 'LOCKED'}
              </span>
            )}
          </div>

          {authChecking ? (
            <section className="rounded-lg border border-white/5 bg-slate-900 p-4" aria-label="Checking admin session" role="status">
              <span className="sr-only">Checking admin session</span>
              <Skeleton className="h-5 w-2/3" />
              <Skeleton className="mt-3 h-3 w-full" />
              <Skeleton className="mt-2 h-3 w-4/5" />
              <Skeleton className="mt-5 h-9 w-full rounded-lg" />
            </section>
          ) : isMonitorOnly ? (
            <section className="rounded-lg border border-white/5 bg-slate-900 p-4" aria-label="Manual trading mode">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Automated trading</p>
              <div className="relative mt-1 flex items-center gap-2">
                <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">{isStandby ? 'Bot running, grid idle' : 'Bot stopped'}</h2>
                <HelpTooltip id="monitor-mode-help" label="Manual trading mode">{isStandby ? 'The backend and market data are running. Grid trading is locked until a separate explicit activation.' : 'The dashboard provides read-only monitoring while you manage the Spot wallet yourself. The bot is not placing grid orders.'}</HelpTooltip>
              </div>
              {isAuthenticated ? (
                <p className="mt-6 font-mono text-sm font-medium tabular-nums text-slate-200">Available Spot USDT: {balanceUnavailable ? 'Unavailable' : formatUsdtTwoDecimals(availableUsdt)}</p>
              ) : (
                <button type="button" onClick={() => { setLoginError(''); setShowLogin(true) }}
                  className="mt-4 w-full rounded-md bg-slate-700 px-3 py-2.5 text-xs font-semibold text-slate-100 transition hover:bg-slate-600">
                  Login to View Balance
                </button>
              )}
            </section>
          ) : isAuthenticated ? (
            <>
              <div role="group" aria-labelledby="live-adjustments-title" className="space-y-5">
                <div className="relative flex items-center gap-2">
                  <h2 id="live-adjustments-title" className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Live Adjustments</h2>
                  <HelpTooltip id="live-adjustments-help" label="Live adjustments">Grid configuration and daily risk controls. Changes to a running grid can affect live orders.</HelpTooltip>
                </div>
              {isStandby ? (
                <section className="rounded-lg border border-white/5 bg-slate-900 p-4" aria-label="Standby account status">
                  <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Bot standby</p>
                <div className="relative mt-1 flex items-center gap-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Ready for your grid</h2>
                  <HelpTooltip id="standby-grid-help" label="Standby grid">Enter your grid settings below, then use START in the top bar when you want trading to begin.</HelpTooltip>
                </div>
                  <p className="mt-5 font-mono text-sm font-medium tabular-nums text-slate-200">Available Spot USDT: {balanceUnavailable ? 'Unavailable' : formatUsdtTwoDecimals(availableUsdt)}</p>
                </section>
              ) : (
              <section className="rounded-lg border border-white/5 bg-slate-900 p-4" aria-label="Safety pause control">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Risk control</p>
                <div className="relative mt-1 flex items-center gap-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Manual Safety Pause</h2>
                  <HelpTooltip id="safety-pause-help" label="Manual safety pause">Cancel tracked BUY limits while keeping SELL limits active. The hard stop uses a separate liquidation protocol.</HelpTooltip>
                </div>
                {isResetPaused ? (
                  <div role="status" className="mt-6 rounded-md border border-emerald-500/20 bg-transparent px-3 py-2.5 text-xs font-medium text-emerald-400">
                    ✓ Orders Cleared. Awaiting New Grid Anchor.
                  </div>
                ) : (
                  <button aria-busy={pauseBusy} type="button" onClick={togglePause} disabled={!isOnline || !engineRunning || pauseBusy || hardStopLocked || isSizingPaused || (isCapExhausted && bot?.pause_mode !== 'PAUSED_MANUAL')}
                    className="mt-4 w-full rounded-md border border-orange-500/20 bg-orange-500/5 px-3 py-2.5 text-xs font-semibold text-orange-300 transition hover:bg-orange-500/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-orange-400/60 disabled:cursor-not-allowed disabled:opacity-50">
                    <ActionContent busy={pauseBusy}>{pauseBusy ? 'Updating…' : bot?.pause_mode === 'PAUSED_MANUAL' ? 'Release Safety Pause' : 'Trigger Safety Pause'}</ActionContent>
                  </button>
                )}
                {isSizingPaused && <p className="mt-3 text-xs leading-5 text-red-400" role="alert">Grid orders are paused because an exchange-rounded order is too small. Re-anchor with enough capital or explicitly choose fewer levels. {bot?.pause_reason}</p>}
                {isCapExhausted && <p className="mt-3 text-xs leading-5 text-amber-300" role="alert">New BUYs are paused at the allocation cap. While the engine is running, accepted orders, SELL protection and the hard stop remain active. Review and save a new grid to resume. {bot?.allocation_pause_reason}</p>}
                {controlError && <p className="mt-3 text-xs leading-5 text-red-400" role="alert">{controlError}</p>}
              </section>
              )}

              {!isStandby && !hardStopLocked && <section className="rounded-lg border border-white/5 bg-slate-900 px-4 py-5" aria-label="Adjust hard stop control">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Risk control</p>
                <div className="relative mt-1 flex items-center gap-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Raise Hard Stop</h2>
                  <HelpTooltip id="hard-stop-help" label="Raise hard stop">Raising the hard stop keeps the grid and its orders in place. The floor cannot be lowered and must remain below the configured lower bound.</HelpTooltip>
                </div>
                <div className="mt-4"><WarningBanner>Current floor: <strong className="font-mono font-medium">{formatUsdtTwoDecimals(bot?.current_hard_stop_loss)}</strong>. New stop must be higher and remain below the grid lower bound.</WarningBanner></div>
                <form onSubmit={handleHardStopUpdate} className="mt-5 space-y-4">
                  <label className="block text-xs text-slate-400"><span className="text-[11px] font-semibold uppercase tracking-wider">New Hard Stop (USDT)</span>
                    <span className="mt-2 flex overflow-hidden rounded-md border-b bg-slate-950/60 transition-colors border-white/5 focus-within:border-slate-400/70">
                      <input type="text" inputMode="decimal" autoComplete="off" spellCheck={false} required
                        placeholder="e.g. 79,500.00" value={hardStopInput}
                        onChange={(event) => { setHardStopInput(event.target.value); setHardStopError(''); setHardStopMessage('') }}
                        className="min-w-0 w-full bg-transparent px-3 py-2.5 font-mono text-sm font-medium normal-case tracking-normal tabular-nums text-white outline-none placeholder:font-sans placeholder:text-[11px] placeholder:font-normal placeholder:text-slate-400" />
                      <span className="flex shrink-0 items-center pr-3 font-mono text-[10px] font-medium tracking-normal text-slate-400">USDT</span>
                    </span>
                  </label>
                  <button aria-busy={hardStopBusy} type="submit" disabled={!isOnline || hardStopBusy || !hardStopInput.trim()}
                    className="w-full rounded-md border border-orange-500/20 bg-orange-500/5 px-3 py-2.5 text-xs font-semibold text-orange-300 transition hover:bg-orange-500/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-orange-400/60 disabled:cursor-not-allowed disabled:opacity-50">
                    <ActionContent busy={hardStopBusy}>{hardStopBusy ? 'Updating…' : 'Raise Hard Stop'}</ActionContent>
                  </button>
                  {hardStopMessage && <p className="text-xs leading-5 text-slate-300" role="status">{hardStopMessage}</p>}
                  {hardStopError && <p className="text-xs leading-5 text-red-400" role="alert">{hardStopError}</p>}
                </form>
              </section>}

              <section id="reanchor-grid-control" className={`rounded-lg border border-white/5 bg-slate-900 p-4 ${isResetPaused || canConfigureNewGrid ? 'ring-1 ring-inset ring-slate-500/50' : ''}`} aria-label="Re-anchor grid control">
                {dustRecovery && <div className="mb-5 rounded-md border border-amber-500/30 bg-transparent p-3" aria-label="Dust recovery">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wider text-amber-300">Unsellable BTC Recovery</h2>
                  <p className="mt-2 text-xs leading-5 text-amber-200/80"><span className="font-mono">{formatBtc(dustRecovery.quantity)}</span> remains after the exit. Binance cannot market-sell this amount. The engine is halted; retrying this sale cannot clear it.</p>
                  <p className="mt-2 text-xs text-slate-300">Recorded cost basis: <span className="font-mono">{formatUsdtTwoDecimals(dustRecovery.cost_basis_quote)}</span>.</p>
                  <p className="mt-2 text-xs leading-5 text-slate-300">You may explicitly keep this BTC as an unmonitored reserve. Its quantity and cost stay recorded. This unlocks new-grid configuration and does not place orders.</p>
                  <button aria-busy={dustBusy} ref={dustTriggerRef} type="button" disabled={!dustRecovery.can_acknowledge || dustBusy || engineBusy || panicBusy}
                    onClick={() => { setDustError(''); setDustConfirmation({ ...dustRecovery }) }}
                    className="mt-3 rounded-lg border border-amber-500/30 bg-transparent px-3 py-2.5 text-xs font-semibold text-amber-300 hover:bg-amber-500/5 disabled:opacity-50"><ActionContent busy={dustBusy}>Review &amp; Acknowledge Dust</ActionContent></button>
                  {dustError && !dustConfirmation && <p className="mt-3 text-sm text-red-300" role="alert">{dustError}</p>}
                </div>}
                {(isResetPaused || canConfigureNewGrid) && <span className="inline-flex rounded-full bg-white/5 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-slate-300">Action Required</span>}
                {isResetPaused && engineRunning && <div className="mt-3"><WarningBanner>STOP the engine, then queue a new Re-anchor Grid to unlock.</WarningBanner></div>}
                <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Grid execution</p>
                <div className="relative mt-1 flex items-center gap-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">{isStandby ? 'Configure Your Grid' : isLiquidated && !engineRunning ? 'Configure New Grid' : isLiquidated ? 'Reset Liquidated Bot' : 'Re-anchor Grid'}</h2>
                  <HelpTooltip id="reanchor-help" label="Grid execution">{isStandby ? 'Review your center, ceiling, hard stop, capital, and exact level count. Saving settings does not place orders; use START when ready.' : isAccountCleared ? 'All account BTC was sold. This grid is closed. To configure a new grid, open Advanced / Danger Zone and use Clear Order History (Database Reset) first.' : isLiquidated && !engineRunning ? 'Save a new grid after liquidation, then press START to trade.' : isLiquidated ? 'An admin reset starts a new grid and seed buy after the completed liquidation.' : engineRunning ? 'Set absolute prices for the new grid. The bot reconciles existing orders before placing Post-Only limits.' : bot?.pending_grid ? 'A saved grid is waiting. Submitting new settings replaces it while IDLE; START shows the final settings for review.' : 'Queue new bounds while the engine is idle. No orders will be placed until you press START.'} Typing values alone does not save them.</HelpTooltip>
                </div>
                {!engineRunning && bot?.pending_grid && <div className="mt-4 rounded-lg border border-white/5 bg-slate-950/40 p-3" role="status">
                  <p className="text-sm font-semibold text-slate-300">Grid settings saved. Ready for review.</p>
                  <button aria-busy={engineBusy} type="button" onClick={requestEngineStart} disabled={engineBusy}
                    className="mt-3 w-full rounded-md bg-slate-700 px-3 py-2.5 text-xs font-semibold text-slate-100 hover:bg-slate-600 disabled:opacity-50"><ActionContent busy={engineBusy}>Review &amp; START Saved Grid</ActionContent></button>
                </div>}
                <form id="reanchor-grid-form" onSubmit={handleRecenter} className="mt-6 space-y-5">
                  {gridConfigurationLocked && <WarningBanner>
                    Trading recovery is locked. You can edit a draft below; saving and START remain blocked until the remaining BTC and liquidation alarms are reviewed.
                  </WarningBanner>}
                  <fieldset disabled={panicBusy || dustBusy} className="space-y-5">
                  {canConfigureNewGrid && <p className="text-xs leading-5 text-slate-300" role="status">The previous grid is closed. Choose new bounds and a new hard stop for this run. Saving preserves trade history and places no orders.{dustReserveQuantity > 0 ? ' The dust reserve remains outside the new grid.' : ''}</p>}
                  {bot?.has_grid_run && !isLiquidated && bot?.current_hard_stop_loss != null && <WarningBanner>
                    Keep hard stop ≥ <strong className="font-mono font-medium">{formatPrice(bot.current_hard_stop_loss)}</strong> and the projected lower bound strictly above it.
                  </WarningBanner>}
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Grid inputs</p>
                  <div>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <label htmlFor="center-price" className="block text-xs text-slate-400"><span className="text-[11px] font-semibold uppercase tracking-wider">Center Price (USDT)</span></label>
                      <button type="button" onClick={useLiveCenterPrice} disabled={!!liveCenterDisabledReason}
                        title={liveCenterDisabledReason || 'Copy a fresh Binance price into the center field once.'}
                        aria-describedby="center-price-help"
                        className="rounded-md bg-white/5 px-2.5 py-1 text-[11px] font-semibold text-slate-300 hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-400 disabled:cursor-not-allowed disabled:opacity-40">
                        Use live price
                      </button>
                    </div>
                    <div className={`mt-2 flex overflow-hidden rounded-md border-b bg-slate-950/60 transition-colors ${safeBounds.lowerBoundUnsafe ? 'border-red-400/50 focus-within:border-red-400/80' : 'border-white/5 focus-within:border-slate-400/70'}`}>
                      <input id="center-price" type="text" inputMode="decimal" autoComplete="off" spellCheck={false} required
                        aria-invalid={safeBounds.lowerBoundUnsafe}
                        placeholder="Leave blank to use live price" value={centerPrice}
                        aria-describedby={safeBounds.lowerBoundUnsafe ? 'center-price-help reanchor-safe-bounds-error' : 'center-price-help'}
                        onChange={(event) => { setCenterPrice(event.target.value); setRecenterMessage(''); setGridCalculationFeedback(null) }}
                        className={`min-w-0 w-full bg-transparent px-3 py-2.5 font-mono text-sm font-medium normal-case tracking-normal tabular-nums outline-none placeholder:font-sans placeholder:text-[11px] placeholder:font-normal placeholder:text-slate-400 ${safeBounds.lowerBoundUnsafe ? 'text-red-400' : 'text-white'}`} />
                      <span className="flex shrink-0 items-center pr-3 font-mono text-[10px] font-medium tracking-normal text-slate-400">USDT</span>
                    </div>
                    <p id="center-price-help" className="mt-1 text-[11px] font-normal leading-4 text-slate-400">Blank uses a live snapshot. Enter a value to override.</p>
                    <p className="mt-1 font-mono text-[10px] leading-4 text-slate-400">{liveCenterDisabledReason || `Live reference: ${formatPrice(liveMarketPrice)}. Use live price only fills this field.`}</p>
                  </div>
                  <label className="block text-xs text-slate-400"><span className="text-[11px] font-semibold uppercase tracking-wider">Grid Ceiling (Upper Bound in USDT)</span>
                    <span className={`mt-2 flex overflow-hidden rounded-md border-b bg-slate-950/60 transition-colors ${safeBounds.lowerBoundUnsafe ? 'border-red-400/50 focus-within:border-red-400/80' : 'border-white/5 focus-within:border-slate-400/70'}`}>
                      <input type="text" inputMode="decimal" autoComplete="off" spellCheck={false} required
                        aria-invalid={safeBounds.lowerBoundUnsafe} aria-describedby={safeBounds.lowerBoundUnsafe ? 'reanchor-safe-bounds-error' : undefined}
                        placeholder="e.g. 95,000.00" value={ceilingPrice}
                        onChange={(event) => { setCeilingPrice(event.target.value); setRecenterMessage(''); setGridCalculationFeedback(null) }}
                        className={`min-w-0 w-full bg-transparent px-3 py-2.5 font-mono text-sm font-medium normal-case tracking-normal tabular-nums outline-none placeholder:font-sans placeholder:text-[11px] placeholder:font-normal placeholder:text-slate-400 ${safeBounds.lowerBoundUnsafe ? 'text-red-400' : 'text-white'}`} />
                      <span className="flex shrink-0 items-center pr-3 font-mono text-[10px] font-medium tracking-normal text-slate-400">USDT</span>
                    </span>
                  </label>
                  <label className="block text-xs text-slate-400">
                    <span className="text-[11px] font-semibold uppercase tracking-wider">Total Allocation Cap (USDT)</span>
                    <span className={`mt-2 flex overflow-hidden rounded-md border-b bg-slate-950/60 transition-colors ${allocationExceedsBalance ? 'border-red-400/50 focus-within:border-red-400/80' : 'border-white/5 focus-within:border-slate-400/70'}`}>
                      <input type="text" inputMode="decimal" autoComplete="off" spellCheck={false} required
                        placeholder="e.g. 100" value={allocatedCapital}
                        onChange={(event) => { setAllocatedCapital(event.target.value); setRecenterMessage('') }}
                        className={`min-w-0 w-full bg-transparent px-3 py-2.5 font-mono text-sm font-medium normal-case tracking-normal tabular-nums outline-none placeholder:font-sans placeholder:text-[11px] placeholder:font-normal placeholder:text-slate-400 ${allocationExceedsBalance ? 'text-red-400' : 'text-white'}`} />
                      <span className="flex shrink-0 items-center pr-3 font-mono text-[10px] font-medium tracking-normal text-slate-400">USDT</span>
                    </span>
                    {allocationExceedsBalance && <span className="mt-2 block text-xs font-normal text-red-400" role="alert">New BUY funding exceeds the currently available USDT.</span>}
                    {requiredNewQuote != null && <span className="mt-1 block font-mono text-xs normal-case text-slate-400">New quote funding: {formatPrice(requiredNewQuote)}</span>}
                    <span className={`mt-2 block text-xs font-normal ${exceedsTotalCap ? 'text-red-400' : 'text-slate-400'}`} role="status">Includes carried bot BTC cost and planned BUY notionals. Excludes future fees and price appreciation.</span>
                    {proposedTotalCost != null && <span className={`mt-1 block font-mono text-xs normal-case ${exceedsTotalCap ? 'text-red-400' : 'text-slate-300'}`}>Projected total cost: {formatPrice(proposedTotalCost)}{exceedsTotalCap ? ` · exceeds cap by ${formatPrice(proposedTotalCost - previewCapital)}` : ''}</span>}
                    {capitalBelowRequired && <span className="mt-2 block text-xs font-normal leading-5 text-red-400" role="alert">Minimum investment required for <span className="font-mono">{previewLevels}</span> levels is <span className="font-mono">{formatUsdtTwoDecimals(requiredCapital)}</span> to meet exchange rules.</span>}
                  </label>
                  <label className="block text-xs text-slate-400"><span className="text-[11px] font-semibold uppercase tracking-wider">Grid Levels</span>
                    <span className="mt-2 flex overflow-hidden rounded-md border-b bg-slate-950/60 transition-colors border-white/5 focus-within:border-slate-400/70">
                      <input type="text" inputMode="numeric" autoComplete="off" spellCheck={false} required
                        placeholder="5 (production baseline)" value={gridLevels}
                        onChange={(event) => { setGridLevels(event.target.value); setRecenterMessage('') }}
                        className="min-w-0 w-full bg-transparent px-3 py-2.5 font-mono text-sm font-medium normal-case tracking-normal tabular-nums text-white outline-none placeholder:font-sans placeholder:text-[11px] placeholder:font-normal placeholder:text-slate-400" />
                      <span className="flex shrink-0 items-center pr-3 font-mono text-[10px] normal-case tracking-normal text-slate-400">levels</span>
                    </span>
                    <span className="mt-1 block text-[11px] font-normal leading-4 text-slate-400">Exact total BUY + SELL levels. Odd totals place one extra BUY level.</span>
                  </label>
                  <div className="space-y-2 rounded-md bg-slate-950/40 p-3" aria-label="Grid calculator">
                    <div className="relative flex items-center gap-2">
                      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Draft calculator</h3>
                      <HelpTooltip id="grid-calculator-help" label="Grid calculator">Generate the 5-level baseline using 0.5× closed 1h ATR and a 0.4% gross spacing floor. A blank center uses a fresh live price once. Retains any higher active hard stop. Review before saving.</HelpTooltip>
                    </div>
                    <button type="button" onClick={calculateGrid} disabled={!canCalculateGrid}
                      title={calculateGridDisabledReason || 'Calculate bounds from your entered center, or a fresh live price if blank, and 1h ATR.'}
                      aria-describedby="grid-atr-status"
                      className="w-full rounded-md border border-white/5 bg-slate-800/60 px-3 py-2 text-xs font-medium text-slate-300 transition hover:bg-slate-800 focus-visible:outline focus-visible:outline-1 focus-visible:outline-slate-500 disabled:cursor-not-allowed disabled:opacity-40">
                      Calculate Grid (ATR)
                    </button>
                    <p id="grid-atr-status" role="status" className={`text-[11px] leading-4 ${canCalculateGrid ? 'text-slate-400' : 'text-amber-200/80'}`}>
                      {calculateGridDisabledReason || 'Ready to calculate from 1h ATR.'}
                    </p>
                  </div>
                  <section className="space-y-3 rounded-md border border-white/5 bg-slate-800/30 p-3 text-xs leading-5 text-slate-400" aria-label="Calculated grid preview" aria-live="polite">
                    <div className="relative flex items-center gap-2 border-b border-white/5 pb-2">
                      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Calculated outputs (read only)</h3>
                      <HelpTooltip id="grid-preview-help" label="Calculated outputs">New grids use a fixed hard stop 15% below the lower bound; tighter active floors are retained. Stops do not trail. A stop cancels grid orders, market-sells bot BTC, and requires manual review before START. Gross grid returns are before fees; the budget per level is an estimate.</HelpTooltip>
                    </div>
                  <label className="block text-xs text-slate-400"><span className="text-[11px] font-semibold uppercase tracking-wider">Hard Stop Loss (USDT)</span>
                    <span className={`mt-2 flex overflow-hidden rounded-md border-b bg-slate-950/60 transition-colors ${safeBounds.stopBelowFloor ? 'border-red-400/50 focus-within:border-red-400/80' : 'border-white/5 focus-within:border-slate-400/70'}`}>
                      <input type="text" inputMode="decimal" autoComplete="off" spellCheck={false} required
                        aria-invalid={safeBounds.stopBelowFloor} aria-describedby={safeBounds.stopBelowFloor ? 'reanchor-safe-bounds-error' : undefined}
                        placeholder="Calculated from the lower bound" readOnly
                        value={Number.isFinite(previewStopLoss) ? previewStopLoss.toFixed(2) : ''}
                        className={`min-w-0 w-full bg-transparent px-3 py-2.5 font-mono text-sm font-medium normal-case tracking-normal tabular-nums outline-none placeholder:font-sans placeholder:text-[11px] placeholder:font-normal placeholder:text-slate-400 ${safeBounds.stopBelowFloor ? 'text-red-400' : 'text-white'}`} />
                      <span className="flex shrink-0 items-center pr-3 font-mono text-[10px] font-medium tracking-normal text-slate-400">USDT</span>
                    </span>
                  </label>

                    <dl className="divide-y divide-white/5">
                      <GridMetric label="Min. gross return / grid" tone={spacingPreview.valid ? 'text-emerald-400' : 'text-red-400'}>{spacingPreview.minimumGrossPercent == null ? '—' : `${spacingPreview.minimumGrossPercent.toFixed(3)}%`}</GridMetric>
                      <GridMetric label="Calculated width">{previewWidthPercentage == null ? '—' : `${previewWidthPercentage.toFixed(2)}%`}</GridMetric>
                      <GridMetric label="Projected lower bound" tone={safeBounds.lowerBoundUnsafe ? 'text-red-400' : 'text-slate-200'}>{projectedLowerBound == null ? '—' : formatPrice(projectedLowerBound)}</GridMetric>
                      <GridMetric label="Distance to stop loss">{previewStopDistance == null ? '—' : `${previewStopDistance.toFixed(2)}%`}</GridMetric>
                      <GridMetric label="Budget / grid level" tone={capitalBelowRequired ? 'text-red-400' : 'text-slate-200'}>{estimatedOrderSize == null ? '—' : formatUsdtTwoDecimals(estimatedOrderSize)}</GridMetric>
                    </dl>
                    <p className="text-[10px] text-slate-400">Required gross return: <span className="font-mono">0.4%</span> per grid, before fees.</p>
                    {!spacingPreview.valid && hasPreviewCeiling && validLevels && <p className="text-red-400" role="alert">{spacingPreview.message}</p>}
                    {!isStandby && !isLiquidated && Number(wallet?.btc_held) > 0 && Number.isSafeInteger(previewLevels) && previewLevels >= 2 && <WarningBanner>
                      SELL limits reuse <span className="font-mono">{formatBtc(wallet.btc_held)}</span> across <span className="font-mono">{Math.floor(previewLevels / 2)}</span> SELL levels. Exchange rounding and fee reserves can make these smaller than the budget estimate. Increasing allocated USDT does not top up this BTC.
                    </WarningBanner>}
                  </section>
                  {!safeBounds.valid && showBoundsError && <p id="reanchor-safe-bounds-error" className="text-xs leading-5 text-red-400" role="alert">
                    {safeBounds.message} {safeBounds.effectiveStop > 0 && <>Required lower bound: above <span className="font-mono">{formatPrice(safeBounds.effectiveStop)}</span>.</>}
                  </p>}
                  {gridCalculationFeedback && <p className={`text-xs leading-5 ${gridCalculationFeedback.error ? 'text-red-400' : 'text-emerald-300'}`} role={gridCalculationFeedback.error ? 'alert' : 'status'}>{gridCalculationFeedback.message}</p>}
                  {recenterMessage && <div ref={recenterFeedbackRef} className={`rounded-md border px-3 py-2.5 text-xs leading-5 ${recenterFailed ? 'border-red-500/30 bg-transparent text-red-300' : 'border-emerald-500/25 bg-transparent text-emerald-300'}`} role={recenterFailed ? 'alert' : 'status'}>
                    <p className="font-semibold">{recenterFailed ? 'Grid settings were not saved' : 'Grid settings saved'}</p>
                    <p>{recenterMessage}</p>
                  </div>}
                  </fieldset>
                </form>
              </section>
              </div>

              <details className="group rounded-lg border border-white/5 bg-slate-900/50">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-lg px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-400 [&::-webkit-details-marker]:hidden">
                  <span>Advanced / Danger Zone</span>
                  <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180">
                    <path d="m5 7.5 5 5 5-5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </summary>
                <div className="space-y-4 px-3 pb-3">
                  <div className="relative flex items-center gap-2">
                    <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">High impact actions</h3>
                    <HelpTooltip id="danger-zone-help" label="Danger zone">These actions can cancel orders, sell bot-tracked BTC, or permanently clear trading records. Each action requires confirmation.</HelpTooltip>
                  </div>
                  <section className="rounded-md border border-white/5 bg-slate-900 p-4" aria-label="Emergency liquidation control">
                    <p className="text-[10px] font-semibold uppercase tracking-wider text-red-400/80">Emergency exit</p>
                    <div className="relative mt-1 flex items-center gap-2">
                      <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Manual Panic Liquidate</h2>
                      <HelpTooltip id="panic-control-help" label="Manual panic liquidation">Cancel all bot grid orders, market-sell remaining bot-tracked BTC, and halt the engine. Market execution can incur slippage. Pre-existing BTC outside the bot remains in your wallet.</HelpTooltip>
                    </div>
                    <button aria-busy={panicBusy} ref={panicTriggerRef} type="button" onClick={openPanicModal} disabled={panicUnavailable}
                      className="mt-4 w-full rounded-md border border-red-500/25 bg-red-500/5 px-3 py-2.5 text-xs font-semibold text-red-300 transition hover:bg-red-500/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-400 disabled:cursor-not-allowed disabled:opacity-50">
                      <ActionContent busy={panicBusy}>{panicBusy ? 'Liquidating…' : 'Panic Liquidate (Sell Bot BTC Only & Halt)'}</ActionContent>
                    </button>
                  </section>
              {!isStandby && <section className="rounded-lg border border-white/5 bg-slate-900 p-4" aria-label="Order history database reset control">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Database maintenance</p>
                <div className="relative mt-1 flex items-center gap-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Clear Order History</h2>
                  <HelpTooltip id="order-history-help" label="Order history maintenance">First clear bot BUY and SELL orders to pause placement and reconcile records. Bot-owned BTC and other BTC/USDT orders must be resolved before wiping data. Database Reset permanently clears bot order, trade, and inventory history; it does not sell BTC or reset Binance balances. Saved settings, transition journal, and dust reserves remain. Trading stays idle until a new grid starts.</HelpTooltip>
                </div>
                <button aria-busy={clearOrdersBusy} type="button" onClick={() => { setClearOrdersError(''); setIsClearOrdersModalOpen(true) }} disabled={!isOnline || clearOrdersBusy || hardStopLocked}
                  className="mt-4 w-full rounded-md border border-white/5 bg-slate-800 px-3 py-2.5 text-xs font-semibold text-slate-300 transition hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white disabled:cursor-not-allowed disabled:opacity-50">
                  <ActionContent busy={clearOrdersBusy}>{clearOrdersBusy ? 'Clearing orders…' : isResetPaused ? 'Retry Bot Order Cleanup' : 'Clear Bot Orders for Reset'}</ActionContent>
                </button>
                {clearOrdersMessage && <p className="mt-3 text-xs leading-5 text-slate-300" role="status">{clearOrdersMessage}</p>}
                <button aria-busy={resetBusy} ref={resetTriggerRef} type="button" onClick={() => { setResetMessage(''); setResetError(null); setIsResetModalOpen(true) }} disabled={!isOnline || resetBusy}
                  className="mt-4 w-full rounded-md border border-red-500/25 bg-transparent px-3 py-2.5 text-xs font-semibold text-red-300 transition hover:bg-red-500/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-400 disabled:cursor-not-allowed disabled:opacity-50">
                  <ActionContent busy={resetBusy}>{resetBusy ? 'Checking account…' : 'Clear Order History (Database Reset)'}</ActionContent>
                </button>
                {resetMessage && <p className="mt-3 text-xs leading-5 text-slate-300" role="status">{resetMessage}</p>}
              </section>}
                </div>
              </details>
              {panicAwaitingConfirmation && !isPanicModalOpen && <p className="text-sm text-amber-300" role="status">Checking the saved liquidation result automatically. Do not submit another exit.</p>}
              {panicError && !isPanicModalOpen && <p className="text-sm text-amber-300" role="alert">{panicError}</p>}
              {panicMessage && <p ref={panicFeedbackRef} tabIndex={-1} className="text-sm text-emerald-300" role="status">{panicMessage}</p>}
            </>
          ) : (
            <section className="rounded-lg border border-white/5 bg-slate-900 p-4" aria-label="Locked controls">
              <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">Admin controls locked</p>
              <p className="mt-3 text-sm leading-6 text-slate-400">Sign in to manage Safety Pause and re-anchor the grid.</p>
              <button type="button" onClick={() => { setLoginError(''); setShowLogin(true) }} disabled={authChecking}
                className="mt-4 w-full rounded-md bg-slate-700 px-3 py-2.5 text-xs font-semibold text-slate-100 transition hover:bg-slate-600 disabled:opacity-50">
                Login to Unlock Controls
              </button>
            </section>
          )}
          {!authChecking && isAuthenticated && !isMonitorOnly && <div className="space-y-3 border-t border-white/5 pt-4 pb-[max(0.5rem,env(safe-area-inset-bottom))]" aria-label="Grid execution action">
            <p className={`text-[11px] leading-5 ${engineRunning ? 'text-amber-200/80' : 'text-slate-400'}`}>
              {engineRunning ? 'Applies to the live grid and may replace orders.' : 'Saves settings only. Review and START separately.'}
            </p>
              <button aria-busy={recenterBusy} type="submit" form="reanchor-grid-form" disabled={gridConfigurationLocked || !isOnline || isAccountCleared || !safeBounds.valid || !spacingPreview.valid || !validAllocation || exceedsTotalCap || capitalBelowRequired || recenterBusy}
                    className="w-full rounded-md bg-slate-700 px-3 py-2.5 text-xs font-semibold text-slate-100 transition hover:bg-slate-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-400 disabled:cursor-not-allowed disabled:opacity-50">
                    <ActionContent busy={recenterBusy}>{gridConfigurationLocked ? 'Recovery Required Before Saving' : recenterBusy ? 'Submitting…' : isStandby ? 'Save Grid Settings' : isLiquidated && !engineRunning ? 'Save New Grid' : isLiquidated ? 'Start New Grid' : engineRunning ? 'Execute Re-anchor' : bot?.pending_grid ? 'Replace Pending Grid' : 'Queue Re-anchor'}</ActionContent>
                  </button>
          </div>}
          </div>
        </aside>}

      {dustConfirmation && <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/75 p-4 backdrop-blur-sm">
        <div ref={dustModalRef} role="alertdialog" aria-modal="true" aria-labelledby="dust-title" aria-describedby="dust-warning"
          aria-busy={dustBusy} tabIndex={-1} onKeyDown={handleDustModalKeyDown}
          className="w-full max-w-lg rounded-xl border border-amber-600 bg-[#181A20] p-6 shadow-2xl">
          <h2 id="dust-title" className="text-xl font-bold text-amber-200">Keep Dust &amp; Unlock Configuration?</h2>
          <p id="dust-warning" className="mt-4 text-sm leading-6 text-gray-200">Keep {formatBtc(dustConfirmation.quantity)} in Spot as an unmonitored dust reserve. Its quantity and cost remain recorded. This BTC is excluded from the next grid and its stop monitoring. No BTC is sold and no trading starts.</p>
          <p className="mt-3 text-sm text-amber-200">Recorded cost basis: {formatUsdtTwoDecimals(dustConfirmation.cost_basis_quote)}.</p>
          <p className="mt-3 text-sm text-slate-400">After confirmation, save a new grid with a new hard stop, then review and press START when ready.</p>
          {dustError && <p className="mt-4 rounded bg-red-950/50 p-3 text-sm text-red-300" role="alert">{dustError}</p>}
          <div className="mt-6 flex justify-end gap-3">
            <button type="button" autoFocus disabled={dustBusy} onClick={closeDustModal} className="rounded-lg px-4 py-2 text-sm text-gray-300">Keep Locked</button>
            <button type="button" disabled={dustBusy} aria-busy={dustBusy} onClick={acknowledgeDust} className="rounded-lg bg-amber-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"><ActionContent busy={dustBusy}>{dustBusy ? 'Verifying…' : 'Confirm Unmonitored Dust'}</ActionContent></button>
          </div>
        </div>
      </div>}

      {isPanicModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4 backdrop-blur-sm">
          <div ref={panicModalRef} role="alertdialog" aria-modal="true" aria-labelledby="panic-title" aria-describedby="panic-warning"
            aria-busy={panicBusy} tabIndex={-1} onKeyDown={handlePanicModalKeyDown}
            className="w-full max-w-md rounded-xl border border-red-700/70 bg-[#181A20] p-6 shadow-2xl">
            <h2 id="panic-title" className="text-xl font-bold text-red-400">Confirm Panic Liquidation</h2>
            <p id="panic-warning" className="mt-3 text-sm leading-6 text-gray-200">Are you sure you want to market-sell all bot BTC and halt?</p>
            <p className="mt-3 text-sm leading-6 text-slate-400">This cancels bot BUY and SELL orders and sells the remaining bot-tracked BTC at market. The sale cannot be undone. Trading stays halted until you save and start a new grid.</p>
            {(panicBusy || panicAwaitingConfirmation) && <p className="mt-4 text-sm text-amber-300" role="status">Checking liquidation completion. Cancelling orders and verifying the market sale may take time. Status updates automatically; do not submit another exit.</p>}
            {panicError && <p className={`mt-4 rounded p-3 text-sm ${panicAwaitingConfirmation ? 'bg-amber-950/50 text-amber-200' : 'bg-red-950/50 text-red-300'}`} role="alert">{panicError}</p>}
            <div className="mt-6 flex justify-end gap-3">
              <button type="button" autoFocus disabled={panicBusy} onClick={closePanicModal}
                className="rounded-lg px-4 py-2 text-sm text-gray-300 hover:text-white focus-visible:outline-2 focus-visible:outline-white disabled:opacity-50">{panicAwaitingConfirmation ? 'Close' : 'Cancel'}</button>
              <button aria-busy={panicBusy || panicAwaitingConfirmation} type="button" disabled={panicUnavailable || !isAuthenticated} onClick={handlePanicLiquidate}
                className="rounded-lg bg-red-700 px-4 py-2 text-sm font-semibold text-white hover:bg-red-600 focus-visible:outline-2 focus-visible:outline-red-400 disabled:opacity-50">
                <ActionContent busy={panicBusy || panicAwaitingConfirmation}>{panicBusy ? 'Liquidating…' : panicAwaitingConfirmation ? 'Checking Status…' : 'Yes, Sell Bot BTC & Halt'}</ActionContent>
              </button>
            </div>
          </div>
        </div>
      )}

      {isResetModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4 backdrop-blur-sm">
          <div ref={resetModalRef} role="dialog" aria-modal="true" aria-labelledby="reset-title" aria-describedby="reset-warning"
            aria-busy={resetBusy} onKeyDown={handleResetModalKeyDown}
            className="w-full max-w-md rounded-lg border border-red-900/50 bg-[#181A20] p-6 shadow-2xl">
            <h2 id="reset-title" className="mb-2 text-xl font-bold text-red-500">Confirm Database Reset</h2>
            <p id="reset-warning" className="mb-4 text-sm leading-6 text-gray-300">WARNING: This will permanently delete all historical trade data, inventory tracking, and reset PNL to zero. This action cannot be undone.</p>
            {resetError && <p className="mb-4 rounded border border-red-800 bg-red-950/50 p-3 text-sm text-red-400" role="alert">{resetError}</p>}
            <div className="mt-6 flex flex-wrap justify-end gap-3">
              <button type="button" autoFocus onClick={closeResetModal} aria-disabled={resetBusy}
                className="rounded px-4 py-2 text-sm text-slate-400 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white">
                Cancel
              </button>
              <button aria-busy={resetBusy} type="button" onClick={handleFactoryReset} disabled={resetBusy}
                className="rounded bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-400 disabled:cursor-wait disabled:opacity-60">
                <ActionContent busy={resetBusy}>{resetBusy ? 'Clearing history…' : 'Yes, Clear Order History'}</ActionContent>
              </button>
            </div>
          </div>
        </div>
      )}

      {isStopEngineModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4 backdrop-blur-sm">
          <div role="dialog" aria-modal="true" aria-labelledby="stop-engine-title"
            className="w-full max-w-md rounded-xl bg-[#181A20] p-6 shadow-2xl">
            <h2 id="stop-engine-title" className="text-xl font-bold text-white">Stop Trading Engine?</h2>
            <p className="mt-3 text-sm leading-6 text-gray-300">This switches the engine to IDLE and cancels all bot-owned BUY and SELL grid orders. Existing bot-held BTC stays in your Spot wallet without active grid orders or hard-stop monitoring until you start again.</p>
            {engineError && <p className="mt-4 rounded bg-red-950/50 p-3 text-sm text-red-400" role="alert">{engineError}</p>}
            <div className="mt-6 flex justify-end gap-3">
              <button type="button" autoFocus disabled={engineBusy} onClick={() => { setIsStopEngineModalOpen(false); setEngineError('') }}
                className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-white disabled:opacity-50">Keep Running</button>
              <button aria-busy={engineBusy} type="button" disabled={engineBusy} onClick={() => changeEngineStatus('stop')}
                className="rounded-lg bg-red-700 px-4 py-2 text-sm font-semibold text-white hover:bg-red-600 disabled:opacity-50">
                <ActionContent busy={engineBusy}>{engineBusy ? 'Stopping…' : 'Stop Engine'}</ActionContent>
              </button>
            </div>
          </div>
        </div>
      )}

      {pendingStartGrid && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4 backdrop-blur-sm">
          <div role="dialog" aria-modal="true" aria-labelledby="pending-grid-title"
            className="w-full max-w-md rounded-xl border border-amber-500/30 bg-[#181A20] p-6 shadow-2xl">
            <p className="text-xs font-semibold uppercase tracking-wider text-amber-400">Saved grid awaiting START</p>
            <h2 id="pending-grid-title" className="mt-2 text-xl font-bold text-white">Review the pending grid</h2>
            <p className="mt-3 text-sm leading-6 text-gray-300">START will place or resume this saved grid. It may be from an earlier attempt. Check the settings before creating orders.</p>
            <dl className="mt-4 grid grid-cols-2 gap-3 rounded-lg bg-white/5 p-4 text-sm">
              <div><dt className="text-slate-400">Request</dt><dd className="text-white">{pendingStartGrid.source === 'manual_recenter' ? 'Saved re-anchor' : pendingStartGrid.source === 'manual_initial' ? 'New grid' : pendingStartGrid.source === 'engine_resume' ? 'Rebuild after STOP' : 'Saved grid'}</dd></div>
              <div><dt className="text-slate-400">Center</dt><dd className="text-white">{pendingStartGrid.center == null ? 'Live price at placement' : formatPrice(pendingStartGrid.center)}</dd></div>
              <div><dt className="text-slate-400">Lower bound</dt><dd className="text-white">{formatPrice(pendingStartGrid.lower)}</dd></div>
              <div><dt className="text-slate-400">Upper bound</dt><dd className="text-white">{formatPrice(pendingStartGrid.upper)}</dd></div>
              <div><dt className="text-slate-400">Hard stop</dt><dd className="text-white">{formatPrice(pendingStartGrid.stop_loss)}</dd></div>
              <div><dt className="text-slate-400">Capital</dt><dd className="text-white">{formatUsdtTwoDecimals(pendingStartGrid.investment_quote)}</dd></div>
              <div className="col-span-2"><dt className="text-slate-400">Grid levels</dt><dd className="text-white">{pendingStartGrid.buy_levels ?? '?'} BUY + {pendingStartGrid.sell_levels ?? '?'} SELL</dd></div>
            </dl>
            {autoCoverInventory && <p className="mt-3 text-xs text-slate-400">Existing bot BTC will be covered with staggered SELL limits before new BUY limits. Resuming keeps the saved grid center.</p>}
            {!pendingStartGrid.token && <p className="mt-3 text-sm text-red-400" role="alert">Saved grid details are invalid. START is blocked until they are reviewed.</p>}
            {engineError && <p className="mt-3 rounded bg-red-950/50 p-3 text-sm text-red-400" role="alert">{engineError}</p>}
            <div className="mt-6 flex justify-end gap-3">
              <button type="button" autoFocus disabled={engineBusy} onClick={() => { setPendingStartGrid(null); setEngineError('') }}
                className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-white disabled:opacity-50">Cancel</button>
              <button aria-busy={engineBusy} type="button" disabled={engineBusy || !pendingStartGrid.token}
                onClick={() => changeEngineStatus('start', pendingStartGrid.token)}
                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-500 disabled:opacity-50">
                <ActionContent busy={engineBusy}>{engineBusy ? 'Starting…' : 'Start This Grid'}</ActionContent>
              </button>
            </div>
          </div>
        </div>
      )}

      {isClearOrdersModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4 backdrop-blur-sm">
          <div role="dialog" aria-modal="true" aria-labelledby="clear-orders-title"
            className="w-full max-w-md rounded-lg bg-[#181A20] p-6 shadow-2xl">
            <h2 id="clear-orders-title" className="text-xl font-bold text-white">Clear Bot Orders for Reset</h2>
            <p className="mt-3 text-sm leading-6 text-gray-300">Cancel all bot-owned BTC/USDT BUY and SELL limits and stop the grid from placing replacements. This keeps any BTC already held; it does not wipe the database.</p>
            {clearOrdersError && <p className="mt-4 rounded bg-red-950/50 p-3 text-sm text-red-400" role="alert">{clearOrdersError}</p>}
            <div className="mt-6 flex flex-wrap justify-end gap-3">
              <button type="button" autoFocus disabled={clearOrdersBusy}
                onClick={() => { setIsClearOrdersModalOpen(false); setClearOrdersError('') }}
                className="rounded px-4 py-2 text-sm text-slate-400 hover:text-white disabled:opacity-50">Cancel</button>
              <button aria-busy={clearOrdersBusy} type="button" onClick={handleClearBotOrders} disabled={clearOrdersBusy}
                className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50">
                <ActionContent busy={clearOrdersBusy}>{clearOrdersBusy ? 'Clearing…' : 'Clear Bot Orders'}</ActionContent>
              </button>
            </div>
          </div>
        </div>
      )}

      {showLogin && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 px-4 backdrop-blur-sm">
          <div role="dialog" aria-modal="true" aria-labelledby="login-title" className="w-full max-w-md rounded-2xl bg-[#181A20] p-8 shadow-2xl shadow-black/50">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-medium uppercase tracking-wider text-slate-400">Admin access</p>
                <h2 id="login-title" className="mt-2 text-2xl font-bold text-white">Unlock controls</h2>
              </div>
              <button type="button" onClick={() => { setShowLogin(false); setPassword('') }} aria-label="Close login" className="rounded-lg px-2 py-1 text-xl text-slate-400 hover:text-white">×</button>
            </div>
            <p className="mt-3 text-sm leading-6 text-slate-400">Sign in to create a short admin session and manage Safety Pause.</p>
            <form onSubmit={handleLogin} className="mt-6 space-y-4">
              <div>
                <label htmlFor="admin-password" className="block text-sm font-semibold text-slate-200">Admin password</label>
                <input
                  id="admin-password"
                  type="password"
                  autoComplete="current-password"
                  required
                  maxLength={128}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  className="mt-2 w-full rounded-lg bg-[#0B0E11] px-4 py-3 text-white outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                />
              </div>
              {loginError && <p className="text-sm text-red-400" role="alert">{loginError}</p>}
              <button aria-busy={loggingIn} type="submit" disabled={loggingIn} className="w-full rounded-lg bg-blue-600 px-4 py-3 text-sm font-semibold text-white transition hover:bg-blue-500 disabled:cursor-wait disabled:opacity-60">
                <ActionContent busy={loggingIn}>{loggingIn ? 'Signing in…' : 'Sign in'}</ActionContent>
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

export default App
