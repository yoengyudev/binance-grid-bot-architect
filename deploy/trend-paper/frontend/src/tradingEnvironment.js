// Backend status is authoritative. Unknown environments never open a feed.
const SPOT_FEEDS = Object.freeze({
  TESTNET: Object.freeze({
    rest: 'https://testnet.binance.vision/api/v3',
    websocket: 'wss://stream.testnet.binance.vision/ws',
  }),
  MAINNET: Object.freeze({
    rest: 'https://api.binance.com/api/v3',
    websocket: 'wss://stream.binance.com:9443/ws',
  }),
})

export function spotFeedFor(environment) {
  return Object.hasOwn(SPOT_FEEDS, environment) ? SPOT_FEEDS[environment] : null
}
