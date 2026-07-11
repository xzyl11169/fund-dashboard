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
        fund_app.update_refresh_state(
            last_run="",
            last_error="",
            running=False,
            completed=0,
            total=0,
            phase="",
            started_at="",
        )

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

    def test_opening_requires_csrf_and_records_history(self):
        self.add_position("000001")
        client = fund_app.app.test_client()

        missing = client.post(
            "/opening",
            data={"code": "000001", "as_of_date": "2026-07-09", "shares": "123.45"},
        )
        self.assertEqual(missing.status_code, 400)

        client.get("/")
        with client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        saved = client.post(
            "/opening",
            data={
                "_csrf_token": token,
                "code": "000001",
                "as_of_date": "2026-07-09",
                "shares": "123.45",
            },
        )
        self.assertEqual(saved.status_code, 302)
        opening = fund_app.opening_for("000001")
        self.assertEqual(opening["shares"], 123.45)
        history_rows = fund_app.db().execute(
            "select shares from position_history where code='000001' order by id"
        ).fetchall()
        self.assertEqual([row["shares"] for row in history_rows], [100, 123.45])

    def test_invalid_opening_is_rejected(self):
        client = fund_app.app.test_client()
        client.get("/")
        with client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = client.post(
            "/opening",
            data={
                "_csrf_token": token,
                "code": "bad-code",
                "as_of_date": "not-a-date",
                "shares": "-1",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(fund_app.db().execute("select count(*) from funds").fetchone()[0], 0)

    def test_only_one_refresh_can_run(self):
        self.assertTrue(fund_app.begin_refresh())
        self.assertFalse(fund_app.begin_refresh())
        fund_app.update_refresh_state(running=False)

    def test_device_access_link_sets_persistent_cookie(self):
        client = fund_app.app.test_client()
        with (
            patch.object(fund_app, "APP_TOKEN", "device-secret"),
            patch.object(fund_app, "APP_PIN", ""),
        ):
            self.assertEqual(client.get("/").status_code, 401)
            unlocked = client.get("/?access=device-secret")
            self.assertEqual(unlocked.status_code, 302)
            self.assertIn("fund_access=", unlocked.headers.get("Set-Cookie", ""))
            self.assertEqual(client.get("/").status_code, 200)

    def test_market_refresh_reuses_intraday_name_data(self):
        self.add_position("000001")
        fund_app.db().execute("update funds set name='测试基金' where code='000001'")
        fund_app.db().commit()
        self.add_nav("000001", "2026-07-09", 1, 0, True)
        now = datetime(2026, 7, 10, 10, 0, tzinfo=CN_TZ)
        calls = {"intraday": 0, "profile": 0, "official": 0}

        def fake_intraday(code):
            calls["intraday"] += 1
            return {
                "code": code,
                "name": code,
                "date": "2026-07-10",
                "nav": 1.1,
                "pct": 10,
                "source": "estimate",
                "quoted_at": "2026-07-10 10:00",
                "is_official": 0,
            }

        def fake_profile(code):
            calls["profile"] += 1
            return {"code": code, "name": code}

        def fake_official(code):
            calls["official"] += 1
            raise AssertionError("盘中后台刷新不应重复查询正式净值")

        with (
            patch.object(fund_app, "cn_now", return_value=now),
            patch.object(fund_app, "fetch_intraday", side_effect=fake_intraday),
            patch.object(fund_app, "fetch_fund_profile", side_effect=fake_profile),
            patch.object(fund_app, "fetch_latest_official", side_effect=fake_official),
        ):
            fund_app.refresh_all()

        self.assertEqual(calls, {"intraday": 1, "profile": 0, "official": 0})


if __name__ == "__main__":
    unittest.main()
