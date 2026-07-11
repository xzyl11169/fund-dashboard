import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app as fund_app


CN_TZ = ZoneInfo("Asia/Shanghai")


class FundDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        fund_app.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        fund_app.init_db()
        self.context = fund_app.app.app_context()
        self.context.push()

    def tearDown(self):
        self.context.pop()
        self.temp_dir.cleanup()

    def add_position(self, code, shares=100):
        conn = fund_app.db()
        conn.execute("insert into funds (code, name) values (?, ?)", (code, code))
        conn.execute(
            """
            insert into opening_positions
              (code, as_of_date, shares, cost_amount, nav, note, created_at)
            values (?, '2026-07-08', ?, 0, 0, '', '2026-07-08T12:00:00')
            """,
            (code, shares),
        )
        conn.commit()

    def add_nav(self, code, day, nav, pct, is_official):
        source = "official" if is_official else "estimate"
        fund_app.db().execute(
            """
            insert into valuations
              (valuation_date, code, nav, pct, source, quoted_at, is_official, created_at)
            values (?, ?, ?, ?, ?, ?, ?, '2026-07-10T10:00:00')
            """,
            (day, code, nav, pct, source, f"{day} 10:00", 1 if is_official else 0),
        )
        fund_app.db().commit()

    def test_close_snapshot_keeps_latest_quote(self):
        first = {
            "date": "2026-07-10",
            "code": "000001",
            "nav": 1.01,
            "pct": 1,
            "quoted_at": "2026-07-10 14:56",
        }
        last = {**first, "nav": 1.05, "pct": 5, "quoted_at": "2026-07-10 15:00"}

        fund_app.capture_close_snapshot(first)
        fund_app.capture_close_snapshot(last)

        row = fund_app.db().execute(
            "select nav, quoted_at from close_snapshots where code='000001'"
        ).fetchone()
        self.assertEqual(row["nav"], 1.05)
        self.assertEqual(row["quoted_at"], "2026-07-10 15:00")

    def test_weekend_is_not_a_market_session(self):
        saturday = datetime(2026, 7, 11, 10, 0, tzinfo=CN_TZ)
        with patch.object(fund_app, "cn_now", return_value=saturday):
            self.assertEqual(fund_app.trading_session(), "closed")
            self.assertFalse(fund_app.market_session_started())
            self.assertFalse(fund_app.in_intraday_refresh_window())

    def test_estimate_return_only_uses_covered_positions(self):
        self.add_position("000001")
        self.add_position("000002")
        for code in ("000001", "000002"):
            self.add_nav(code, "2026-07-09", 1, 0, True)
        self.add_nav("000001", "2026-07-10", 1.1, 10, False)
        now = datetime(2026, 7, 10, 10, 0, tzinfo=CN_TZ)

        with patch.object(fund_app, "cn_now", return_value=now):
            _cards, totals = fund_app.build_summary()

        self.assertAlmostEqual(totals["estimate_today_pnl"], 10)
        self.assertAlmostEqual(totals["estimate_today_return"], 0.1)
        self.assertEqual(totals["estimate_covered_count"], 1)
        self.assertEqual(totals["position_count"], 2)
        self.assertEqual(totals["today_return"], totals["estimate_today_return"])

    def test_confirmed_total_is_explicitly_partial(self):
        self.add_position("000001")
        self.add_position("000002")
        for code in ("000001", "000002"):
            self.add_nav(code, "2026-07-09", 1, 0, True)
        self.add_nav("000001", "2026-07-10", 1.08, 8, True)
        now = datetime(2026, 7, 10, 20, 0, tzinfo=CN_TZ)

        with patch.object(fund_app, "cn_now", return_value=now):
            _cards, totals = fund_app.build_summary()

        self.assertAlmostEqual(totals["actual_today_pnl"], 8)
        self.assertAlmostEqual(totals["actual_today_return"], 0.08)
        self.assertEqual(totals["confirmed_covered_count"], 1)
        self.assertFalse(totals["actual_complete"])

    def test_premarket_hides_current_day_numbers(self):
        self.add_position("000001")
        self.add_nav("000001", "2026-07-09", 1, 0, True)
        self.add_nav("000001", "2026-07-10", 1.1, 10, False)
        now = datetime(2026, 7, 10, 9, 0, tzinfo=CN_TZ)

        with patch.object(fund_app, "cn_now", return_value=now):
            cards, totals = fund_app.build_summary()

        self.assertIsNone(totals["estimate_today_pnl"])
        self.assertIsNone(totals["actual_today_pnl"])
        self.assertEqual(cards[0]["state_label"], "未开盘")
        self.assertIsNone(cards[0]["display_pnl"])

    def test_delayed_official_nav_is_not_today_actual(self):
        self.add_position("164906")
        self.add_nav("164906", "2026-07-09", 1, 0, True)
        self.add_nav("164906", "2026-07-10", 1.1, 10, False)
        now = datetime(2026, 7, 10, 14, 0, tzinfo=CN_TZ)

        with patch.object(fund_app, "cn_now", return_value=now):
            cards, totals = fund_app.build_summary()

        self.assertIsNone(totals["actual_today_pnl"])
        self.assertIsNone(cards[0]["official_nav"])
        self.assertEqual(cards[0]["state_label"], "盘中估算")

    def test_legacy_trades_do_not_change_direct_share_baseline(self):
        self.add_position("000001")
        fund_app.db().execute(
            """
            insert into trades
              (trade_date, code, side, amount, shares, nav, fee, note, created_at)
            values ('2026-07-09', '000001', '买入', 10, 10, 1, 0, '', '2026-07-09')
            """
        )
        fund_app.db().commit()

        opening = fund_app.opening_for("000001")
        self.assertEqual(fund_app.shares_current("000001", opening), 100)


if __name__ == "__main__":
    unittest.main()
