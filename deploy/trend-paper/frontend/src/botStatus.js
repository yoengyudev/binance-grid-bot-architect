export class SessionRequiredError extends Error {
  constructor() {
    super('Admin session expired or missing. Sign in through Settings to continue.')
    this.name = 'SessionRequiredError'
  }
}

export async function readBotStatus(url, { signal } = {}) {
  const response = await fetch(url, { credentials: 'include', cache: 'no-store', signal })
  if (response.status === 401) throw new SessionRequiredError()
  if (!response.ok) throw new Error(`Bot status request failed (${response.status}).`)
  return response.json()
}
