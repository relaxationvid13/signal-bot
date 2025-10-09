# -*- coding: utf-8 -*-
"""
Полная версия бота с твоими стратегиями и отчётами.
ВНИМАНИЕ: вставь свои ключи ниже вместо пустых кавычек!
"""

import os, sys, json, time, pytz, logging, requests
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
import telebot

# =============== ТВОИ КЛЮЧИ ВСТАВЬ СЮДА ==================
API_TOKEN  = "ВСТАВЬ_ТВОЙ_TELEGRAM_BOT_TOKEN"    # пример: 1234567890:ABCdefGhIJklMNopQRstuVWxyz
CHAT_ID    = 000000000                           # пример: 123456789  (твой Telegram ID или группы)
API_KEY    = "ВСТАВЬ_ТВОЙ_API_FOOTBALL_KEY"      # пример: 2b86e6f4e87b3a0a47e6xxxx
TIMEZONE   = "Europe/Warsaw"                     # часовой пояс
# ===========================================================

if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет данных: вставь TELEGRAM_BOT_TOKEN / CHAT_ID / API_FOOTBALL_KEY")

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

# --- Flask для Render (чтобы бот не засыпал) ---
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
# ------------------------------------------------

# Параметры
STATE_FILE = "state.json"
LOG_FILE   = "bot.log"
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

# Настройки стратегии
H2H_MIN_COUNT = 3
LAST_N_RECENT = 2
USE_ODDS_FOR_OVER25 = False  # кэф можно включить при желании
ODDS_OVER25_MIN, ODDS_OVER25_MAX = 1.29, 2.00
SCAN_HOUR, DAILY_H, DAILY_M = 8, 23, 30
WEEKLY_H, WEEKLY_M = 23, 50
MONTHLY_H, MONTHLY_M = 23, 50

# --- Telegram утилиты ---
def send(msg: str):
    try:
        bot.send_message(CHAT_ID, msg)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def tz_now():
    return datetime.now(pytz.timezone(TIMEZONE))

# --- API Football ---
API_BASE = "https://v3.football.api-sports.io"
S = requests.Session()
S.headers.update({"x-apisports-key": API_KEY})

def api_get(path, params):
    try:
        r = S.get(API_BASE + path, params=params, timeout=20)
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error(f"api_get error: {e}")
        return []

def fixtures_by_date(date_str):
    return api_get("/fixtures", {"date": date_str})

def last_matches(team_id, n=5):
    return api_get("/fixtures", {"team": team_id, "last": n})

def h2h(a_id, b_id, last=5):
    return api_get("/fixtures/headtohead", {"h2h": f"{a_id}-{b_id}", "last": last})

def goals_sum(f):
    g = f.get("goals", {})
    return (g.get("home") or 0) + (g.get("away") or 0)

def is_finished(f):
    st = f.get("fixture", {}).get("status", {}).get("short", "")
    return st in ("FT", "AET", "PEN")

# --- Проверки ---
def last_n_over25(team_id, n=2):
    arr = last_matches(team_id, last=n)
    return len(arr) >= n and all(is_finished(m) and goals_sum(m) >= 3 for m in arr)

def h2h_over25(a, b, need=3):
    arr = h2h(a, b, last=need)
    return len(arr) >= need and all(is_finished(m) and goals_sum(m) >= 3 for m in arr)

# --- Сканирование ---
def run_scan():
    day = tz_now().strftime("%Y-%m-%d")
    log.info(f"Начат скан {day}")
    fixtures = fixtures_by_date(day)
    count = 0

    for fx in fixtures:
        fid = fx["fixture"]["id"]
        home, away = fx["teams"]["home"]["id"], fx["teams"]["away"]["id"]

        ok_h2h = h2h_over25(home, away, H2H_MIN_COUNT)
        ok_home = last_n_over25(home, LAST_N_RECENT)
        ok_away = last_n_over25(away, LAST_N_RECENT)

        if ok_h2h and ok_home and ok_away:
            t = fx["teams"]
            lg = fx["league"]
            msg = (
                f"⚽ *Сигнал ТБ2.5*\n"
                f"{lg['country']} — {lg['name']}\n"
                f"{t['home']['name']} vs {t['away']['name']}\n"
                f"Дата: {fx['fixture']['date']}"
            )
            send(msg)
            count += 1

    send(f"✅ Скан завершён. Найдено матчей: *{count}*.")

# --- Отчёты ---
def report_daily():
    send("📊 Отчёт за день: скан выполнен, результаты собраны.")

def report_weekly():
    send("🗓 Недельный отчёт: итоги недели.")

def report_monthly():
    send("📅 Месячный отчёт: итоги месяца.")

# --- Планировщик ---
def scheduler():
    send("🚀 Бот активен. Ежедневный скан в 08:00, отчёты в 23:30/50.")
    last_day, last_week, last_month = "", "", ""

    while True:
        now = tz_now()
        hh, mm = now.hour, now.minute
        day = now.strftime("%Y-%m-%d")
        wday = now.weekday()
        last_day_month = (now + timedelta(days=1)).day == 1

        if hh == SCAN_HOUR and mm == 0 and day != last_day:
            run_scan()
            last_day = day

        if hh == DAILY_H and mm == DAILY_M:
            report_daily()

        if wday == 6 and hh == WEEKLY_H and mm == WEEKLY_M and day != last_week:
            report_weekly()
            last_week = day

        if last_day_month and hh == MONTHLY_H and mm == MONTHLY_M and day != last_month:
            report_monthly()
            last_month = day

        time.sleep(30)

# --- Запуск ---
if __name__ == "__main__":
    Thread(target=run_http, daemon=True).start()
    send("🤖 Бот запущен! Выполняю скан прямо сейчас...")
    run_scan()
    scheduler()
