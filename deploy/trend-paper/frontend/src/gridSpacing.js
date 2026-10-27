// Gross SELL/BUY return. Commissions, inventory losses and stop slippage are separate.
export const MIN_GRID_RETURN = 0.004
const tick = 0.01

export function catastrophicStop(lower) {
  if (!Number.isFinite(lower) || lower <= 0) return NaN
  return Math.floor((lower * 0.85 + 1e-9) * 100) / 100
}

export function fixedGridStop(center, ceiling, retainedStop = null) {
  const lower = 2 * center - ceiling
  if (!(Number.isFinite(center) && ceiling > center && lower > 0)) return NaN
  return Math.max(catastrophicStop(lower), Number(retainedStop) || 0)
}

export function validateGridSpacing({ center, ceiling, levels, minimum = MIN_GRID_RETURN }) {
  if (!(Number.isFinite(center) && center > 0 && Number.isFinite(ceiling)
    && ceiling > center && 2 * center > ceiling && Number.isSafeInteger(levels)
    && levels >= 2 && levels <= 100)) {
    return { valid: false, minimumGrossPercent: null, message: 'Enter valid bounds and 2–100 grid levels.' }
  }
  const anchor = Math.round(center * 100)
  const lower = 2 * center - ceiling
  const returns = []
  let valid = true
  for (const [bound, count] of [[lower, Math.ceil(levels / 2)], [ceiling, Math.floor(levels / 2)]]) {
    const ratio = (bound / center) ** (1 / count)
    let previous = anchor
    for (let i = 1; i <= count; i++) {
      const price = Math.round((i === count ? bound : center * ratio ** i) * 100)
      const low = Math.min(previous, price), high = Math.max(previous, price)
      if (![low, high].every(Number.isSafeInteger) || low <= 0) {
        return { valid: false, minimumGrossPercent: null, message: 'Prices cannot be validated safely.' }
      }
      // Integer ticks and a conservative rational ratio defend rounded prices.
      if (BigInt(high) * 1000000000000n < BigInt(low) * BigInt(Math.ceil((1 + minimum) * 1e12))) valid = false
      returns.push((high - low) / low * 100)
      previous = price
    }
  }
  return { valid, minimumGrossPercent: Math.min(...returns),
    message: valid ? '' : 'Fee defense requires at least 0.4% gross return per grid level. Calculate Grid (ATR) applies the production baseline.' }
}

export function feeDefendedBounds(center, requestedHalfWidth, levels, minimum = MIN_GRID_RETURN) {
  if (!(Number.isFinite(center) && center > 0 && Number.isFinite(requestedHalfWidth)
    && requestedHalfWidth > 0 && Number.isSafeInteger(levels) && levels >= 2 && levels <= 100)) {
    throw new Error('Enter a positive center and 2–100 grid levels before calculating bounds.')
  }
  const nb = Math.ceil(levels / 2), ns = Math.floor(levels / 2)
  const required = Math.max(center * (1 - (1 + minimum) ** -nb),
    center * ((1 + minimum) ** ns - 1))
  let width = Math.max(requestedHalfWidth, required)
  if (width <= required + tick) width += 4 * Math.max(nb, ns) * tick
  let widthTicks = Math.ceil(width * 100 - 1e-7)
  for (let attempt = 0; attempt < 256; attempt++, widthTicks++) {
    const ceiling = (Math.round(center * 100) + widthTicks) / 100
    if (ceiling >= 2 * center) break
    if (validateGridSpacing({ center, ceiling, levels, minimum }).valid) {
      return { ceiling, lower: 2 * center - ceiling }
    }
  }
  throw new Error('The requested count cannot fit defended spacing within positive bounds.')
}

export function compressedAtrBounds(center, atr, levels) {
  if (!(Number.isFinite(atr) && atr > 0) || levels !== 5) {
    throw new Error('The production baseline requires closed 1h ATR and exactly 5 levels.')
  }
  return feeDefendedBounds(center, tick, levels, Math.max(0.5 * atr / center, MIN_GRID_RETURN))
}
