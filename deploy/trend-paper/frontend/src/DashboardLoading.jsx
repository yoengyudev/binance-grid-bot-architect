import { dashboardLayout } from './dashboardLayout.js'

export function Skeleton({ className = '' }) {
  return <span aria-hidden="true" className={`block rounded bg-white/10 motion-safe:animate-pulse ${className}`} />
}

export function InlineSpinner({ className = '' }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" className={`inline-block h-3.5 w-3.5 shrink-0 motion-safe:animate-spin ${className}`}>
      <circle cx="10" cy="10" r="7" stroke="currentColor" strokeWidth="2" opacity="0.2" />
      <path d="M10 3a7 7 0 0 1 7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

export function ActionContent({ busy, children }) {
  return <span className="inline-flex items-center justify-center gap-2">{busy && <InlineSpinner />}{children}</span>
}

export function DashboardSkeleton() {
  return (
    <div role="status" aria-live="polite" aria-busy="true" aria-label="Loading trading dashboard" className="flex h-dvh w-full flex-col overflow-hidden bg-slate-950 text-slate-200">
      <span className="sr-only">Loading the first confirmed dashboard snapshot…</span>
      <div aria-hidden="true" className={dashboardLayout.header}>
        <div className={dashboardLayout.ticker}>
          <Skeleton className="hidden h-9 w-28 shrink-0 2xl:block" />
          <div className="flex min-w-0 flex-1 items-center gap-5 overflow-hidden">
            {[44, 28, 28, 28].map((width, index) => <div key={index} className="shrink-0 space-y-2" style={{ width: `${width / 4}rem` }}><Skeleton className="h-3 w-4/5" /><Skeleton className="h-5 w-full" /></div>)}
          </div>
          <div className="flex shrink-0 items-center gap-1"><span className="w-6" /><Skeleton className="h-8 w-8 lg:hidden" /><Skeleton className="h-8 w-8" /></div>
        </div>
        <div className={dashboardLayout.controls}>
          <div className="w-36 space-y-2"><Skeleton className="h-3 w-full" /><Skeleton className="h-5 w-28" /></div>
          <div className="ml-auto w-32 space-y-2"><Skeleton className="ml-auto h-5 w-24" /><Skeleton className="h-3 w-full" /></div>
        </div>
      </div>
      <div aria-hidden="true" className={dashboardLayout.workspace}>
        <div className={dashboardLayout.chartColumn}>
          <div className={`${dashboardLayout.chart} flex flex-col p-3`}>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><Skeleton className="h-5 w-48" /><Skeleton className="h-7 w-40" /></div>
            <Skeleton className="mb-2 h-3 w-64 max-w-full" />
            <div className="relative min-h-0 flex-1 border-b border-r border-white/5 bg-[linear-gradient(to_right,rgba(255,255,255,0.035)_1px,transparent_1px),linear-gradient(to_bottom,rgba(255,255,255,0.035)_1px,transparent_1px)] bg-[size:12.5%_20%]">
              <Skeleton className="absolute right-1 top-1/4 h-3 w-12" /><Skeleton className="absolute right-1 top-1/2 h-3 w-12" /><Skeleton className="absolute right-1 top-3/4 h-3 w-12" />
            </div>
            <div className="mt-2 flex justify-between gap-4"><Skeleton className="h-3 w-48" /><Skeleton className="h-3 w-28" /></div>
          </div>
          <div className="m-3 mt-0 min-w-0 max-w-full flex-none rounded-md border border-white/5 bg-slate-900">
            <div className="border-b border-white/5 px-3 py-2"><Skeleton className="h-4 w-32" /></div>
            <div className={dashboardLayout.parameters}>
              {Array.from({ length: 8 }, (_, index) => <div key={index} className="min-w-0 space-y-2"><Skeleton className="h-3 w-3/4" /><Skeleton className="h-5 w-full" /></div>)}
            </div>
            <div className={dashboardLayout.marketIntel}>
              {Array.from({ length: 3 }, (_, index) => <div key={index} className="space-y-2"><Skeleton className="h-3 w-28" /><Skeleton className="h-5 w-4/5" /></div>)}
            </div>
            <div className="border-t border-white/5 px-3 py-2"><Skeleton className="h-3 w-72 max-w-full" /></div>
          </div>
        </div>
        <div className={dashboardLayout.ledger}>
          <div className="space-y-2 border-b border-white/5 bg-slate-900 px-3 py-2.5"><Skeleton className="h-3 w-28" /><Skeleton className="h-4 w-40" /></div>
          <div className={`${dashboardLayout.ledgerRows} lg:overflow-hidden`}>
            {['sell', 'buy'].map((side) => <div key={side} className="pb-3"><Skeleton className="mx-3 my-3 h-3 w-24" /><div className="flex justify-between bg-slate-900/60 px-3 py-2">{[1, 2, 3, 4].map((column) => <Skeleton key={column} className="h-3 w-12" />)}</div>
              {Array.from({ length: 4 }, (_, index) => <div key={index} className="flex justify-between gap-2 px-3 py-1.5 even:bg-white/[0.025]">{[1, 2, 3, 4].map((column) => <Skeleton key={column} className="h-5 flex-1" />)}</div>)}
            </div>)}
          </div>
          <div className="border-t border-white/5 px-3 py-2"><Skeleton className="h-3 w-36" /></div>
        </div>
      </div>
    </div>
  )
}
