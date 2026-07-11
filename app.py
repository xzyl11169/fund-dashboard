import json
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, g, jsonify, redirect, render_template, request, send_from_directory, url_for

try:
    import cn_stock_holidays as stock_holidays
except ImportError:  # pragma: no cover - fallback during bootstrap
    stock_holidays = None

if stock_holidays is not None:
    stock_is_trading_day = stock_holidays.meta_functions.meta_is_trading_day(stock_holidays.get_local)
else:  # pragma: no cover - fallback during bootstrap
    stock_is_trading_day = None


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("FUND_DB_PATH", APP_DIR / "fund_tracker.sqlite3"))
APP_PIN = os.environ.get("FUND_APP_PIN", "").strip()

app = Flask(__name__)
refresh_state = {"last_run": "", "last_error": "", "running": False}
history_jobs = set()
CN_TZ = ZoneInfo("Asia/Shanghai")


def cn_now():
    return datetime.now(CN_TZ)


def cn_today():
    return cn_now().date().isoformat()


def previous_trading_day(base_day=None):
    day = base_day or cn_now().date()
    day = day - timedelta(days=1)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def is_trading_day(day):
    if stock_is_trading_day is not None:
        return stock_is_trading_day(day)
    return day.weekday() < 5


def short_cn_date(value):
    if not value:
        return "--"
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        return f"{dt.month}月{dt.day}日"
    except ValueError:
        return value


def clean_number(value, precision=2):
    if value is None:
        return None
    threshold = 0.5 * (10 ** -precision)
    return 0 if abs(value) < threshold else value


@app.template_filter("money")
def money_filter(value):
    value = clean_number(value, 2)
    return "-" if value is None else f"{value:.2f}"


@app.template_filter("percent")
def percent_filter(value):
    value = clean_number(value, 2)
    return "-" if value is None else f"{value:.2f}%"


@app.template_filter("navfmt")
def nav_filter(value):
    value = clean_number(value, 4)
    return "-" if value is None else f"{value:.4f}"


@app.template_filter("pct_bg")
def pct_bg_filter(value):
    value = clean_number(value, 2) or 0
    strength = min(abs(value) / 1.5, 1)
    alpha = 0.08 + strength * 0.24
    border_alpha = 0.22 + strength * 0.34
    if value > 0:
        return f"background: rgba(194, 65, 12, {alpha:.3f}); border-color: rgba(194, 65, 12, {border_alpha:.3f});"
    if value < 0:
        return f"background: rgba(21, 128, 61, {alpha:.3f}); border-color: rgba(21, 128, 61, {border_alpha:.3f});"
    return "background: #f8fafc;"


def eastmoney_get(url, timeout=10):
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "close",
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0",
    }
    last_exc = None
    for _attempt in range(3):
        try:
            resp = requests.get(url, timeout=timeout, headers=headers)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(0.4)
    raise last_exc


def authorized():
    if not APP_PIN:
        return True
    auth = request.authorization
    return bool(auth and auth.username in {"fund", "root"} and auth.password == APP_PIN)


@app.before_request
def require_pin():
    if request.path.startswith("/static/") or request.path == "/service-worker.js":
        return None
    if authorized():
        return None
    return Response(
        "需要访问密码",
        401,
        {"WWW-Authenticate": 'Basic realm="Fund Dashboard"'},
    )


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        create table if not exists funds (
          code text primary key,
          name text not null,
          fund_type text default '',
          reference text default '',
          note text default ''
        );

        create table if not exists trades (
          id integer primary key autoincrement,
          trade_date text not null,
          code text not null,
          side text not null,
          amount real default 0,
          shares real default 0,
          nav real default 0,
          fee real default 0,
          note text default '',
          created_at text not null
        );

        create table if not exists opening_positions (
          code text primary key,
          as_of_date text not null,
          shares real not null,
          cost_amount real not null,
          nav real default 0,
          note text default '',
          created_at text not null
        );

        create table if not exists position_history (
          id integer primary key autoincrement,
          code text not null,
          as_of_date text not null,
          shares real not null,
          created_at text not null
        );

        create table if not exists valuations (
          id integer primary key autoincrement,
          valuation_date text not null,
          code text not null,
          nav real not null,
          pct real default 0,
          source text not null,
          quoted_at text default '',
          is_official integer default 0,
          created_at text not null,
          unique(valuation_date, code, source)
        );

        create table if not exists close_snapshots (
          snapshot_date text not null,
          code text not null,
          nav real not null,
          pct real default 0,
          quoted_at text default '',
          created_at text not null,
          primary key(snapshot_date, code)
        );

        create table if not exists valuation_ticks (
          id integer primary key autoincrement,
          sampled_at text not null,
          valuation_date text not null,
          code text not null,
          nav real not null,
          pct real default 0,
          source text not null,
          quoted_at text default '',
          is_official integer default 0,
          unique(code, source, quoted_at, sampled_at)
        );

        create table if not exists portfolio_ticks (
          id integer primary key autoincrement,
          sampled_at text not null,
          snapshot_date text not null,
          today_pnl real default 0,
          today_return real default 0,
          market real default 0,
          unique(sampled_at)
        );

        create table if not exists benchmark_values (
          index_code text not null,
          valuation_date text not null,
          nav real not null,
          created_at text not null,
          primary key(index_code, valuation_date)
        );

        create index if not exists idx_valuations_code_official_date
          on valuations(code, is_official, valuation_date desc, id desc);
        create index if not exists idx_ticks_code_date_official_quote
          on valuation_ticks(code, valuation_date, is_official, quoted_at, sampled_at);
        create index if not exists idx_trades_code_date_id
          on trades(code, trade_date, id);
        create index if not exists idx_position_history_code_date_created
          on position_history(code, as_of_date, created_at, id);
        """
    )
    conn.execute(
        """
        insert into position_history (code, as_of_date, shares, created_at)
        select o.code, o.as_of_date, o.shares, o.created_at
        from opening_positions o
        where not exists (
          select 1 from position_history h where h.code=o.code
        )
        """
    )
    conn.commit()
    conn.close()


def fetch_intraday(code):
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(datetime.now().timestamp() * 1000)}"
    resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    resp.encoding = resp.apparent_encoding or "utf-8"
    text = resp.text
    match = re.search(r"jsonpgz\((.*)\);?", text)
    if not match:
        raise ValueError("盘中估值接口没有返回可解析数据")
    data = json.loads(match.group(1))
    return {
        "code": data.get("fundcode", code),
        "name": data.get("name") or code,
        "date": (data.get("gztime") or datetime.now().strftime("%Y-%m-%d")).split(" ")[0],
        "nav": float(data["gsz"]),
        "pct": float(data.get("gszzl") or 0),
        "source": "天天基金盘中估算",
        "quoted_at": data.get("gztime") or "",
        "is_official": 0,
    }


def fetch_latest_official(code):
    url = f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize=1"
    headers = {"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, timeout=8, headers=headers)
    data = resp.json()
    rows = ((data.get("Data") or {}).get("LSJZList") or [])
    if not rows:
        raise ValueError("正式净值接口没有返回数据")
    row = rows[0]
    return {
        "code": code,
        "name": code,
        "date": row["FSRQ"],
        "nav": float(row["DWJZ"]),
        "pct": float(row.get("JZZZL") or 0),
        "source": "东方财富正式净值",
        "quoted_at": row["FSRQ"],
        "is_official": 1,
    }


def fetch_eastmoney_fund_profile(code):
    url = f"https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?m=1&key={code}"
    headers = {"Referer": "https://fund.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, timeout=8, headers=headers)
    data = resp.json()
    rows = data.get("Datas") or []
    for row in rows:
        if str(row.get("CODE") or row.get("FCODE") or row.get("_id") or "") == code:
            name = row.get("NAME") or ((row.get("FundBaseInfo") or {}).get("SHORTNAME"))
            if name:
                return {"code": code, "name": name, "source": "Eastmoney fund search"}
    raise ValueError("fund profile not found")


def fetch_official_history(code, years=3):
    headers = {"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    cutoff = shift_years(cn_now().date(), years).isoformat()
    items = []
    for page in range(1, 80):
        url = f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex={page}&pageSize=20"
        resp = requests.get(url, timeout=10, headers=headers)
        data = resp.json()
        rows = ((data.get("Data") or {}).get("LSJZList") or [])
        if not rows:
            break
        stop = False
        for row in rows:
            try:
                item_date = row["FSRQ"]
                items.append(
                    {
                        "code": code,
                        "name": code,
                        "date": item_date,
                        "nav": float(row["DWJZ"]),
                        "pct": float(row.get("JZZZL") or 0),
                        "source": "东方财富正式净值",
                        "quoted_at": item_date,
                        "is_official": 1,
                    }
                )
                if item_date <= cutoff:
                    stop = True
            except (KeyError, TypeError, ValueError):
                continue
        if stop:
            break
    return items


def fetch_hs300_intraday():
    day_rows = fetch_hs300_daily_remote((cn_now().date() - timedelta(days=10)).isoformat())
    pre_close = 0
    today_text = cn_today()
    for row in reversed(day_rows):
        if row["date"] < today_text:
            pre_close = row["nav"]
            break
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000300"
    resp = requests.get(url, timeout=10, headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    data = (((resp.json().get("data") or {}).get("sh000300") or {}).get("data") or {}).get("data") or []
    points = []
    for item in data:
        parts = item.split()
        if len(parts) < 2:
            continue
        try:
            price = float(parts[1])
            pct = ((price / pre_close) - 1) * 100 if pre_close else 0
            points.append({"label": f"{parts[0][:2]}:{parts[0][2:]}", "value": pct})
        except ValueError:
            continue
    return points


def fetch_hs300_daily_remote(beg_date):
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000300,day,,,900,qfq"
    resp = requests.get(url, timeout=10, headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    data = (((resp.json().get("data") or {}).get("sh000300") or {}).get("day") or [])
    rows = []
    for item in data:
        if len(item) < 3:
            continue
        try:
            if item[0] >= beg_date:
                rows.append({"date": item[0], "nav": float(item[2])})
        except (TypeError, ValueError):
            continue
    return rows


def upsert_benchmark_values(index_code, rows):
    if not rows:
        return
    now_text = datetime.now().isoformat(timespec="seconds")
    db().executemany(
        """
        insert into benchmark_values (index_code, valuation_date, nav, created_at)
        values (?, ?, ?, ?)
        on conflict(index_code, valuation_date) do update set
          nav=excluded.nav,
          created_at=excluded.created_at
        """,
        [(index_code, row["date"], row["nav"], now_text) for row in rows],
    )
    db().commit()


def ensure_hs300_history():
    cutoff = shift_years(cn_now().date(), 3).isoformat()
    row = db().execute(
        """
        select min(valuation_date) as min_date, count(*) as count
        from benchmark_values
        where index_code='HS300'
        """
    ).fetchone()
    if row and row["count"] and row["min_date"] <= cutoff:
        return
    upsert_benchmark_values("HS300", fetch_hs300_daily_remote(cutoff))


def hs300_daily_series(beg_date):
    rows = db().execute(
        """
        select valuation_date, nav
        from benchmark_values
        where index_code='HS300' and valuation_date>=?
        order by valuation_date
        """,
        (beg_date,),
    ).fetchall()
    return [{"date": r["valuation_date"], "nav": r["nav"]} for r in rows]


def fetch_fund_profile(code):
    try:
        item = fetch_intraday(code)
        return {
            "code": item["code"],
            "name": item["name"],
            "source": "天天基金基金信息",
        }
    except Exception:
        try:
            return fetch_eastmoney_fund_profile(code)
        except Exception:
            return {"code": code, "name": code, "source": "未自动识别"}


def update_fund_name_from_profile(code):
    profile = fetch_fund_profile(code)
    row = db().execute("select name from funds where code=?", (code,)).fetchone()
    if profile["name"] and profile["name"] != code and (not row or not row["name"] or row["name"] == code):
        db().execute("update funds set name=? where code=?", (profile["name"], code))
        db().commit()
    return profile


def upsert_valuation(item):
    db().execute(
        """
        insert into valuations (valuation_date, code, nav, pct, source, quoted_at, is_official, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(valuation_date, code, source) do update set
          nav=excluded.nav,
          pct=excluded.pct,
          quoted_at=excluded.quoted_at,
          is_official=excluded.is_official,
          created_at=excluded.created_at
        """,
        (
            item["date"],
            item["code"],
            item["nav"],
            item["pct"],
            item["source"],
            item["quoted_at"],
            item["is_official"],
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db().commit()


def upsert_valuations(items):
    if not items:
        return
    now_text = datetime.now().isoformat(timespec="seconds")
    db().executemany(
        """
        insert into valuations (valuation_date, code, nav, pct, source, quoted_at, is_official, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(valuation_date, code, source) do update set
          nav=excluded.nav,
          pct=excluded.pct,
          quoted_at=excluded.quoted_at,
          is_official=excluded.is_official,
          created_at=excluded.created_at
        """,
        [
            (
                item["date"],
                item["code"],
                item["nav"],
                item["pct"],
                item["source"],
                item["quoted_at"],
                item["is_official"],
                now_text,
            )
            for item in items
        ],
    )
    db().commit()


def insert_valuation_tick(item):
    sampled_at = cn_now().isoformat(timespec="seconds")
    db().execute(
        """
        insert or ignore into valuation_ticks
          (sampled_at, valuation_date, code, nav, pct, source, quoted_at, is_official)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sampled_at,
            item["date"],
            item["code"],
            item["nav"],
            item["pct"],
            item["source"],
            item["quoted_at"] or sampled_at,
            item["is_official"],
        ),
    )
    db().commit()


def should_capture_close_snapshot(item):
    now = cn_now()
    if item.get("date") != now.date().isoformat():
        return False
    quoted_at = item.get("quoted_at") or ""
    try:
        quoted_time = datetime.strptime(quoted_at, "%Y-%m-%d %H:%M").time()
    except ValueError:
        return now.time() >= dt_time(14, 55)
    return quoted_time >= dt_time(14, 55)


def capture_close_snapshot(item):
    db().execute(
        """
        insert into close_snapshots (snapshot_date, code, nav, pct, quoted_at, created_at)
        values (?, ?, ?, ?, ?, ?)
        on conflict(snapshot_date, code) do update set
          nav=excluded.nav,
          pct=excluded.pct,
          quoted_at=excluded.quoted_at,
          created_at=excluded.created_at
        where coalesce(excluded.quoted_at, '') >= coalesce(close_snapshots.quoted_at, '')
        """,
        (
            item["date"],
            item["code"],
            item["nav"],
            item["pct"],
            item["quoted_at"],
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db().commit()


def market_minutes(now=None):
    now = now or cn_now()
    return now.hour * 60 + now.minute


def trading_session(now=None):
    now = now or cn_now()
    if not is_trading_day(now.date()):
        return "closed"
    minutes = market_minutes(now)
    if minutes < 9 * 60 + 30:
        return "premarket"
    if minutes <= 11 * 60 + 30:
        return "morning"
    if minutes < 13 * 60:
        return "lunch"
    if minutes <= 15 * 60 + 5:
        return "afternoon"
    return "postmarket"


def in_intraday_refresh_window(now=None):
    now = now or cn_now()
    if not is_trading_day(now.date()):
        return False
    minutes = market_minutes(now)
    return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60 + 5)


def market_session_started(now=None):
    now = now or cn_now()
    return is_trading_day(now.date()) and market_minutes(now) >= 9 * 60 + 30


def next_refresh_sleep(now=None):
    now = now or cn_now()
    minutes = market_minutes(now)
    if 9 * 60 + 20 <= minutes < 9 * 60 + 30:
        target = now.replace(hour=9, minute=30, second=0, microsecond=0)
        return max(5, int((target - now).total_seconds()))
    if 9 * 60 + 30 <= minutes <= 11 * 60 + 30:
        return 180
    if 11 * 60 + 30 < minutes < 13 * 60:
        target = now.replace(hour=13, minute=0, second=0, microsecond=0)
        return max(60, int((target - now).total_seconds()))
    if 13 * 60 <= minutes <= 15 * 60 + 5:
        return 180
    return 600


def insert_portfolio_tick(totals):
    sampled_at = cn_now().isoformat(timespec="seconds")
    db().execute(
        """
        insert or ignore into portfolio_ticks
          (sampled_at, snapshot_date, today_pnl, today_return, market)
        values (?, ?, ?, ?, ?)
        """,
        (
            sampled_at,
            cn_today(),
            totals.get("today_pnl") or 0,
            totals.get("today_return") or 0,
            totals.get("market") or 0,
        ),
    )
    db().commit()


def refresh_all():
    codes = [r["code"] for r in db().execute("select code from funds").fetchall()]
    errors = []
    fetch_intraday_now = in_intraday_refresh_window()
    for code in codes:
        if fetch_intraday_now:
            try:
                intraday = fetch_intraday(code)
                upsert_valuation(intraday)
                insert_valuation_tick(intraday)
                if should_capture_close_snapshot(intraday):
                    capture_close_snapshot(intraday)
            except Exception as exc:
                errors.append(f"{code} 盘中估值失败: {exc}")
        try:
            update_fund_name_from_profile(code)
        except Exception as exc:
            errors.append(f"{code} 名称更新失败: {exc}")
        try:
            upsert_valuation(fetch_latest_official(code))
        except Exception as exc:
            errors.append(f"{code} 正式净值失败: {exc}")
    try:
        _cards, totals = build_summary()
        insert_portfolio_tick(totals)
    except Exception as exc:
        errors.append(f"组合快照失败: {exc}")
    refresh_state["last_run"] = cn_now().strftime("%Y-%m-%d %H:%M:%S")
    refresh_state["last_error"] = "；".join(errors[-4:])


def background_refresh():
    if refresh_state["running"]:
        return
    refresh_state["running"] = True

    def loop():
        while True:
            try:
                with app.app_context():
                    init_db()
                    refresh_all()
            except Exception as exc:
                refresh_state["last_error"] = str(exc)
            time.sleep(next_refresh_sleep())

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


def cash_flow(row):
    side = row["side"]
    amount = row["amount"] or 0
    fee = row["fee"] or 0
    if side == "买入":
        return -(amount + fee)
    if side in ("赎回", "现金分红"):
        return amount - fee
    return 0


def opening_for(code):
    return db().execute("select * from opening_positions where code=?", (code,)).fetchone()


def shares_delta(row, fallback_nav=None):
    side = row["side"]
    shares = row["shares"] or 0
    amount = row["amount"] or 0
    nav = row["nav"] or fallback_nav or 0
    if side in ("买入", "赎回") and not shares and nav:
        shares = amount / nav
    if side == "赎回":
        return -shares
    if side in ("买入", "分红再投"):
        return shares
    return 0


def latest_nav(code, on_or_before=None):
    params = [code]
    where = "code=?"
    if on_or_before:
        where += " and valuation_date<=?"
        params.append(on_or_before)
    row = db().execute(
        f"""
        select * from valuations
        where {where}
        order by valuation_date desc, is_official desc, id desc
        limit 1
        """,
        params,
    ).fetchone()
    return row


def latest_nav_by_type(code, is_official):
    return db().execute(
        """
        select * from valuations
        where code=? and is_official=?
        order by valuation_date desc, id desc
        limit 1
        """,
        (code, 1 if is_official else 0),
    ).fetchone()


def official_nav_on(code, valuation_date):
    return db().execute(
        """
        select * from valuations
        where code=? and is_official=1 and valuation_date=?
        order by id desc
        limit 1
        """,
        (code, valuation_date),
    ).fetchone()


def previous_official_nav(code, before_date):
    return db().execute(
        """
        select * from valuations
        where code=? and is_official=1 and valuation_date<?
        order by valuation_date desc, id desc
        limit 1
        """,
        (code, before_date),
    ).fetchone()


def close_snapshot(code, snapshot_date):
    return db().execute(
        """
        select snapshot_date as valuation_date, code, nav, pct, quoted_at
        from close_snapshots
        where code=? and snapshot_date=?
        limit 1
        """,
        (code, snapshot_date),
    ).fetchone()


def previous_nav(code, before_date):
    return db().execute(
        """
        select * from valuations
        where code=? and valuation_date<?
        order by valuation_date desc, is_official desc, id desc
        limit 1
        """,
        (code, before_date),
    ).fetchone()


def trades_for(code, until=None, before=None):
    params = [code]
    where = "code=?"
    if until:
        where += " and trade_date<=?"
        params.append(until)
    if before:
        where += " and trade_date<?"
        params.append(before)
    return db().execute(f"select * from trades where {where} order by trade_date, id", params).fetchall()


def shares_before(code, before_date, opening, fallback_nav=0):
    if not opening:
        return 0
    return opening["shares"] if opening["as_of_date"] < before_date else 0


def shares_current(code, opening, fallback_nav=0):
    return opening["shares"] if opening else 0


def confirmed_shares_current(code, opening, fallback_nav=0):
    return shares_before(code, cn_today(), opening, fallback_nav)


def confirmed_trades_after_opening(code, opening):
    return []


def pending_trades_today(code, opening):
    return []


def row_pnl(shares, current_nav, base_nav):
    if not current_nav or not base_nav:
        return None
    return shares * (current_nav["nav"] - base_nav["nav"])


def confirmed_pnl_for_latest_official(code, opening, fallback_nav=0):
    official = latest_nav_by_type(code, True)
    if not official:
        return None
    opening_date = opening["as_of_date"] if opening else ""
    if opening_date and opening_date >= official["valuation_date"]:
        return None
    base = previous_official_nav(code, official["valuation_date"])
    shares = shares_before(code, official["valuation_date"], opening, base["nav"] if base else fallback_nav)
    return row_pnl(shares, official, base)


def build_summary():
    today = cn_today()
    session = trading_session()
    trading_today = session != "closed"
    market_started = market_session_started()
    funds = db().execute("select * from funds order by code").fetchall()
    cards = []
    totals = {
        "market": 0,
        "net_invested": 0,
        "pnl": 0,
        "today_pnl": 0,
        "today_base": 0,
        "estimate_today_pnl": 0,
        "estimate_today_base": 0,
        "actual_today_pnl": None,
        "actual_today_base": 0,
        "blended_today_pnl": 0,
        "blended_today_base": 0,
        "official_updated_count": 0,
        "fund_count": len(funds),
        "position_count": 0,
        "estimate_covered_count": 0,
        "confirmed_covered_count": 0,
        "up_count": 0,
        "down_count": 0,
        "flat_count": 0,
        "session": session,
    }
    for fund in funds:
        code = fund["code"]
        nav = latest_nav(code)
        estimate_nav = latest_nav_by_type(code, False)
        latest_official_nav = latest_nav_by_type(code, True)
        today_estimate_nav = estimate_nav if estimate_nav and estimate_nav["valuation_date"] == today else None
        today_official_nav = official_nav_on(code, today)
        subject_date = today
        official_nav = today_official_nav
        prev_official = previous_official_nav(code, subject_date)
        prev_prev_official = previous_official_nav(code, prev_official["valuation_date"]) if prev_official else None
        today_official_ready = bool(official_nav and official_nav["valuation_date"] == today)
        state = "B" if today_official_ready else "A"
        close_nav = close_snapshot(code, subject_date)
        if not close_nav and today_estimate_nav:
            close_nav = today_estimate_nav

        use_close_estimate = session == "postmarket" or state == "B"
        estimate_display_nav = close_nav if use_close_estimate and close_nav else today_estimate_nav
        display_nav = official_nav if state == "B" else estimate_display_nav
        display_pct = display_nav["pct"] if display_nav else 0
        nav_value = display_nav["nav"] if display_nav else (nav["nav"] if nav else 0)
        market_nav_value = latest_official_nav["nav"] if latest_official_nav else 0
        opening = opening_for(code)
        shares = confirmed_shares_current(code, opening, nav_value)
        pending_rows = []
        pending_shares = 0
        pending_amount = 0
        net_invested = 0
        market = shares * market_nav_value
        pnl = 0

        subject_shares = shares_before(code, subject_date, opening, prev_official["nav"] if prev_official else nav_value)
        yesterday_shares = shares_before(code, prev_official["valuation_date"], opening, prev_prev_official["nav"] if prev_prev_official else nav_value) if prev_official else 0

        estimate_pnl = row_pnl(subject_shares, today_estimate_nav, prev_official)
        close_pnl = row_pnl(subject_shares, close_nav, prev_official)
        estimate_display_pnl = close_pnl if use_close_estimate and close_nav else estimate_pnl
        today_pnl = row_pnl(subject_shares, official_nav, prev_official)
        yesterday_pnl = row_pnl(yesterday_shares, prev_official, prev_prev_official)
        if today_official_ready:
            totals["official_updated_count"] += 1
        if shares > 0:
            totals["position_count"] += 1
        premarket_mode = (not trading_today or not market_started) and (not today_official_ready)
        if today_official_ready:
            confirmed_pnl = today_pnl
            confirmed_pnl_label = "今日确认盈亏"
        else:
            confirmed_pnl = None
            confirmed_pnl_label = "确认盈亏"
        display_pnl = today_pnl if today_official_ready else estimate_pnl
        if premarket_mode:
            confirmed_pnl = None
            confirmed_pnl_label = "确认盈亏"
            estimate_display_nav = None
            display_pct = 0
            display_pnl = None
            estimate_display_pnl = None
            estimate_pnl = None
        today_base = subject_shares * prev_official["nav"] if prev_official else 0

        totals["market"] += market
        totals["net_invested"] += net_invested
        totals["pnl"] += pnl
        if display_pnl is not None:
            totals["blended_today_pnl"] += display_pnl
            totals["blended_today_base"] += today_base
        if estimate_display_pnl is not None:
            totals["estimate_today_pnl"] += estimate_display_pnl
            totals["estimate_today_base"] += today_base
            if shares > 0:
                totals["estimate_covered_count"] += 1
        if today_official_ready and today_pnl is not None:
            totals["actual_today_pnl"] = (totals["actual_today_pnl"] or 0) + today_pnl
            totals["actual_today_base"] += today_base
            if shares > 0:
                totals["confirmed_covered_count"] += 1
        if display_nav and display_pct > 0:
            totals["up_count"] += 1
        elif display_nav and display_pct < 0:
            totals["down_count"] += 1
        elif display_nav:
            totals["flat_count"] += 1
        cards.append(
            {
                "fund": fund,
                "shares": shares,
                "net_invested": net_invested,
                "market": market,
                "pending_shares": pending_shares,
                "pending_amount": pending_amount,
                "pending_count": len(pending_rows),
                "pnl": pnl,
                "return_rate": pnl / net_invested if net_invested else 0,
                "today_pnl": today_pnl,
                "confirmed_pnl": confirmed_pnl,
                "confirmed_pnl_label": confirmed_pnl_label,
                "display_pnl": display_pnl,
                "display_pct": display_pct,
                "display_nav": display_nav,
                "estimate_display_nav": estimate_display_nav,
                "estimate_display_pnl": estimate_display_pnl,
                "latest_nav": nav,
                "estimate_nav": estimate_nav,
                "official_nav": official_nav,
                "latest_official_nav": latest_official_nav,
                "prev_nav": prev_official,
                "prev_prev_nav": prev_prev_official,
                "close_nav": close_nav,
                "premarket_mode": premarket_mode,
                "state": state,
                "state_label": (
                    "净值复盘"
                    if state == "B"
                    else "休市"
                    if not trading_today
                    else "未开盘"
                    if not market_started
                    else "盘中估算"
                ),
                "state_note": (
                    "东方财富转发的已披露净值"
                    if state == "B"
                    else "天天基金盘中估算，非基金公司官方数据"
                ),
                "subject_date": subject_date,
                "subject_date_label": short_cn_date(subject_date),
                "estimate_pnl": estimate_pnl,
                "close_pnl": close_pnl,
                "yesterday_pnl": yesterday_pnl,
                "source_note": "天天基金盘中估算，非官方" if state == "A" else "东方财富转发的已披露净值",
                "nav_status": "已披露净值" if state == "B" else "盘中估算",
                "date_status": short_cn_date(subject_date),
                "opening": opening,
            }
        )
    totals["return_rate"] = totals["pnl"] / totals["net_invested"] if totals["net_invested"] else 0
    intraday_estimates = portfolio_estimated_intraday_series(today)
    if intraday_estimates:
        latest_intraday_estimate = intraday_estimates[-1]
        totals["estimate_today_pnl"] = latest_intraday_estimate["today_pnl"]
        totals["estimate_today_return"] = (
            latest_intraday_estimate["today_return"] / 100
            if latest_intraday_estimate["today_return"] is not None
            else None
        )
    else:
        totals["estimate_today_return"] = (
            totals["estimate_today_pnl"] / totals["estimate_today_base"]
            if totals["estimate_today_base"] and totals["estimate_covered_count"]
            else None
        )
    if not totals["estimate_covered_count"]:
        totals["estimate_today_pnl"] = None
    totals["actual_today_return"] = (
        totals["actual_today_pnl"] / totals["actual_today_base"]
        if totals["actual_today_pnl"] is not None and totals["actual_today_base"]
        else None
    )
    totals["blended_today_return"] = (
        totals["blended_today_pnl"] / totals["blended_today_base"]
        if totals["blended_today_base"]
        else None
    )
    totals["today_pnl"] = totals["estimate_today_pnl"]
    totals["today_return"] = totals["estimate_today_return"]
    totals["estimate_complete"] = (
        totals["position_count"] > 0
        and totals["estimate_covered_count"] == totals["position_count"]
    )
    totals["actual_complete"] = (
        totals["position_count"] > 0
        and totals["confirmed_covered_count"] == totals["position_count"]
    )
    return cards, totals


def latest_date_label(is_official):
    row = db().execute(
        """
        select max(valuation_date) as latest_date
        from valuations
        where is_official=?
        """,
        (1 if is_official else 0,),
    ).fetchone()
    return short_cn_date(row["latest_date"] if row else "")


def ensure_official_history(code):
    cutoff = shift_years(cn_now().date(), 3).isoformat()
    row = db().execute(
        """
        select min(valuation_date) as min_date, max(valuation_date) as max_date, count(*) as count
        from valuations
        where code=? and is_official=1
        """,
        (code,),
    ).fetchone()
    if row and row["count"] and row["min_date"] <= cutoff:
        return
    upsert_valuations(fetch_official_history(code))


def official_history_ready(code):
    cutoff = shift_years(cn_now().date(), 3).isoformat()
    row = db().execute(
        """
        select min(valuation_date) as min_date, count(*) as count
        from valuations
        where code=? and is_official=1
        """,
        (code,),
    ).fetchone()
    return bool(row and row["count"] and row["min_date"] <= cutoff)


def hs300_history_ready():
    cutoff = shift_years(cn_now().date(), 3).isoformat()
    row = db().execute(
        """
        select min(valuation_date) as min_date, count(*) as count
        from benchmark_values
        where index_code='HS300'
        """
    ).fetchone()
    return bool(row and row["count"] and row["min_date"] <= cutoff)


def warm_detail_history_async(code):
    job_key = f"detail:{code}"
    if job_key in history_jobs:
        return
    history_jobs.add(job_key)

    def worker():
        try:
            with app.app_context():
                init_db()
                ensure_official_history(code)
                ensure_hs300_history()
                refresh_state["last_run"] = cn_now().strftime("%Y-%m-%d %H:%M:%S")
        finally:
            history_jobs.discard(job_key)

    threading.Thread(target=worker, daemon=True).start()


def intraday_series(code, day):
    rows = db().execute(
        """
        select sampled_at, quoted_at, nav, pct
        from valuation_ticks
        where code=? and valuation_date=? and is_official=0
        order by quoted_at, sampled_at
        """,
        (code, day),
    ).fetchall()
    series = []
    for r in rows:
        time_text = (r["quoted_at"] or r["sampled_at"])[11:16] if (r["quoted_at"] or r["sampled_at"]) else ""
        if is_market_chart_time(time_text):
            series.append({"time": time_text, "nav": r["nav"], "pct": r["pct"]})
    if not series:
        latest_estimate = db().execute(
            """
            select quoted_at, nav, pct
            from valuations
            where code=? and valuation_date=? and is_official=0
            order by id desc
            limit 1
            """,
            (code, day),
        ).fetchone()
        if latest_estimate:
            quoted_at = latest_estimate["quoted_at"] or ""
            time_text = quoted_at[11:16] if len(quoted_at) >= 16 else ""
            if is_market_chart_time(time_text):
                series.append(
                    {
                        "time": time_text,
                        "nav": latest_estimate["nav"],
                        "pct": latest_estimate["pct"],
                    }
                )
    return series


def is_market_chart_time(time_text):
    if not time_text or ":" not in time_text:
        return False
    try:
        hour, minute = [int(part) for part in time_text.split(":", 1)]
    except ValueError:
        return False
    minutes = hour * 60 + minute
    return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)


def portfolio_intraday_series(day):
    if day == cn_today() and not market_session_started():
        return []
    estimated_series = portfolio_estimated_intraday_series(day)
    if estimated_series:
        return estimated_series
    rows = db().execute(
        """
        select sampled_at, today_pnl, today_return, market
        from portfolio_ticks
        where snapshot_date=?
        order by sampled_at
        """,
        (day,),
    ).fetchall()
    series = []
    for r in rows:
        time_text = r["sampled_at"][11:16]
        if is_market_chart_time(time_text):
            series.append(
                {
                    "time": time_text,
                    "today_pnl": r["today_pnl"],
                    "today_return": (r["today_return"] or 0) * 100,
                    "market": r["market"],
                }
            )
    return series


def portfolio_estimated_intraday_series(day):
    funds = db().execute("select * from funds order by code").fetchall()
    fund_rows = []
    all_times = set()
    for fund in funds:
        code = fund["code"]
        opening = opening_for(code)
        latest = latest_nav(code)
        fallback_nav = latest["nav"] if latest else 0
        prev_official = previous_official_nav(code, day)
        shares = shares_before(code, day, opening, prev_official["nav"] if prev_official else fallback_nav)
        if not shares:
            continue
        base_nav = prev_official or latest_nav_by_type(code, True)
        ticks = db().execute(
            """
            select sampled_at, quoted_at, nav
            from valuation_ticks
            where code=? and valuation_date=? and is_official=0
            order by quoted_at, sampled_at
            """,
            (code, day),
        ).fetchall()
        points = []
        seen = set()
        for tick in ticks:
            raw_time = (tick["quoted_at"] or tick["sampled_at"] or "")[11:16]
            if not is_market_chart_time(raw_time):
                continue
            seen.add(raw_time)
            points.append({"time": raw_time, "nav": tick["nav"]})
        if not points:
            continue
        all_times.update(seen)
        fund_rows.append(
            {
                "shares": shares,
                "base_nav": base_nav["nav"] if base_nav else fallback_nav,
                "pnl_base_nav": prev_official["nav"] if prev_official else None,
                "points": points,
            }
        )
    if not fund_rows or not all_times:
        return []
    series = []
    for time_text in sorted(all_times):
        market_total = 0
        pnl_total = 0
        pnl_base_total = 0
        for row in fund_rows:
            nav = row["base_nav"]
            for point in row["points"]:
                if point["time"] <= time_text:
                    nav = point["nav"]
                else:
                    break
            market_total += row["shares"] * nav
            if row["pnl_base_nav"]:
                pnl_total += row["shares"] * (nav - row["pnl_base_nav"])
                pnl_base_total += row["shares"] * row["pnl_base_nav"]
        series.append(
            {
                "time": time_text,
                "today_pnl": pnl_total,
                "today_return": (pnl_total / pnl_base_total * 100) if pnl_base_total else None,
                "market": market_total,
            }
        )
    return series


def official_series(code, limit=120):
    rows = db().execute(
        """
        select valuation_date, nav, pct
        from valuations
        where code=? and is_official=1
        order by valuation_date desc
        limit ?
        """,
        (code, limit),
    ).fetchall()
    return [
        {"date": r["valuation_date"], "nav": r["nav"], "pct": r["pct"]}
        for r in reversed(rows)
    ]


def shift_years(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def range_start(range_key, today):
    if range_key == "1m":
        return today - timedelta(days=31)
    if range_key == "6m":
        return today - timedelta(days=183)
    if range_key == "1y":
        return today - timedelta(days=366)
    if range_key == "3y":
        return shift_years(today, 3)
    if range_key == "ytd":
        return date(today.year, 1, 1)
    return today


def normalize_points(rows, label_key="date"):
    if not rows:
        return []
    base = rows[0]["nav"]
    if not base:
        return []
    return [
        {
            "label": r[label_key],
            "value": ((r["nav"] / base) - 1) * 100,
        }
        for r in rows
        if r.get("nav") is not None
    ]


def build_compare_ranges(code, today_text):
    today_obj = datetime.strptime(today_text, "%Y-%m-%d").date()
    range_defs = [
        ("today", "当日"),
        ("1m", "近一月"),
        ("6m", "近半年"),
        ("1y", "近一年"),
        ("3y", "近三年"),
        ("ytd", "今年以来"),
    ]
    official_rows = official_series(code, 900)
    compare = {}

    intraday = intraday_series(code, today_text)
    fund_today = [{"label": r["time"], "value": r["pct"]} for r in intraday if r.get("time")]
    compare["today"] = {"fund": fund_today, "benchmark": []}

    min_start = range_start("3y", today_obj).isoformat()
    hs300_rows = hs300_daily_series(min_start)
    for key, _label in range_defs[1:]:
        start = range_start(key, today_obj).isoformat()
        fund_rows = [r for r in official_rows if r["date"] >= start]
        last_fund_date = fund_rows[-1]["date"] if fund_rows else today_text
        benchmark_rows = [r for r in hs300_rows if start <= r["date"] <= last_fund_date]
        compare[key] = {
            "fund": normalize_points(fund_rows),
            "benchmark": normalize_points(benchmark_rows),
        }
    return [{"key": key, "label": label, **compare[key]} for key, label in range_defs]


@app.route("/")
def index():
    init_db()
    cards, totals = build_summary()
    sort = request.args.get("sort", "today_pnl_desc")
    sorters = {
        "today_pnl_desc": lambda c: (c["display_pnl"] or 0, c["market"]),
        "today_pnl_asc": lambda c: (-(c["display_pnl"] or 0), -c["market"]),
        "market_desc": lambda c: (c["market"], c["display_pnl"] or 0),
        "market_asc": lambda c: (-c["market"], -(c["display_pnl"] or 0)),
        "pct_desc": lambda c: (c["display_pct"], c["display_pnl"] or 0),
        "pct_asc": lambda c: (-c["display_pct"], -(c["display_pnl"] or 0)),
        "code_asc": lambda c: tuple(-ord(ch) for ch in c["fund"]["code"]),
    }
    cards = sorted(cards, key=sorters.get(sort, sorters["today_pnl_desc"]), reverse=True)
    funds = db().execute("select * from funds order by code").fetchall()
    edit_code = request.args.get("edit_opening", "").strip()
    edit_opening = opening_for(edit_code) if edit_code else None
    return render_template(
        "index.html",
        cards=cards,
        totals=totals,
        funds=funds,
        sort=sort,
        edit_code=edit_code,
        edit_opening=edit_opening,
        today=cn_today(),
        default_opening_date=previous_trading_day().isoformat(),
        refresh_state=refresh_state,
    )


@app.get("/funds/<code>")
def fund_detail(code):
    init_db()
    code = code.strip()
    fund = db().execute("select * from funds where code=?", (code,)).fetchone()
    if not fund:
        return redirect(url_for("index"))
    history_error = ""
    if not official_history_ready(code) or not hs300_history_ready():
        warm_detail_history_async(code)
        history_error = "历史数据正在后台补齐，页面会先显示已有缓存；稍后自动刷新后会更完整。"
    today = cn_today()
    return render_template(
        "detail.html",
        fund=fund,
        today=today,
        compare_ranges=build_compare_ranges(code, today),
        official_history=official_series(code),
        latest_nav=latest_nav(code),
        history_error=history_error,
        refresh_state=refresh_state,
    )


@app.get("/today")
def today_detail():
    init_db()
    today = cn_today()
    cards, totals = build_summary()
    return render_template(
        "today.html",
        today=today,
        series=portfolio_intraday_series(today),
        totals=totals,
        refresh_state=refresh_state,
    )


@app.get("/api/state")
def api_state():
    return jsonify(refresh_state)


@app.post("/funds")
def add_fund():
    code = request.form["code"].strip()
    typed_name = request.form["name"].strip()
    profile = fetch_fund_profile(code) if not typed_name else {"name": typed_name}
    db().execute(
        "insert or replace into funds (code, name, fund_type, reference, note) values (?, ?, ?, ?, ?)",
        (
            code,
            typed_name or profile["name"] or code,
            request.form.get("fund_type", "").strip(),
            request.form.get("reference", "").strip(),
            request.form.get("note", "").strip(),
        ),
    )
    db().commit()
    return redirect(url_for("index"))


@app.get("/api/fund/<code>")
def fund_profile(code):
    return jsonify(fetch_fund_profile(code.strip()))


@app.post("/trades")
def add_trade():
    amount = float(request.form.get("amount") or 0)
    shares = float(request.form.get("shares") or 0)
    nav = float(request.form.get("nav") or 0)
    if not shares and amount and nav and request.form["side"] in ("买入", "赎回"):
        shares = amount / nav
    db().execute(
        """
        insert into trades (trade_date, code, side, amount, shares, nav, fee, note, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request.form["trade_date"],
            request.form["code"],
            request.form["side"],
            amount,
            shares,
            nav,
            float(request.form.get("fee") or 0),
            request.form.get("note", ""),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db().commit()
    return redirect(url_for("index"))


@app.post("/opening")
def save_opening():
    code = request.form["code"].strip()
    if not db().execute("select code from funds where code=?", (code,)).fetchone():
        profile = fetch_fund_profile(code)
        db().execute(
            "insert into funds (code, name, fund_type, reference, note) values (?, ?, '', '', '')",
            (code, profile["name"] or code),
        )
    now_text = datetime.now().isoformat(timespec="seconds")
    db().execute(
        """
        insert into opening_positions (code, as_of_date, shares, cost_amount, nav, note, created_at)
        values (?, ?, ?, ?, ?, ?, ?)
        on conflict(code) do update set
          as_of_date=excluded.as_of_date,
          shares=excluded.shares,
          cost_amount=excluded.cost_amount,
          nav=excluded.nav,
          note=excluded.note,
          created_at=excluded.created_at
        """,
        (
            code,
            request.form["as_of_date"],
            float(request.form.get("shares") or 0),
            float(request.form.get("cost_amount") or 0),
            float(request.form.get("nav") or 0),
            request.form.get("note", ""),
            now_text,
        ),
    )
    db().execute(
        """
        insert into position_history (code, as_of_date, shares, created_at)
        values (?, ?, ?, ?)
        """,
        (
            code,
            request.form["as_of_date"],
            float(request.form.get("shares") or 0),
            now_text,
        ),
    )
    db().commit()
    return redirect(url_for("index"))


@app.post("/funds/<code>/delete")
def delete_fund(code):
    code = code.strip()
    db().execute("delete from trades where code=?", (code,))
    db().execute("delete from opening_positions where code=?", (code,))
    db().execute("delete from valuations where code=?", (code,))
    db().execute("delete from valuation_ticks where code=?", (code,))
    db().execute("delete from close_snapshots where code=?", (code,))
    db().execute("delete from funds where code=?", (code,))
    db().commit()
    return redirect(url_for("index"))


@app.post("/refresh")
def refresh():
    refresh_all()
    return redirect(url_for("index"))


@app.get("/service-worker.js")
def service_worker():
    return send_from_directory(app.static_folder, "service-worker.js")


if __name__ == "__main__":
    init_db()
    background_refresh()
    host = os.environ.get("FUND_APP_HOST", "0.0.0.0")
    port = int(os.environ.get("FUND_APP_PORT", "8765"))
    app.run(host=host, port=port, debug=False, use_reloader=False)
