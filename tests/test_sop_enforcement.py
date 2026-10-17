import os
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import bot_engine


class SOPEnforcementTests(unittest.TestCase):
    def test_missing_external_checks_fail_closed(self):
        with patch.object(bot_engine, 'SOP_EXTERNAL_FILE', '/tmp/definitely_missing_sop_checks.csv'):
            passed, reasons = bot_engine.evaluate_sop_external_filters('TEST', '2026-10-17')
        self.assertFalse(passed)
        self.assertTrue(any('UNKNOWN' in reason or 'No external SOP' in reason for reason in reasons))

    def test_position_size_respects_risk_budget_and_capital(self):
        with patch.object(bot_engine, 'TRADING_CAPITAL_INR', 100000), patch.object(bot_engine, 'MAX_RISK_PER_TRADE_PCT', 1.5):
            qty = bot_engine.calculate_sop_position_size(100, 95)
        self.assertEqual(qty, 300)
        self.assertLessEqual(qty * (100 - 95), 1500)
        self.assertLessEqual(qty * 100, 100000)

    def test_emergency_exit_returns_none_for_insufficient_history(self):
        hist = pd.DataFrame({'Open': [10]*10, 'High': [11]*10, 'Low': [9]*10,
                             'Close': [10]*10, 'Volume': [100]*10})
        self.assertIsNone(bot_engine.evaluate_emergency_exit(hist))


    def test_verified_external_order_catalyst_can_pass(self):
        import csv
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'checks.csv')
            row = {
                'symbol': 'TEST', 'as_of_date': '2026-10-17', 'circuit_band_pct': '10',
                'promoter_pledge_pct': '0', 'institutional_status': 'PASS',
                'surveillance_stage': 'NONE', 'sector_tailwind_status': 'PASS',
                'catalyst_status': 'PASS', 'catalyst_type': 'ORDER',
                'contract_value_pct_revenue': '15', 'yoy_profit_growth_pct': '',
                'opm_expansion_bps': '', 'guidance_status': '', 'policy_catalyst_status': '',
                'board_meeting_date': '2026-11-20', 'equity_dilution_status': 'CLEAR',
                'delivery_volume_ratio': '1.3', 'notes': 'verified test fixture'
            }
            with open(path, 'w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=bot_engine.SOP_EXTERNAL_COLUMNS)
                writer.writeheader(); writer.writerow(row)
            with patch.object(bot_engine, 'SOP_EXTERNAL_FILE', path):
                passed, reasons = bot_engine.evaluate_sop_external_filters('TEST', '2026-10-17')
            self.assertTrue(passed, reasons)

    def test_sop_technical_gate_fails_closed_on_short_history(self):
        idx = pd.bdate_range('2026-01-01', periods=60)
        close = pd.Series(np.linspace(50, 60, 60), index=idx)
        hist = pd.DataFrame({'Open': close, 'High': close+1, 'Low': close-1,
                             'Close': close, 'Volume': 10000}, index=idx)
        passed, setup, reasons, _ = bot_engine.evaluate_sop_technical_setup(hist, 60, 60)
        self.assertFalse(passed)
        self.assertEqual(setup, 'NONE')
        self.assertIn('160 daily candles', reasons[0])

    def test_fixed_stop_is_below_entry(self):
        idx = pd.bdate_range('2025-01-01', periods=40)
        close = pd.Series(np.linspace(50, 60, 40), index=idx)
        hist = pd.DataFrame({'Open': close, 'High': close+1, 'Low': close-1,
                             'Close': close, 'Volume': 10000}, index=idx)
        with patch.object(bot_engine, 'SOP_STOP_MODEL', 'FIXED'):
            stop, atr = bot_engine.calculate_sop_stop(60, hist)
        self.assertAlmostEqual(stop, 57.3, places=4)
        self.assertGreater(atr, 0)


if __name__ == '__main__':
    unittest.main()
