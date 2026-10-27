// Shared by the populated dashboard and its cold-start skeleton.
export const dashboardLayout = {
  header: 'z-20 grid flex-none grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-2 border-b border-white/5 bg-slate-900 px-3 py-2',
  ticker: 'order-2 col-span-2 flex min-w-0 items-center gap-3 lg:order-1 lg:col-span-1 lg:gap-5',
  controls: 'order-1 col-span-2 flex items-start justify-between gap-3 lg:order-2 lg:col-span-1 lg:items-center lg:justify-end',
  workspace: 'relative flex min-h-0 min-w-0 flex-1 flex-col overflow-x-hidden overflow-y-auto overscroll-contain [scrollbar-width:thin] lg:grid lg:grid-cols-[minmax(0,1fr)_22rem] lg:grid-rows-[minmax(0,1fr)] lg:overflow-hidden xl:grid-cols-[minmax(0,1fr)_24rem]',
  chartColumn: 'relative flex min-w-0 flex-none flex-col lg:min-h-0',
  chart: 'h-[300px] min-h-[300px] flex-none md:h-[380px] lg:h-auto lg:min-h-0 lg:flex-1',
  parameters: 'grid min-w-0 grid-cols-2 gap-4 px-3 py-2 md:grid-cols-4 lg:grid-cols-8',
  marketIntel: 'grid grid-cols-1 gap-x-5 gap-y-3 border-t border-white/5 bg-slate-950/30 px-3 py-2 md:grid-cols-3',
  ledger: 'z-10 flex min-w-0 w-full flex-none flex-col border-t border-white/5 bg-slate-950 lg:min-h-0 lg:border-l lg:border-t-0',
  ledgerRows: 'min-w-0 flex-none transition-opacity lg:min-h-0 lg:flex-1 lg:overflow-y-auto [scrollbar-color:#334155_transparent] [scrollbar-width:thin]',
}
