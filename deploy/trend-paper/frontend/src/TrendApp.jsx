import { useEffect, useState } from 'react'
import TrendChart from './TrendChart.jsx'
import { money, number, freshSnapshot, signalChecks, recentFills, controlAvailability } from './trendModel.js'
import './trend.css'

const API = import.meta.env.VITE_TREND_API_BASE || window.location.origin
const time = (seconds) => seconds ? new Date(seconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—'
const datetime = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString() : '—'
const label = (value) => String(value || '').replaceAll('_', ' ')

async function request(path, body) {
  const response = await fetch(`${API}${path}`, {
    method: body ? 'POST' : 'GET', credentials: 'include', signal: AbortSignal.timeout(8000),
    ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  })
  const result = await response.json()
  if (!response.ok) {
    const error = new Error(typeof result.detail === 'string' ? result.detail : 'Request failed')
    error.status = response.status
    throw error
  }
  return result
}

function Metric({ title, value, suffix = '', pnl = false }) {
  const n = number(value)
  return <div className="trend-metric"><span>{title}</span><strong className={pnl ? n < 0 ? 'trend-negative' : n > 0 ? 'trend-positive' : '' : ''}>{money(value, pnl)}{suffix && <small> {suffix}</small>}</strong></div>
}

export default function TrendApp() {
  const [session, setSession] = useState(null)
  const [password, setPassword] = useState('')
  const [snapshot, setSnapshot] = useState(null)
  const [error, setError] = useState('')
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [settings, setSettings] = useState(false)
  const [now, setNow] = useState(() => Date.now() / 1000)

  useEffect(() => {
    document.title = 'Trend Terminal | SOL Paper'
  }, [])

  useEffect(() => {
    let active = true
    request('/api/auth/me').then(() => { if (active) setSession(true) })
      .catch(() => { if (active) setSession(false) })
    return () => { active = false }
  }, [])
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(timer)
  }, [])
  useEffect(() => {
    if (!session) return undefined
    let active = true
    let timer
    async function poll() {
      try {
        const result = await request('/api/trend/status')
        if (result.mode !== 'PAPER' || result.strategy_id !== 'sol-ema-20-50-200-atr3-v1' || result.symbol !== 'SOL/USDT') {
          throw new Error('Unexpected service; paper controls disabled')
        }
        if (active) { setSnapshot(result); setFailed(false) }
      } catch (e) {
        if (active) { setFailed(true); if (e.status === 401) setSession(false) }
      }
      if (active) timer = setTimeout(poll, 5000)
    }
    poll()
    const renewal = setInterval(() => request('/api/auth/refresh', {}).catch(() => setSession(false)), 20 * 60_000)
    return () => { active = false; clearTimeout(timer); clearInterval(renewal) }
  }, [session])

  async function login(event) {
    event.preventDefault(); setBusy(true); setError('')
    try { await request('/api/auth/login', { password }); setPassword(''); setSession(true) }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }
  async function action(value) {
    if (value === 'close' && !window.confirm('Close the simulated SOL position at the next available modeled execution price and pause entries?')) return
    setBusy(true); setError('')
    try { setSnapshot(await request('/api/trend/control', { action: value, confirmed: value === 'close' })); setFailed(false) }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const fresh = freshSnapshot(snapshot, now, failed)
  const controls = controlAvailability(snapshot, fresh)
  const position = snapshot?.position
  const signal = snapshot?.signal
  const fills = recentFills(snapshot?.events)
  const activity = (snapshot?.events || []).filter((e) => !['fill', 'trade', 'market_rules'].includes(e.kind)).slice(0, 3)
  const pending = signal && signal.scheduled_at > now
  const headline = !fresh ? snapshot ? 'Market data unavailable' : 'Connecting to paper account'
    : !snapshot.history_complete ? 'Preparing signal history'
      : snapshot.error ? 'Signal checks paused'
        : position?.active ? 'Following the trend'
          : snapshot.entries_enabled ? 'Waiting for the next setup' : 'Ready for paper trading'

  if (session !== true) return <main className="trend-login-shell"><form className="trend-login" onSubmit={login}>
    <div className="trend-eyebrow">SOL TREND · PAPER ACCOUNT</div><h1>Trend Terminal</h1>
    <p>Public market prices. Simulated capital and orders.</p>
    <label htmlFor="trend-password">Dashboard password</label>
    <input id="trend-password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
    {error && <div role="alert" className="trend-negative">{error}</div>}
    <button disabled={busy || session === null} type="submit">{session === null ? 'Connecting…' : busy ? 'Signing in…' : 'Open paper account'}</button>
  </form></main>

  return <main className="trend-terminal">
    <header className="trend-header">
      <div className="trend-brand"><b>TREND TERMINAL</b><span>SOL / USDT <em>PAPER</em></span></div>
      <div className="trend-header-metrics">
        <Metric title="ACCOUNT EQUITY" value={snapshot?.equity} suffix="USDT" />
        <Metric title="REALIZED P&L" value={snapshot?.realized_pnl} suffix="USDT" pnl />
        <Metric title="UNREALIZED P&L" value={snapshot?.unrealized_pnl} suffix="USDT" pnl />
        <Metric title="AVAILABLE CASH" value={snapshot?.cash} suffix="USDT" />
      </div>
      <div className="trend-actions">
        <span className={fresh ? 'trend-connection trend-positive' : 'trend-connection trend-warning'}>● {fresh ? 'MARKET CONNECTED' : 'RECONNECTING'}</span>
        <button className="trend-icon" title="Strategy settings" aria-label="Strategy settings" onClick={() => setSettings(true)}>⚙</button>
        <button disabled={busy || !(snapshot?.entries_enabled ? controls.pause : controls.start)} onClick={() => action(snapshot.entries_enabled ? 'pause' : 'start')}>
          {busy ? 'Working…' : snapshot?.entries_enabled ? 'Pause entries' : 'Start paper'}
        </button>
      </div>
    </header>
    <div className="trend-scope"><span>SIMULATED TRADING</span> Mainnet market data · {money(snapshot?.policy.capital)} USDT starting capital · No exchange orders</div>
    {(error || failed || snapshot?.error) && <div className="trend-alert" role="alert">{error || (failed ? 'Dashboard refresh failed. Displaying the last confirmed paper snapshot.' : snapshot.error)}</div>}
    <div className="trend-workspace">
      <section className="trend-main-column">
        <section className="trend-signal-card">
          <div className="trend-signal-heading"><div><span className="trend-eyebrow">EMA TREND FOLLOWING · 4H</span><h1>{headline}</h1></div>
            <div className="trend-signal-time">Last 4h close <b>{datetime(signal?.closed_at)}</b>{pending && <small>Decision eligible at {time(signal.scheduled_at)}</small>}</div></div>
          <div className="trend-checks">{signal ? signalChecks(signal).map((check) => <span key={check.label}><i className={check.passed ? 'trend-positive' : ''}>{check.passed ? '✓' : '○'}</i>{check.label}</span>)
            : <span>{snapshot?.hourly_bars ? `${snapshot.hourly_bars.toLocaleString()} hourly candles loaded; restoring the historical indicator seed` : 'Loading market history and exchange size rules'}</span>}</div>
        </section>
        <section className="trend-chart-panel">
          <div className="trend-chart-title"><b>PRICE & TREND</b><span><i className="trend-ema-fast">EMA20</i><i className="trend-ema-slow">EMA50</i><i className="trend-ema-long">EMA200</i></span><span>4h · closed candles · UTC chart</span></div>
          <div className="trend-chart-wrap"><TrendChart snapshot={snapshot} />{!snapshot?.candles?.length && <div className="trend-chart-empty">Chart appears after closed candles arrive</div>}</div>
        </section>
        <section className="trend-position-strip">
          <Metric title="SOL MARKET MID" value={snapshot?.quote?.mid} suffix="USDT" />
          <Metric title="POSITION ENTRY" value={position?.entry} suffix="USDT" />
          <Metric title="TRAILING STOP" value={position?.stop} suffix="USDT" />
          <Metric title="4H ATR · 14" value={signal?.atr} suffix="USDT" />
          <Metric title="CURRENT 1% RISK BUDGET" value={number(snapshot?.equity) == null ? null : Number(snapshot.equity) * .01} suffix="USDT" />
        </section>
        <footer className="trend-footer"><span>Signal delay 1h · Initial/trailing stop 3× ATR · One position</span><span>Snapshot {time(snapshot?.updated_at)} · Costs modeled</span></footer>
      </section>
      <aside className="trend-sidebar">
        <div className="trend-sidebar-heading"><b>PAPER POSITION</b><span className={position?.active ? 'trend-positive' : ''}>{position?.active ? 'OPEN' : 'FLAT'}</span></div>
        <section className="trend-position-details">
          <div><span>Held SOL</span><strong>{number(position?.quantity)?.toFixed(6) ?? '—'}</strong></div>
          <div><span>Purchase cost</span><strong>{money(position?.cost_basis)} USDT</strong></div>
          <div><span>Opened</span><strong>{datetime(position?.opened_at)}</strong></div>
          {position?.residual && <small>Below-step residual remains in equity and cost basis.</small>}
          <button className="trend-close" disabled={busy || !controls.close} onClick={() => action('close')}>Close paper position</button>
        </section>
        <div className="trend-sidebar-heading"><b>RECENT SIMULATED FILLS</b><span>{fills.length ? 'Latest 6' : 'No fills yet'}</span></div>
        <div className="trend-fill-list">{fills.length ? fills.map((fill) => <div className="trend-fill" key={fill.id}>
          <div><b className={fill.side === 'buy' ? 'trend-positive' : 'trend-negative'}>{fill.side.toUpperCase()}</b><strong>{money(fill.price)}</strong><time>{time(fill.t)}</time></div>
          <div><span>{Number(fill.quantity).toFixed(6)} SOL</span><span>{fill.profit == null ? 'Inventory added' : <b className={Number(fill.profit) < 0 ? 'trend-negative' : 'trend-positive'}>{money(fill.profit, true)} USDT</b>}</span></div>
          <small>{label(fill.reason)}</small>
        </div>) : <div className="trend-empty">The bot waits for a fresh crossover. Starting the account does not immediately buy SOL.</div>}</div>
        <section className="trend-activity"><div className="trend-eyebrow">LATEST ACTIVITY</div>
          {activity.length ? activity.map((event) => <p key={event.id}><time>{time(event.time)}</time>{event.message || label(event.reason || event.action || event.kind)}</p>) : <p>Paper ledger ready. No exchange account connected.</p>}
        </section>
        <div className="trend-decision">{snapshot?.decision || 'Waiting for account status'}</div>
      </aside>
    </div>
    {settings && <div className="trend-modal-backdrop" onClick={() => setSettings(false)}><section className="trend-modal" role="dialog" aria-modal="true" aria-labelledby="trend-settings-title" onClick={(event) => event.stopPropagation()}>
      <div className="trend-modal-heading"><h2 id="trend-settings-title">Frozen paper strategy</h2><button aria-label="Close settings" onClick={() => setSettings(false)}>×</button></div>
      <dl>{[['Market', 'SOL/USDT · Spot, long only'], ['Signal', '4h EMA20/50 crossover; close above EMA200'], ['Execution', '1h decision delay; fresh observed ask/bid'], ['Exit', 'Bearish EMA state or 3× ATR14 trailing stop'], ['Planned risk', '1% of current paper equity per trade'], ['Allocation ceiling', '80% of current paper equity'], ['Costs', '0.1% commission + 0.1% adverse execution per side; observed spread'], ['Starting capital', `${money(snapshot?.policy.capital)} USDT`]].map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
      <p>The stop monitors observed quotes while this service runs, including when entries are paused. Missed entry windows are skipped. Stop execution can exceed the planned risk.</p>
      <p>These settings identify the tested baseline. This paper version has no live-trading switch.</p>
    </section></div>}
  </main>
}
