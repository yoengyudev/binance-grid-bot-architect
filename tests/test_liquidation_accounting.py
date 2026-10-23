import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock, patch

from database import GridDatabase
from execution_history import ExecutionHistoryPending
from models import TradingHalt
from repair_liquidation_fees import repair_trade


class LiquidationAccountingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = GridDatabase(Path(folder.name)/'grid_testnet.sqlite')
        self.client = 'gridbot-audited-stop'
        self.db.record_trade('82521.14312913907284768211921',
                             '68892.86397350993377483443709', '-82.7309189984',
                             sell_order_id=self.client, sold_base='0.00604', fee_basis='estimated')
        self.exchange = Mock(has={'fetchOrderTrades': True})
        self.exchange.fetch_order.return_value = dict(
            id='5836609', clientOrderId=self.client, symbol='BTC/USDT', type='market',
            side='sell', status='closed', filled='0.00604', cost='416.1128984',
            average='68892.86397350993377483443709', fee=None, fees=[])
        self.exchange.fetch_order_trades.return_value = [dict(
            id='1122210', order='5836609', symbol='BTC/USDT',
            info=dict(qty='0.00604', quoteQty='416.1128984',
                      commission='0', commissionAsset='USDT'))]

    def repair(self, **kwargs):
        return repair_trade(self.db, self.exchange, 'BTC/USDT', self.client, **kwargs)

    def test_dry_run_and_idempotent_apply_preserve_timestamp_basis_and_sale(self):
        before = self.db.get_trade_by_sell_order_id(self.client)
        preview = self.repair()
        self.assertEqual(self.db.get_trade_by_sell_order_id(self.client), before)
        self.assertEqual(D(preview['profit']), D('-82.3148061'))
        self.repair(apply=True)
        after = self.db.get_trade_by_sell_order_id(self.client)
        for field in ('timestamp', 'sold_base', 'buy_price', 'sell_price', 'id'):
            self.assertEqual(before[field], after[field])
        self.assertEqual(D(after['profit']), D('-82.3148061'))
        snapshot = json.loads(self.db.get_state('fill_snapshot:'+self.client))
        self.assertEqual(snapshot['fees'], [{'currency': 'USDT', 'cost': '0'}])
        self.assertTrue(self.repair(apply=True)['already_corrected'])
        self.assertEqual(len(self.db.fetch_trade_history()), 1)
        self.assertEqual(len(self.db.fetch_transition_journal()), 1)

    def test_nonzero_quote_fee_is_deducted(self):
        self.exchange.fetch_order_trades.return_value[0]['info']['commission'] = '0.2'
        result = self.repair(apply=True)
        self.assertEqual(D(result['profit']), D('-82.5148061'))

    def test_quantity_or_cost_mismatch_does_not_rewrite_history(self):
        before = self.db.fetch_trade_history()
        self.exchange.fetch_order_trades.return_value[0]['info']['quoteQty'] = '417'
        with self.assertRaises(TradingHalt):
            self.repair(apply=True)
        self.assertEqual(self.db.fetch_trade_history(), before)

    def test_incomplete_history_and_third_asset_fees_remain_unmodified(self):
        before = self.db.fetch_trade_history()
        with patch.object(self.exchange, 'fetch_order_trades', return_value=[]):
            with self.assertRaises(ExecutionHistoryPending):
                self.repair(apply=True)
        self.exchange.fetch_order_trades.return_value[0]['info'].update(
            commission='0.001', commissionAsset='BNB')
        with self.assertRaises(TradingHalt):
            self.repair(apply=True)
        self.assertEqual(self.db.fetch_trade_history(), before)

    def test_normal_grid_order_is_excluded(self):
        self.db.insert_order(self.client, 1, 'SELL', '70000', '0.00604', client_order_id=self.client)
        with self.assertRaises(TradingHalt):
            self.repair(apply=True)

    def test_fenced_repair_and_failed_snapshot_leave_no_partial_correction(self):
        before = self.db.fetch_trade_history()
        with self.assertRaisesRegex(TradingHalt, 'lease'):
            self.repair(apply=True, before_commit=Mock(side_effect=TradingHalt('lease lost')))
        self.assertEqual(self.db.fetch_trade_history(), before)
        with patch('repair_liquidation_fees.save_fill_snapshot', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                self.repair(apply=True)
        self.assertEqual(self.db.fetch_trade_history(), before)
        self.assertIsNone(self.db.get_state('liquidation_fee_correction:'+self.client))
        self.assertEqual(self.db.fetch_transition_journal(), [])
