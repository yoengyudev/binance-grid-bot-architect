import { lazy, Suspense } from 'react'

const Selected = lazy(() => import.meta.env.VITE_STRATEGY_MODE === 'trend'
  ? import('./TrendApp.jsx') : import('./App.jsx'))

export default function StrategyApp() {
  return <Suspense fallback={<div style={{ background: '#080f1e', color: '#91a4c1', minHeight: '100dvh', padding: 24 }}>Connecting to dashboard…</div>}><Selected /></Suspense>
}
