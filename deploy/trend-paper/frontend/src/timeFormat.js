// Use the browser's local timezone and explicit AM/PM for every clock display.
const CLOCK_OPTIONS = { hour: '2-digit', minute: '2-digit', hour12: true }
const DATE_OPTIONS = { year: 'numeric', month: 'short', day: '2-digit' }

function validDate(value) {
  if (value == null || value === '') return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatTime(value, { seconds = true } = {}) {
  const date = validDate(value)
  return date ? date.toLocaleTimeString('en-US', {
    ...CLOCK_OPTIONS, ...(seconds ? { second: '2-digit' } : {}),
  }) : '—'
}

export function formatDateTime(value, { seconds = true } = {}) {
  const date = validDate(value)
  return date ? date.toLocaleString('en-US', {
    ...DATE_OPTIONS, ...CLOCK_OPTIONS, ...(seconds ? { second: '2-digit' } : {}),
  }) : '—'
}

export function formatChartTime(time) {
  // Lightweight Charts uses UNIX seconds, while Date uses milliseconds.
  if (typeof time === 'number') return formatDateTime(time * 1000, { seconds: false })
  const date = validDate(typeof time === 'string' ? time
    : time && Date.UTC(time.year, time.month - 1, time.day))
  // Business-day chart values have no clock or timezone component.
  return date ? date.toLocaleDateString('en-US', { ...DATE_OPTIONS, timeZone: 'UTC' }) : '—'
}
