# BTC/USDT Spot Testnet hard-stop event — 2026-09-30

Window: 07:19:00–07:21:00 UTC (14:19:00–14:21:00 Phnom Penh). Active trailing floor: **81,948.20 USDT**.

| UTC | Source | Observation |
| --- | --- | --- |
| 07:19:46.729 | Binance Spot Testnet aggregate trade 2008493 | First trade below floor: **81,947.58 USDT** |
| 07:19:47.947 | Binance Spot Testnet aggregate trade 2008904 | **76,581.05 USDT** |
| 07:19:48.213 | Bot systemd journal | Hard stop triggered at **76,581.05 USDT** |
| 07:20:27.184 | Bot systemd journal | Market sale confirmed for **0.0012 BTC** |
| 07:20:40.772 | Binance Spot Testnet aggregate trade 2009506 | First trade back above floor: **81,972.87 USDT** |

The Testnet last-traded price was below the floor for **54.043 seconds**, measured from the first below-floor trade to the first above-floor trade in this window. A five-second confirmation based on the last-traded price would not have filtered this event.

The bot's historic journal contains the trigger price and sale confirmation, but **not every ticker poll**. SQLite's `last_market_price` is overwritten. The 1,364 Testnet aggregate trades in the [CSV](2026-09-30-hard-stop.csv) are independent market observations; they are not a replay of the bot's ticker responses. Binance can aggregate multiple executions into one aggregate trade. The dashboard's separate public Mainnet WebSocket could not show this Testnet move.

Reproduce with `diagnose_stop_event.py` and the command in [README.md](../README.md). The saved CSV has SHA-256 `b4eaeaa307db6b8ee1c916f37b4a5f283c530e3ff5a407a4145edd0bfac32fb7`.
