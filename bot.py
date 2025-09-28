# -*- coding: utf-8 -*-
"""
Бот: опрос лайвов по окну времени + отчёты.
Исправлено: отчёты не дублируются (анти-спам по дням/неделям/месяцам).
"""

import os, sys, time, json, logging
from datetime import datetime, timedelta, date
import pytz, requests, telebot

# ---------- для Render (healthcheck) ----------
from threading import Thread
from flask import Flask
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
# ---------------------------------------------

# ======= Секреты/окружение =======
API_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
API_KEY    = os.getenv("API_FOOTBALL_KEY")
TIMEZONE   = os.getenv("TZ_NAME", "Europe/Warsaw")  # отчёты в этом часовом поясе
DISABLE_REPORTS = os.getenv("DISABLE_REPORTS", "0") == "1"

if not API_TOKEN or not CHAT_ID:
    sys.exit("❌ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы")
CHAT_ID = int(CHAT_ID)

# ======= Параметры =======
POLL_SECONDS = 300           # 5 минут
ACTIVE_FROM  = (16, 0)       # 16:00 лок.
ACTIVE_TILL  = (23, 29)      # 23:29 лок.
STAKE = 1                    # условная ставка для отчётов

LOG_FILE   = "bot.log"
STATE_FILE = "signals.json"  # тут храним сигналы и отметки о последних отчётах

# ======= Логи =======
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bot")

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

API = requests.Session()
if API_KEY:
    API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 12

# ======= Состояние (persist) =======
state = {
    # сигналы за сегодня
    "signals": [],  # каждый: {"ts":"YYYY-MM-DDTHH:MM:SS", "type":"OVER20/UNDER20", "desc":"..."}
    # антиспам-метки
    "last_daily": "",     # "YYYY-MM-DD"
    "last_weekly": "",    # "YYYY-Www" (ISO week)
    "last_monthly": ""    # "YYYY-MM"
}

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # аккуратно обновим ключи (чтобы не потерять новые)
                for k in state.keys():
                    if k in data:
                        state[k] = data[k]
        except Exception as e:
            log.error(f"load_state error: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_state error: {e}")

def tz_now():
    return datetime.now(pytz.timezone(TIMEZONE))

def send(txt: str):
    try:
        bot.send_message(CHAT_ID, txt)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ======= Заглушки логики сигналов (здесь можешь добавить свою логику сканирования) =======
def in_active_window(now_local: datetime) -> bool:
    hm = (now_local.hour, now_local.minute)
    return (hm >= ACTIVE_FROM) and (hm <= ACTIVE_TILL)

def scan_and_collect_signals():
    """
    Заглушка: здесь могла бы быть твоя логика опроса API и выявления сигналов.
    Сейчас ничего не добавляем, чтобы сфокусироваться на отчётах.
    Пример, как добавить сигнал:
    state["signals"].append({
        "ts": tz_now().strftime("%Y-%m-%dT%H:%M:%S"),
        "type": "OVER20",   # или UNDER20
        "desc": "Turkey 2.Lig | 19' 2-0 | кэф на ТБ3: 1.60"
    })
    """
    pass

# ======= Формирование отчётов =======
def build_day_report(day: date) -> str:
    # фильтруем события за дату day
    day_str = day.strftime("%Y-%m-%d")
    today_signals = [s for s in state["signals"]
                     if s.get("ts","").startswith(day_str)]
    lines = ["🧾 *Отчёт за день*",
             f"Период: {day_str}",
             f"Ставок: {len(today_signals)}"]
    # при желании можно считать вин/луз, профит — сейчас нет исходов матчей
    if not today_signals:
        lines.append("За сегодня ставок не было.")
        return "\n".join(lines)

    # сводка по типам
    over = sum(1 for s in today_signals if s.get("type")=="OVER20")
    under= sum(1 for s in today_signals if s.get("type")=="UNDER20")

    lines.append(f"OVER20: {over} | UNDER20: {under}")
    lines.append("──────────")
    cut = today_signals[-12:]  # покажем последние 12 для краткости
    for i,s in enumerate(cut, 1):
        lines.append(f"{i:02d}. {s.get('desc','')}")
    return "\n".join(lines)

def build_week_report(week_date: date) -> str:
    # неделя ISO — возьмём период понедельник-воскресенье
    iso_year, iso_week, iso_weekday = week_date.isocalendar()
    lines = ["📅 *Недельная сводка*",
             f"ISO-week: {iso_year}-W{iso_week:02d}"]

    # набираем сигналы за всю неделю
    week_signals = []
    for s in state["signals"]:
        try:
            d = datetime.strptime(s["ts"][:10], "%Y-%m-%d").date()
            y, w, wd = d.isocalendar()
            if y==iso_year and w==iso_week:
                week_signals.append(s)
        except Exception:
            pass

    lines.append(f"Ставок: {len(week_signals)}")
    if not week_signals:
        lines.append("За эту неделю ставок не было.")
        return "\n".join(lines)

    over = sum(1 for s in week_signals if s.get("type")=="OVER20")
    under= sum(1 for s in week_signals if s.get("type")=="UNDER20")
    lines.append(f"OVER20: {over} | UNDER20: {under}")
    return "\n".join(lines)

def build_month_report(month_date: date) -> str:
    ym = month_date.strftime("%Y-%m")
    lines = ["🗓 *Месячная сводка*",
             f"Месяц: {ym}"]

    month_signals = [s for s in state["signals"]
                     if s.get("ts","").startswith(ym)]
    lines.append(f"Ставок: {len(month_signals)}")
    if not month_signals:
        lines.append("В этом месяце ставок не было.")
        return "\n".join(lines)

    over = sum(1 for s in month_signals if s.get("type")=="OVER20")
    under= sum(1 for s in month_signals if s.get("type")=="UNDER20")
    lines.append(f"OVER20: {over} | UNDER20: {under}")
    return "\n".join(lines)

# ======= Анти-спам отчётов =======
def maybe_send_reports():
    if DISABLE_REPORTS:
        return

    now = tz_now()
    # Время отчёта — ровно 23:30
    if not (now.hour == 23 and now.minute == 30):
        return

    # DAILY — один раз в день
    day_key = now.strftime("%Y-%m-%d")
    if state.get("last_daily") != day_key:
        send(build_day_report(now.date()))
        state["last_daily"] = day_key
        save_state()
        # 60 секунд «тишины», чтобы не продублировать в ту же минуту
        time.sleep(60)

    # WEEKLY — по воскресеньям (weekday() == 6)
    if now.weekday() == 6:
        iso_year, iso_week, _ = now.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        if state.get("last_weekly") != week_key:
            send(build_week_report(now.date()))
            state["last_weekly"] = week_key
            save_state()
            time.sleep(5)

    # MONTHLY — в последний день месяца
    next_day = now.date() + timedelta(days=1)
    if next_day.day == 1:  # значит сегодня последний день месяца
        month_key = now.strftime("%Y-%m")
        if state.get("last_monthly") != month_key:
            send(build_month_report(now.date()))
            state["last_monthly"] = month_key
            save_state()
            time.sleep(5)

# ======= RUN =======
if __name__ == "__main__":
    # HTTP для Render (Web Service/Free)
    Thread(target=run_http, daemon=True).start()

    load_state()

    send("🚀 Бот запущен — новая версия!")
    send("✅ Активное окно: 16:00—23:29 (PL), опрос каждые 5 минут.\nОтчёты — в 23:30.")

    while True:
        try:
            now = tz_now()

            # опрос матчей только в окне
            if in_active_window(now):
                scan_and_collect_signals()

            # отчёты с антиспамом
            maybe_send_reports()

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(POLL_SECONDS)
