import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from diagnose_stop_event import label_trades, read_journal


class StopEventDiagnosticTests(unittest.TestCase):
    def test_journal_extracts_trigger_without_inventing_ticker_history(self):
        start = datetime(2026, 9, 30, 7, 19, tzinfo=timezone.utc)
        entries = [
            {"__REALTIME_TIMESTAMP": str(int((start + timedelta(seconds=48.213)).timestamp() * 1_000_000)),
             "MESSAGE": "2026-09-30 07:19:48,213 CRITICAL Hard stop triggered at 76581.05 BTC/USDT; liquidating bot inventory."},
            {"__REALTIME_TIMESTAMP": str(int((start + timedelta(seconds=49)).timestamp() * 1_000_000)),
             "MESSAGE": "Unrelated order update at 76581.05"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in entries), encoding="utf-8")
            events = read_journal(start, start + timedelta(minutes=2), path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "LIQUIDATION TRIGGER")
        self.assertEqual(events[0]["price"], Decimal("76581.05"))

    def test_trade_labels_follow_price_crossings(self):
        start = datetime(2026, 9, 30, 7, 19, tzinfo=timezone.utc)
        events = [
            {"time": start + timedelta(seconds=1), "source": "Testnet trade", "price": Decimal("83000"), "event": "TRADE"},
            {"time": start + timedelta(seconds=2), "source": "Testnet trade", "price": Decimal("81947.58"), "event": "TRADE"},
            {"time": start + timedelta(seconds=56), "source": "Testnet trade", "price": Decimal("81972.87"), "event": "TRADE"},
        ]
        label_trades(events, Decimal("81948.20"))
        self.assertEqual([event["event"] for event in events], ["ABOVE", "BREACH", "RECOVERY"])


if __name__ == "__main__":
    unittest.main()
