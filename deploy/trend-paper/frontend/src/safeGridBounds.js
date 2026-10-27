// BTC/USDT prices in this form use the exchange's 0.01 USDT price tick.
const PRICE_SCALE = 100
const positive = (value) => Number.isFinite(value) && value > 0

export function validateSafeGridBounds({ center, ceiling, stop, activeStop, requireActiveStop = false }) {
  const projectedLowerBound = positive(center) && positive(ceiling) ? 2 * center - ceiling : null
  const activeStopUnavailable = requireActiveStop && !positive(activeStop)
  const effectiveStop = Math.max(positive(activeStop) ? activeStop : 0, positive(stop) ? stop : 0)
  const lowerBoundUnsafe = projectedLowerBound !== null && effectiveStop > 0
    && Math.round(projectedLowerBound * PRICE_SCALE) / PRICE_SCALE <= effectiveStop
  const stopBelowFloor = positive(stop) && positive(activeStop) && stop < activeStop
  let message = ''
  if (activeStopUnavailable) message = 'Active hard stop unavailable. Wait for the bot status before saving.'
  else if (![center, ceiling, stop].every(positive)) message = 'Enter valid positive center, ceiling, and hard-stop prices.'
  else if (![center, ceiling, stop].every((value) => Number.isSafeInteger(Math.round(value * PRICE_SCALE)))) message = 'Prices are too large to validate safely.'
  else if (stopBelowFloor) message = 'The hard stop cannot be lower than the active floor. Recalculate while retaining the active floor.'
  else if (!(ceiling > center && center > stop)) message = 'Prices must satisfy Ceiling > Center > Hard Stop.'
  else if (lowerBoundUnsafe) message = 'Projected Lower Bound must be above both the active and entered hard stops after price rounding. Review the center and ceiling; the active stop cannot be lowered.'
  return { valid: !message, message, projectedLowerBound, effectiveStop,
    lowerBoundUnsafe, stopBelowFloor, activeStopUnavailable }
}
