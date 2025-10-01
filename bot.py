# -*- coding: utf-8 -*-
"""
Pre-match signals bot for Render (sleep-resilient) + Telegram commands:
- Автозадачи (как раньше): скан ≥ 08:00; отчёт ≥ 23:30; неделя — вс ≥ 23:50; месяц — в посл. день ≥ 23:50.
- Команды в чате:
  /scan     — принудительный дневной прогон
  /report   — дневной отчёт
  /weekly   — недельный отчёт
  /monthly  — месячный отчёт
  /status   — показать последние отметки
  /help     — список команд
"""

import os
import json
import time
import pytz
import logging
import threading
from datetime import datetime, timedelta, date
from threading import Thread, Lock

import requests
import telebot
from telebot import types
from flask import Flask

# ===================== Settings & Secrets =====================

API_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID")
API_KEY     = os.getenv("API_FOOTBALL_KEY")
TIMEZONE    = os.getenv("TZ", "Europe/Warsaw")

LEAGUE_FILTER = os.getenv("LEAGUE_IDS", "").strip()
LEAGUE_SET = set(s.strip() for s in LEAGUE_FILTER.split(",") if s.strip())

REQUEST_TIMEOUT = 15
STORAGE_FILE    = "signals.json"
LOG_FILE        = "bot.log"

if not API_TOKEN or not CHAT_ID_RAW or not API_KEY:
    raise SystemExit("❌ Need TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

try:
    CHAT_ID = int(CHAT_ID_RAW)
except Exception:
    CHAT_ID = CHAT_ID_RAW

# ===================== Logging =====================

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("prematch-bot")

# ===================== Telegram & HTTP session =====================

bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})

# ===================== Flask (Render Web Service) =====================

app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ===================== Helpers =====================

def tz():
    return pytz.timezone(TIMEZONE)

def now_local():
    return datetime.now(tz())

def send(msg: str):
    try:
        bot.send_message(CHAT_ID, msg)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def default_store():
    return {
        "meta": {
            "last_scan_date": None,            # "YYYY-MM-DD"
            "last_daily_report_date": None,    # "YYYY-MM-DD"
            "last_weekly_yrwk": None,          # "YYYY-WW"
            "last_monthly_yrmo": None          # "YYYY-MM"
        },
        "days": {}  # "YYYY-MM-DD": [ {fixture,...} ]
    }

def load_store():
    if not os.path.exists(STORAGE_FILE):
        return default_store()
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "meta" not in data:
            data["meta"] = default_store()["meta"]
        if "days" not in data:
            data["days"] = {}
        return data
    except Exception as e:
        log.error(f"load_store error: {e}")
        return default_store()

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
    try:
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return gh, ga, (gh + ga)
    except Exception:
        return 0, 0, 0

def is_finished_status(short):
    return short in ("FT", "AET", "PEN")

# ===================== Strategy checks =====================

def h2h_three_all_over3(home_id, away_id):
    url = "https://v3.football.api-sports.io/fixtures/headtohead"
    data = api_get(url, {"h2h": f"{home_id}-{away_id}", "last": 3})
    resp = data.get("response") or []
    if len(resp) < 3:
        return False
    for m in resp:
        _, _, tot = total_goals_of_fixture(m)
        if tot < 3:
            return False
    return True

def team_last3_at_least2_over3(team_id):
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
    try:
        url = "https://v3.football.api-sports.io/odds"
        data = api_get(url, {"fixture": fixture_id})
        resp = data.get("response") or []
        for item in resp:
            for book in item.get("bookmakers", []):
                for bet in book.get("bets", []):
                    name = (bet.get("name") or "").lower()
                    if "over/under" in name:
                        for val in bet.get("values", []):
                            v = (val.get("value") or "").lower()
                            if v in ("over 2.5", "o 2.5", "over2.5"):
                                odd = val.get("odd")
                                if odd:
                                    return odd
        return "n/a"
    except Exception as e:
        log.error(f"odds error for fixture {fixture_id}: {e}")
        return "n/a"

# ===================== Daily scan =====================

def scan_today():
    today_str = now_local().strftime("%Y-%m-%d")
    send(f"🛰️ Старт дневного прогона ({today_str}).")
    log.info("Daily scan started.")

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

            if any(x.get("fixture_id")==fid for x in day_list):
                continue

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

            day_list.append({
                "fixture_id": fid,
                "home": home_name,
                "away": away_name,
                "league": league_name,
                "odds": odds,
                "time": f["date"],
                "result_checked": False,
            })
            signals_sent += 1
            save_store(store)
            time.sleep(0.3)

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
    pnl = 0.0

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

# ===================== Sleep-resilient scheduler =====================

def scan_due(nowdt, meta):
    target_hour = 8
    today = nowdt.strftime("%Y-%m-%d")
    if meta.get("last_scan_date") == today:
        return False
    if nowdt.hour >= target_hour:
        return True
    return False

def daily_report_due(nowdt, meta):
    target = (23, 30)
    today = nowdt.strftime("%Y-%m-%d")
    if meta.get("last_daily_report_date") == today:
        return False
    if (nowdt.hour, nowdt.minute) >= target:
        return True
    return False

def weekly_report_due(nowdt, meta):
    target = (23, 50)
    yrwk = f"{nowdt.isocalendar().year}-{nowdt.isocalendar().week:02d}"
    if meta.get("last_weekly_yrwk") == yrwk:
        return False
    if nowdt.weekday() == 6 and (nowdt.hour, nowdt.minute) >= target:
        return True
    return False

def monthly_report_due(nowdt, meta):
    target = (23, 50)
    yrmo = nowdt.strftime("%Y-%m")
    tomorrow = nowdt.date() + timedelta(days=1)
    is_last_day = (tomorrow.day == 1)
    if meta.get("last_monthly_yrmo") == yrmo:
        return False
    if is_last_day and (nowdt.hour, nowdt.minute) >= target:
        return True
    return False

# ====== синхронизационный замок, чтобы задачи не накладывались ======
TASK_LOCK = Lock()

def main_loop():
    send("🚀 Бот запущен (предматч, Render-ready, устойчив к сну).")
    send("ℹ️ График: скан ≥08:00 1р/день; отчёт ≥23:30; неделя — вс ≥23:50; месяц — в посл. день ≥23:50.")
    last_log_min = None

    while True:
        try:
            nowdt = now_local()
            key = nowdt.strftime("%Y-%m-%d %H:%M")
            if key != last_log_min:
                last_log_min = key
                log.info("tick %s", key)

            with TASK_LOCK:
                store = load_store()
                meta = store.get("meta", {})
                changed = False

                if scan_due(nowdt, meta):
                    scan_today()
                    meta["last_scan_date"] = nowdt.strftime("%Y-%m-%d")
                    changed = True

                if daily_report_due(nowdt, meta):
                    daily_report()
                    meta["last_daily_report_date"] = nowdt.strftime("%Y-%m-%d")
                    changed = True

                if weekly_report_due(nowdt, meta):
                    weekly_report()
                    yrwk = f"{nowdt.isocalendar().year}-{nowdt.isocalendar().week:02d}"
                    meta["last_weekly_yrwk"] = yrwk
                    changed = True

                if monthly_report_due(nowdt, meta):
                    monthly_report()
                    meta["last_monthly_yrmo"] = nowdt.strftime("%Y-%m")
                    changed = True

                if changed:
                    store["meta"] = meta
                    save_store(store)

            time.sleep(5)

        except Exception as e:
            log.error(f"main loop error: {e}")
            time.sleep(5)

# ===================== Telegram commands =====================

def owner_only(message):
    return str(message.chat.id) == str(CHAT_ID)

@bot.message_handler(commands=['help', 'start'])
def cmd_help(message):
    if not owner_only(message): return
    bot.reply_to(message,
        "Команды:\n"
        "/scan — дневной прогон сейчас\n"
        "/report — дневной отчёт сейчас\n"
        "/weekly — недельная сводка\n"
        "/monthly — месячная сводка\n"
        "/status — последние отметки (когда выполнялось)\n"
    )

@bot.message_handler(commands=['status'])
def cmd_status(message):
    if not owner_only(message): return
    st = load_store().get("meta", {})
    bot.reply_to(message,
        "📌 Статус:\n"
        f"last_scan_date: {st.get('last_scan_date')}\n"
        f"last_daily_report_date: {st.get('last_daily_report_date')}\n"
        f"last_weekly_yrwk: {st.get('last_weekly_yrwk')}\n"
        f"last_monthly_yrmo: {st.get('last_monthly_yrmo')}\n"
    )

@bot.message_handler(commands=['scan'])
def cmd_scan(message):
    if not owner_only(message): return
    with TASK_LOCK:
        scan_today()
        store = load_store()
        store["meta"]["last_scan_date"] = now_local().strftime("%Y-%m-%d")
        save_store(store)
    bot.reply_to(message, "👌 Скан завершён.")

@bot.message_handler(commands=['report'])
def cmd_report(message):
    if not owner_only(message): return
    with TASK_LOCK:
        daily_report()
        store = load_store()
        store["meta"]["last_daily_report_date"] = now_local().strftime("%Y-%m-%d")
        save_store(store)
    bot.reply_to(message, "👌 Дневной отчёт отправлен.")

@bot.message_handler(commands=['weekly'])
def cmd_weekly(message):
    if not owner_only(message): return
    with TASK_LOCK:
        weekly_report()
        store = load_store()
        yrwk = f"{now_local().isocalendar().year}-{now_local().isocalendar().week:02d}"
        store["meta"]["last_weekly_yrwk"] = yrwk
        save_store(store)
    bot.reply_to(message, "👌 Недельная сводка отправлена.")

@bot.message_handler(commands=['monthly'])
def cmd_monthly(message):
    if not owner_only(message): return
    with TASK_LOCK:
        monthly_report()
        store = load_store()
        store["meta"]["last_monthly_yrmo"] = now_local().strftime("%Y-%m")
        save_store(store)
    bot.reply_to(message, "👌 Месячная сводка отправлена.")

def tg_polling():
    # устойчиво к сетевым сбоям
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            log.error(f"polling error: {e}")
            time.sleep(3)

# ===================== RUN =====================

if __name__ == "__main__":
    Thread(target=run_http, daemon=True).start()   # для Render Web Service
    Thread(target=tg_polling, daemon=True).start() # приём команд
    main_loop()
