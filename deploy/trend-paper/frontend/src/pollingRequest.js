// One request per resource. Retired responses cannot update a newer session,
// environment, or refresh, even when a transport ignores AbortSignal.
export function createPollingRequest({ load, onStart, onSuccess, onError, onFinish,
  timeoutMs, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let active = null
  let disposed = false

  function cancel() {
    if (!active) return
    const request = active
    active = null
    clearTimer(request.timer)
    request.controller.abort()
    request.rejectDeadline?.(new Error('Request cancelled.'))
    if (!disposed) onFinish?.()
  }

  function run() {
    if (disposed) return Promise.resolve()
    if (active) return active.promise
    const controller = new AbortController()
    const request = { controller, timer: null, promise: null }
    active = request
    onStart?.()
    // Race the deadline explicitly: a hung transport must not leave the UI busy
    // forever. A late result is ignored by the already-settled race.
    const deadline = new Promise((_, reject) => {
      request.rejectDeadline = reject
      request.timer = setTimer(() => {
        controller.abort()
        reject(new Error('Request timed out.'))
      }, timeoutMs)
    })
    request.promise = Promise.race([Promise.resolve().then(() => {
      if (controller.signal.aborted) throw new Error('Request cancelled.')
      return load(controller.signal)
    }), deadline])
      .then((data) => {
        if (!disposed && active === request) onSuccess?.(data)
      })
      .catch((failure) => {
        if (!disposed && active === request) onError?.(failure)
      })
      .finally(() => {
        clearTimer(request.timer)
        if (!disposed && active === request) {
          active = null
          onFinish?.()
        }
      })
    return request.promise
  }

  return { run, cancel, dispose() { disposed = true; cancel() } }
}
