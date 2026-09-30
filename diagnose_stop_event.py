"""Read-only timeline of bot journal events and Binance Spot Testnet trades.

The bot historically saved only its latest ticker price, not every poll. Public
aggregate trades are independent market evidence, not a replay of bot ticks.
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


TESTNET_AGG_TRADES_URL = "https://testnet.binance.vision/api/v3/aggTrades"
TRIGGER_RE = re.compile(r"Hard stop triggered at ([0-9.]+) BTC/USDT")
TRAIL_RE = re.compile(r"Trailing hard stop raised to ([0-9.]+) after new high ([0-9.]+)")
SOLD_RE = re.compile(r"Hard stop sold ([0-9.Ee+-]+) BTC")
TICKER_RE = re.compile(r"(?:ticker|last price)[:= ]+([0-9]+(?:\.[0-9]+)?)", re.I)


def parse_time(value):
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Time must include Z or a UTC offset")
    return timestamp.astimezone(timezone.utc)


def format_time(timestamp):
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def extract_bot_event(timestamp, message):
    trigger = TRIGGER_RE.search(message)
    if trigger:
        return {"time": timestamp, "source": "bot journal", "price": Decimal(trigger[1]),
                "event": "LIQUIDATION TRIGGER", "detail": message}
    trail = TRAIL_RE.search(message)
    if trail:
        return {"time": timestamp, "source": "bot journal", "price": Decimal(trail[2]),
                "event": "TRAILING HIGH", "detail": "new floor " + trail[1]}
    sold = SOLD_RE.search(message)
    if sold:
        return {"time": timestamp, "source": "bot journal", "price": None,
                "event": "MARKET SELL CONFIRMED", "detail": sold[1] + " BTC"}
    ticker = TICKER_RE.search(message)
    if ticker:
        return {"time": timestamp, "source": "bot log", "price": Decimal(ticker[1]),
                "event": "RECORDED TICKER", "detail": message}
    return None


def read_journal(start, end, json_file=None, unit="binance-grid-bot.service"):
    if json_file:
        lines = Path(json_file).read_text(encoding="utf-8").splitlines()
    else:
        command = ["journalctl", "-u", unit, "--since", start.isoformat(),
                   "--until", end.isoformat(), "--output=json", "--no-pager"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "journalctl failed")
        lines = result.stdout.splitlines()
    events = []
    for line in lines:
        try:
            entry = json.loads(line)
            timestamp = datetime.fromtimestamp(
                int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000, timezone.utc
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if start <= timestamp <= end:
            event = extract_bot_event(timestamp, str(entry.get("MESSAGE", "")))
            if event:
                events.append(event)
    return events


def fetch_testnet_trades(start, end, max_pages=25):
    """Fetch every aggregate trade in the window, paging beyond Binance's 1000 cap."""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    params = {"symbol": "BTCUSDT", "startTime": start_ms,
              "endTime": end_ms, "limit": 1000}
    events = []
    last_id = None
    for _ in range(max_pages):
        url = TESTNET_AGG_TRADES_URL + "?" + urlencode(params)
        with urlopen(url, timeout=15) as response:
            page = json.load(response)
        if not isinstance(page, list):
            raise RuntimeError("Testnet aggregate-trade response is not a list")
        for trade in page:
            trade_id = int(trade["a"])
            if last_id is not None and trade_id <= last_id:
                raise RuntimeError("Testnet trade pagination did not advance")
            last_id = trade_id
            timestamp_ms = int(trade["T"])
            if start_ms <= timestamp_ms <= end_ms:
                events.append({
                    "time": datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc),
                    "source": "Testnet trade", "price": Decimal(trade["p"]),
                    "event": "TRADE", "detail": "aggregate trade ID " + str(trade_id),
                })
        if len(page) < 1000 or not page or int(page[-1]["T"]) >= end_ms:
            return events
        params = {"symbol": "BTCUSDT", "fromId": last_id + 1, "limit": 1000}
    raise RuntimeError("Page limit reached before the end of the window; report incomplete")


def label_trades(events, floor):
    prior_below = None
    for event in sorted((e for e in events if e["source"] == "Testnet trade"),
                        key=lambda e: e["time"]):
        below = event["price"] < floor
        event["event"] = ("BREACH" if prior_below is False else "BELOW") if below else (
            "RECOVERY" if prior_below is True else "ABOVE")
        prior_below = below


def summarize(events, floor):
    triggers = [e for e in events if e["event"] == "LIQUIDATION TRIGGER"]
    trades = sorted((e for e in events if e["source"] == "Testnet trade"),
                    key=lambda e: e["time"])
    print("Hard-stop floor:", floor, "USDT")
    if triggers:
        print("Bot trigger:", format_time(triggers[0]["time"]),
              triggers[0]["price"], "USDT")
    else:
        print("Bot trigger: not recorded")
    print("Recorded bot ticker observations:", sum(e["source"] != "Testnet trade" and
          e["price"] is not None for e in events))
    print("Public Testnet aggregate trades:", len(trades))
    if triggers and trades:
        trigger = triggers[0]
        matches = [e for e in trades if e["price"] == trigger["price"] and
                   abs((e["time"] - trigger["time"]).total_seconds()) <= 2]
        if matches:
            match = min(matches, key=lambda e: abs((e["time"] - trigger["time"]).total_seconds()))
            print("Matching public trade:", format_time(match["time"]), match["price"], "USDT")
    breaches = [e for e in trades if e["event"] == "BREACH"]
    if breaches:
        breach = breaches[0]
        recovery = next((e for e in trades if e["time"] > breach["time"] and
                         e["price"] >= floor), None)
        print("First public breach:", format_time(breach["time"]), breach["price"], "USDT")
        if recovery:
            print("First public recovery:", format_time(recovery["time"]),
                  recovery["price"], "USDT")
            print("Trade-price time below floor:",
                  f"{(recovery['time'] - breach['time']).total_seconds():.3f} seconds")
        else:
            print("First public recovery: not in selected window")


def write_csv(path, events, floor):
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_utc", "source", "price_usdt", "below_floor", "event", "detail"])
        for event in events:
            price = event["price"]
            writer.writerow([format_time(event["time"]), event["source"],
                             "" if price is None else str(price),
                             "" if price is None else str(price < floor).lower(),
                             event["event"], event["detail"]])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="ISO time with timezone")
    parser.add_argument("--end", required=True, help="ISO time with timezone")
    parser.add_argument("--floor", required=True, type=Decimal, help="Hard-stop USDT price")
    parser.add_argument("--journal-json", help="Saved journalctl -o json output")
    parser.add_argument("--skip-journal", action="store_true")
    parser.add_argument("--testnet-trades", action="store_true",
                        help="Include public BTCUSDT Spot Testnet aggregate trades")
    parser.add_argument("--csv", help="Save the full timeline to this CSV path")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        start, end = parse_time(args.start), parse_time(args.end)
        if end <= start or not args.floor.is_finite() or args.floor <= 0:
            raise ValueError("Require end after start and a positive floor")
        if args.testnet_trades and (end - start).total_seconds() > 300:
            raise ValueError("Testnet trade windows are limited to five minutes")
        events = [] if args.skip_journal else read_journal(start, end, args.journal_json)
        if args.testnet_trades:
            events.extend(fetch_testnet_trades(start, end))
        label_trades(events, args.floor)
        events.sort(key=lambda e: (e["time"], e["source"]))
        summarize(events, args.floor)
        print("Note: public trades are not the bot's full ticker-poll history.")
        if args.csv:
            write_csv(args.csv, events, args.floor)
            print("Saved full timeline:", args.csv)
        if not args.summary_only:
            print("\nUTC time                 | Source        | Price USDT      | Event                   | Detail")
            print("-" * 110)
            for event in events:
                price = event["price"]
                print(f"{format_time(event['time']):24} | {event['source']:13} | "
                      f"{str(price) if price is not None else '':15} | "
                      f"{event['event']:23} | {event['detail']}")
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print("Diagnostic failed:", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
