import unittest
import pandas as pd
import numpy as np

from bot_engine import evaluate_swing_entry, calculate_atr, trading_sessions_held, rotating_scan_batch


class SwingStrategyTests(unittest.TestCase):
    def make_history(self):
        n = 50
        dates = pd.bdate_range("2026-07-01", periods=n)
        close = np.linspace(80, 110, n)
        high = close + 0.25
        low = close - 0.25
        # Last completed candle has not closed above the previous 20-session high;
        # the live quote in the test is the breakout.
        close[-1] = close[-2] - 0.2
        high[-1] = close[-1] + 0.25
        low[-1] = close[-1] - 0.25
        volume = np.full(n, 100_000.0)
        volume[-1] = 150_000.0
        return pd.DataFrame({"Open": close, "High": high, "Low": low,
                             "Close": close, "Volume": volume}, index=dates)

    def test_breakout_setup_passes_all_filters(self):
        hist = self.make_history()
        passed, details = evaluate_swing_entry(hist, current_price=125.0, rsi=60.0)
        self.assertTrue(passed, details)

    def test_rsi_outside_range_fails(self):
        hist = self.make_history()
        passed, _ = evaluate_swing_entry(hist, current_price=125.0, rsi=75.0)
        self.assertFalse(passed)

    def test_rotating_batch_wraps_and_covers_universe(self):
        import bot_engine
        original = bot_engine._read_scan_cursor
        try:
            bot_engine._read_scan_cursor = lambda n: 4
            batch, start = rotating_scan_batch(["A", "B", "C", "D", "E", "F"], 4)
            self.assertEqual(start, 4)
            self.assertEqual(batch, ["E", "F", "A", "B"])
        finally:
            bot_engine._read_scan_cursor = original

    def test_atr_positive(self):
        self.assertGreater(calculate_atr(self.make_history()), 0)

    def test_holding_period_counts_entry_session(self):
        hist = self.make_history()
        # During entry session, today's partial candle is excluded from history.
        next_session = (hist.index[-1] + pd.offsets.BDay(1)).date()
        self.assertEqual(trading_sessions_held(hist, f"{next_session} 10:00:00"), 1)
        # On the next session, the completed entry-day candle is present in history.
        self.assertEqual(trading_sessions_held(hist, f"{hist.index[-1].date()} 10:00:00"), 2)


if __name__ == "__main__":
    unittest.main()
