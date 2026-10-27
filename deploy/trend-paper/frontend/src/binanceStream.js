const STALE_AFTER_MS = 15_000
const INITIAL_BACKOFF_MS = 1_000
const MAX_BACKOFF_MS = 30_000

// onMessage returns true only for a valid update for the requested stream.
export function connectBinanceStream(url, { onMessage, onStateChange }) {
  let active = true
  let socket = null
  let reconnectTimer
  let watchdogTimer
  let attempts = 0

  function retireSocket() {
    window.clearTimeout(watchdogTimer)
    const retired = socket
    socket = null
    if (!retired) return
    retired.onmessage = retired.onerror = retired.onclose = null
    retired.close()
  }

  function reconnect() {
    if (!active) return
    retireSocket()
    onStateChange('Reconnecting')
    // Equal jitter avoids synchronized retries and keeps the delay <= 30s.
    const ceiling = Math.min(MAX_BACKOFF_MS, INITIAL_BACKOFF_MS * 2 ** Math.min(attempts, 5))
    attempts += 1
    const delay = ceiling * (0.5 + Math.random() * 0.5)
    window.clearTimeout(reconnectTimer)
    reconnectTimer = window.setTimeout(connect, delay)
  }

  function armWatchdog() {
    window.clearTimeout(watchdogTimer)
    watchdogTimer = window.setTimeout(reconnect, STALE_AFTER_MS)
  }

  function connect() {
    if (!active) return
    try {
      socket = new WebSocket(url)
    } catch {
      reconnect()
      return
    }
    const connection = socket
    // Also recover a handshake that never opens or produces a message.
    armWatchdog()
    connection.onmessage = (event) => {
      if (!active || socket !== connection) return
      if (onMessage(event) !== true) return
      attempts = 0
      armWatchdog()
      onStateChange('Live')
    }
    connection.onerror = connection.onclose = () => {
      if (active && socket === connection) reconnect()
    }
  }

  onStateChange('Connecting')
  connect()
  return () => {
    active = false
    window.clearTimeout(reconnectTimer)
    retireSocket()
  }
}
