# -*- coding: utf-8 -*-
"""
Pre-match signals bot:
- Daily (08:00 Europe/Warsaw): scan today's fixtures in API-Football and send signals to Telegram
  Strategy: 
    H2H last 3 meetings: each had total >= 3
    EACH team's last 3 matches: at least 2 with total >= 3
    -> Signal: OVER 2.5 (try to attach odds from /odds?fixture=)
- Daily report at 23:30 (Europe/Warsaw): resolves results for today's signals and computes PnL
  Rule win: final total >= 3
- Weekly report Sun 23:50; Monthly report last day of month 23:50
- Render-compatible: tiny Flask HTTP server binds $PORT so Web Service deploys fine
"""

import os
import json
import time
import pytz
import logging
from datetime import datetime, timedelta, date
from threading import Thread

import requests
import telebot
from flask import Flask

# ===================== Settings & Secrets =====================

API_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")      # Telegram Bot Token
CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID")        # may be int/string
API_KEY     = os.getenv("API_FOOTBALL_KEY")        # API-Football key
TIMEZONE    = os.getenv("TZ", "Europe/Warsaw")     # local timezone

# optional filter – comma separated league ids (e.g. "39,140,61")
LEAGUE_FILTER = os.getenv("LEAGUE_IDS", "").strip()  # '' means all leagues
LEAGUE_SET = set(s.strip() for s in LEAGUE_FILTER.split(",") if s.strip())

REQUEST_TIMEOUT = 15
STORAGE_FILE    = "signals.json"
LOG_FILE        = "bot.log"

# safety
if not API_TOKEN or not CHAT_ID_RAW or not API_KEY:
    raise SystemExit("❌ Need TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY in env")

try:
    CHAT_ID = int(CHAT_ID_RAW)
except Exception:
    CHAT_ID = CHAT_ID_RAW  # allow @channelusername or string id

# ===================== Logging =====================

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("prematch-bot")

# ===================== TeleBot & HTTP session =====================

bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})

# ===================== Flask for Render =====================

app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ===================== Helpers =====================

def now_local():
    return datetime.now(pytz.timezone(TIMEZONE))

def send(msg: str):
    try:
        bot.send_message(CHAT_ID, msg)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def load_store():
    if not os.path.exists(STORAGE_FILE):
        return {"days": {}}  # {"days": {"YYYY-MM-DD":[{...}]}}
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"load_store error: {e}")
        return {"days": {}}

def save_store(data):
    try:
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_store error: {e}")

def api_get(url, params=None):
    try:
        r = API.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"GET {url} error: {e}")
        return {}

def total_goals_of_fixture(m):
    """return (home, away, total) for /fixtures item m"""
    try:
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return gh, ga, (gh + ga)
    except Exception:
        return 0, 0, 0

def is_finished_status(short):
    # finished statuses in API-Football: FT, AET, PEN, etc.
    return short in ("FT", "AET", "PEN")

# ===================== Strategy checks =====================

def h2h_three_all_over3(home_id, away_id):
    """Last 3 H2H must each have total >= 3."""
    url = "https://v3.football.api-sports.io/fixtures/headtohead"
    data = api_get(url, {"h2h": f"{home_id}-{away_id}", "last": 3})
    resp = data.get("response") or []
    if len(resp) < 3:
        return False  # need exactly last 3 to be present
    for m in resp:
        _, _, tot = total_goals_of_fixture(m)
        if tot < 3:
            return False
    return True

def team_last3_at_least2_over3(team_id):
    """Team's last 3 matches: at least 2 have total >= 3."""
    url = "https://v3.football.api-sports.io/fixtures"
    data = api_get(url, {"team": team_id, "last": 3})
    resp = data.get("response") or []
    if len(resp) < 3:
        return False
    count_over3 = 0
    for m in resp:
        _, _, tot = total_goals_of_fixture(m)
        if tot >= 3:
            count_over3 += 1
    return count_over3 >= 2

def try_get_odds_over25(fixture_id):
    """Try to get odds for O2.5. Not all plans have odds – return 'n/a' if not found."""
    try:
        url = "https://v3.football.api-sports.io/odds"
        data = api_get(url, {"fixture": fixture_id})
        resp = data.get("response") or []
        # structure can be complex: bookmakers -> bets -> values
        # We'll look for bet name like 'Over/Under' and value 'Over 2.5'
        for item in resp:
            for book in item.get("bookmakers", []):
                for bet in book.get("bets", []):
                    name = (bet.get("name") or "").lower()
                    if "over/under" in name:
                        for val in bet.get("values", []):
                            val_name = (val.get("value") or "").lower()
                            if val_name in ("over 2.5", "o 2.5", "over2.5"):
                                odd = val.get("odd")
                                if odd:
                                    return odd
        return "n/a"
    except Exception as e:
        log.error(f"odds error for fixture {fixture_id}: {e}")
        return "n/a"

# ===================== Scanning (08:00) =====================

def scan_today():
    tz = pytz.timezone(TIMEZONE)
    today_str = now_local().strftime("%Y-%m-%d")
    send(f"🛰️ Старт дневного прогона ({today_str}).")
    log.info("Daily scan started.")

    # 1) get all fixtures of today
    url = "https://v3.football.api-sports.io/fixtures"
    fixtures_data = api_get(url, {"date": today_str})
    fixtures = fixtures_data.get("response") or []

    if LEAGUE_SET:
        fixtures = [m for m in fixtures if str(m["league"]["id"]) in LEAGUE_SET]

    store = load_store()
    day_list = store["days"].setdefault(today_str, [])

    signals_sent = 0

    for m in fixtures:
        try:
            f      = m["fixture"]
            league = m["league"]
            teams  = m["teams"]
            fid    = f["id"]
            home_id = teams["home"]["id"]
            away_id = teams["away"]["id"]
            home_name = teams["home"]["name"]
            away_name = teams["away"]["name"]
            league_name = league["country"] + " — " + league["name"]

            # already signaled for this match?
            if any(x.get("fixture_id")==fid for x in day_list):
                continue

            # Strategy conditions
            if not h2h_three_all_over3(home_id, away_id):
                continue
            if not team_last3_at_least2_over3(home_id):
                continue
            if not team_last3_at_least2_over3(away_id):
                continue

            odds = try_get_odds_over25(fid)

            msg = (
                "⚽ <b>Сигнал (прематч)</b>\n"
                f"🏆 {league_name}\n"
                f"{home_name} — {away_name}\n"
                f"🎯 Рекомендуем: <b>ТБ 2.5</b>\n"
                f"💹 Коэф: <b>{odds}</b>\n"
            )
            send(msg)

            # save
            day_list.append({
                "fixture_id": fid,
                "home": home_name,
                "away": away_name,
                "league": league_name,
                "odds": odds,
                "time": f["date"],      # ISO
                "result_checked": False,
            })
            signals_sent += 1
            save_store(store)

            # tiny delay to be gentle
            time.sleep(0.4)

        except Exception as e:
            log.error(f"scan item error: {e}")

    send(f"✅ Прогон завершён. Найдено матчей: {signals_sent}.")
    log.info("Daily scan finished: %s", signals_sent)

# ===================== Reports =====================

def resolve_fixture_result(fid):
    url = "https://v3.football.api-sports.io/fixtures"
    data = api_get(url, {"id": fid})
    resp = data.get("response") or []
    if not resp:
        return None
    m = resp[0]
    st = m["fixture"]["status"]["short"]
    gh, ga, tot = total_goals_of_fixture(m)
    return {"status": st, "home_goals": gh, "away_goals": ga, "total": tot}

def daily_report():
    d_str = now_local().strftime("%Y-%m-%d")
    store = load_store()
    day_list = store["days"].get(d_str, [])

    if not day_list:
        send("📊 Отчёт за день\nСегодня сигналов не было.")
        return

    wins = losses = pend = 0
    lines = ["📊 <b>Отчёт за день</b>"]
    pnl = 0.0  # считаем ставку 1 ед.

    for rec in day_list:
        fid = rec["fixture_id"]
        res = resolve_fixture_result(fid)
        if not res:
            pend += 1
            lines.append(f"{rec['home']} — {rec['away']} | результат: n/a")
            continue

        st = res["status"]
        tot = res["total"]
        if is_finished_status(st):
            if tot >= 3:
                wins += 1
                pnl += 1.0
                mark = "✅"
            else:
                losses += 1
                pnl -= 1.0
                mark = "❌"
            lines.append(f"{rec['home']} {res['home_goals']}-{res['away_goals']} {rec['away']} | {mark}")
            rec["result_checked"] = True
            rec["final_total"] = tot
        else:
            pend += 1
            lines.append(f"{rec['home']} — {rec['away']} | статус: {st}")

    lines.append("─────────────")
    lines.append(f"Сигналов: {len(day_list)} | ✅ {wins}  ❌ {losses}  ⏳ {pend}")
    lines.append(f"Итог PnL: {pnl:+.2f} (ставка=1)")

    save_store(store)
    send("\n".join(lines))

def weekly_report():
    tz = pytz.timezone(TIMEZONE)
    today = now_local().date()
    week_ago = today - timedelta(days=7)

    store = load_store()
    wins = losses = total = 0
    pnl = 0.0

    for d_str, recs in store["days"].items():
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if not (week_ago <= d <= today):
            continue
        for r in recs:
            if r.get("final_total") is None:
                continue
            total += 1
            if r["final_total"] >= 3:
                wins += 1
                pnl += 1.0
            else:
                losses += 1
                pnl -= 1.0

    lines = [
        "🗓️ <b>Недельная сводка</b>",
        f"Период: {week_ago} — {today}",
        f"Ставок: {total}, Вин: {wins}, Луз: {losses}",
        f"PnL: {pnl:+.2f} (ставка=1)"
    ]
    send("\n".join(lines))

def monthly_report():
    today = now_local().date()
    first_day = today.replace(day=1)
    # last day of month:
    next_month = (first_day + timedelta(days=32)).replace(day=1)
    last_day = next_month - timedelta(days=1)

    store = load_store()
    wins = losses = total = 0
    pnl = 0.0

    for d_str, recs in store["days"].items():
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if not (first_day <= d <= last_day):
            continue
        for r in recs:
            if r.get("final_total") is None:
                continue
            total += 1
            if r["final_total"] >= 3:
                wins += 1
                pnl += 1.0
            else:
                losses += 1
                pnl -= 1.0

    lines = [
        "📅 <b>Месячная сводка</b>",
        f"Период: {first_day} — {last_day}",
        f"Ставок: {total}, Вин: {wins}, Луз: {losses}",
        f"PnL: {pnl:+.2f} (ставка=1)"
    ]
    send("\n".join(lines))

# ===================== Scheduler loop =====================

def should_run(now_dt, hh, mm):
    return now_dt.hour == hh and now_dt.minute == mm

def main_loop():
    send("🚀 Бот запущен (предматч, Render-ready).")
    send("ℹ️ График: скан в 08:00; отчёт 23:30; неделя — вс 23:50; месяц — в последний день 23:50.")

    # simple minute-tick loop
    last_minute = None
    while True:
        try:
            now_dt = now_local()
            this_minute = now_dt.strftime("%Y-%m-%d %H:%M")
            if this_minute != last_minute:
                last_minute = this_minute

                # Daily scan at 08:00
                if should_run(now_dt, 8, 0):
                    scan_today()

                # Daily report 23:30
                if should_run(now_dt, 23, 30):
                    daily_report()

                # Weekly (Sunday) 23:50
                if now_dt.weekday() == 6 and should_run(now_dt, 23, 50):
                    weekly_report()

                # Monthly (last day) 23:50
                tomorrow = now_dt.date() + timedelta(days=1)
                if tomorrow.day == 1 and should_run(now_dt, 23, 50):
                    monthly_report()

            time.sleep(1)
        except Exception as e:
            log.error(f"main loop error: {e}")
            time.sleep(3)

# ===================== RUN =====================

if __name__ == "__main__":
    # Start tiny HTTP server for Render
    Thread(target=run_http, daemon=True).start()
    # Main loop
    main_loop()
