# -*- coding: utf-8 -*-
"""
Предматч-бот (Render-ready, Web Service Free).
Стратегия:
  ✅ Последний очный матч — ТБ 2.5
  ✅ Два последних матча КАЖДОЙ команды — ТБ 2.5
  ✅ (Опционально) фильтр по кэфу на ТБ2.5 в диапазоне [ODDS_MIN, ODDS_MAX]

Расписание (Europe/Warsaw):
  - 08:00 — скан карточки матчей на сегодня и отправка сигналов
  - 23:30 — дневной отчёт
  - Вс 23:50 — недельный отчёт
  - Последний день месяца 23:50 — месячный отчёт
"""

import os, sys, time, json, logging, math
from datetime import datetime, timedelta, date
from threading import Thread

import pytz
import requests
import telebot
from flask import Flask

# ----------------- Mini web (Render port binding) -----------------
app = Flask(__name__)
@app.get("/")
def health():
    return "ok"  # Render needs an open port to keep process alive
def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
# ------------------------------------------------------------------

# ================= Конфигурация =================
API_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")
API_KEY     = os.getenv("API_FOOTBALL_KEY")
TIMEZONE    = os.getenv("TZ", "Europe/Warsaw")

if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ env vars required: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

CHAT_ID = int(CHAT_ID)

# Порог кэфов (опционально). Если оставить None — фильтр по кэфу отключён.
def _get_float_or_none(name, default=None):
    v = os.getenv(name, "")
    try:
        return float(v) if v else default
    except Exception:
        return default

ODDS_MIN = _get_float_or_none("ODDS_MIN", None)  # напр. 1.29
ODDS_MAX = _get_float_or_none("ODDS_MAX", None)  # напр. 2.00

# Время цикла бота: держим невысоким, он сам проверяет «часы»
LOOP_SECONDS = 60

# Файлы состояния/логов
LOG_FILE   = "bot.log"
STATE_FILE = "signals.json"

# ================= Логи =================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("prematch-bot")

# ================= Telegram =================
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

def now_local():
    return datetime.now(pytz.timezone(TIMEZONE))

def send(msg: str):
    try:
        bot.send_message(CHAT_ID, msg)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ================= API-Football =================
API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 15

def api_get(url, params=None):
    try:
        r = API.get(url, params=params or {}, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("HTTP %s %s", r.status_code, r.text[:160])
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"api_get error: {e}")
        return None

def goals_total_from_fixture(m):
    """ Возвращает (home_goals, away_goals, total) из объекта матча API-Football. """
    try:
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return gh, ga, gh + ga
    except Exception:
        return 0, 0, None

def is_tb25_total(total):
    return (total is not None) and (total >= 3)

def odds_over25_for_fixture(fixture_id):
    """
    Возвращает кэф на ТБ2.5 (float) или None.
    Ищем market "Over/Under", line "2.5" / outcome "Over".
    """
    url = "https://v3.football.api-sports.io/odds"
    data = api_get(url, {"fixture": fixture_id})
    if not data: 
        return None
    resp = data.get("response", []) or []
    for book in resp:
        for market in book.get("bookmakers", []):
            for mark in market.get("bets", []):
                if (mark.get("name", "").lower() in ("over/under", "ou", "over under")):
                    for v in mark.get("values", []):
                        ln = v.get("value", "").replace(" ", "")
                        if ln in ("Over2.5", "2.5", "Over 2.5"):
                            try:
                                return float(v.get("odd"))
                            except Exception:
                                continue
    return None

def last_h2h_is_tb25(home_id, away_id):
    """
    Проверяем ПОСЛЕДНИЙ очный матч => ТБ2.5?
    """
    url = "https://v3.football.api-sports.io/fixtures/headtohead"
    data = api_get(url, {"h2h": f"{home_id}-{away_id}", "last": 1})
    if not data: 
        return False
    resp = data.get("response", []) or []
    if not resp:
        return False
    gh, ga, tot = goals_total_from_fixture(resp[0])
    return is_tb25_total(tot)

def last_k_is_tb25_for_team(team_id, k=2):
    """
    Проверяем: у команды последние k матчей — ТБ2.5?
    """
    url = "https://v3.football.api-sports.io/fixtures"
    data = api_get(url, {"team": team_id, "last": k})
    if not data:
        return False
    resp = data.get("response", []) or []
    if len(resp) < k:
        return False
    for m in resp:
        gh, ga, tot = goals_total_from_fixture(m)
        if not is_tb25_total(tot):
            return False
    return True

# ================= Состояние (сигналы/ставки) =================
STATE = {
    "signals": [],        # список сигналов за сегодня
    "history": []         # архив (для недели/месяца)
}

def load_state():
    global STATE
    if os.path.exists(STATE_FILE):
        try:
            STATE = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        except Exception as e:
            log.error(f"load_state: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
    except Exception as e:
        log.error(f"save_state: {e}")

def reset_today():
    """Начать новый день: текущие сигналы -> историю, очистить список."""
    today_str = now_local().strftime("%Y-%m-%d")
    if STATE.get("signals"):
        STATE["history"].extend(STATE["signals"])
    STATE["signals"] = []
    save_state()

# ================= Формирование отчётов =================
def summarize(items):
    # Простейшая "прибыль": считаем +1 если FT total >= 3, иначе -1 (модель для фиксации результата)
    win = lose = draw = 0
    for it in items:
        res = it.get("result")
        if res == "WIN":
            win += 1
        elif res == "LOSE":
            lose += 1
        else:
            draw += 1
    played = win + lose + draw
    streak = f"Ставок: {played}, Вин: {win}, Луз: {lose}, Н/Д: {draw}\n"
    return streak

def finalize_results_for_day(day_items):
    """Обновляем результаты сигналов (WIN/LOSE) на основании финального счёта."""
    url = "https://v3.football.api-sports.io/fixtures"
    changed = 0
    for it in day_items:
        if it.get("result") in ("WIN", "LOSE"):
            continue
        fid = it.get("fixture_id")
        data = api_get(url, {"id": fid}) or {}
        resp = (data.get("response") or [])
        if not resp:
            continue
        st = resp[0]["fixture"]["status"]["short"]
        gh, ga, tot = goals_total_from_fixture(resp[0])
        if st in ("FT", "AET", "PEN"):
            it["final_total"] = tot
            it["result"] = "WIN" if is_tb25_total(tot) else "LOSE"
            changed += 1
    if changed:
        save_state()

def day_report():
    # перед отчётом — обновляем результаты сегодняшних сигналов
    finalize_results_for_day(STATE.get("signals", []))
    txt = ["📊 *Отчёт за день*"]
    today_str = now_local().strftime("%Y-%m-%d")
    today_signals = [s for s in STATE.get("signals", []) if s.get("date") == today_str]
    if not today_signals:
        txt.append("За сегодня сигналов не было.")
    else:
        txt.append(summarize(today_signals))
        for i, s in enumerate(today_signals, 1):
            line = (
                f"{i}. {s['home']}–{s['away']} | "
                f"условие: H2H ТБ2.5 + 2/2 ТБ2.5 у обеих | "
                f"кэф ТБ2.5: {s.get('odds_over25','–')}"
            )
            res = s.get("result")
            if res:
                line += f" | итог: {s.get('final_total','?')} ({'✅' if res=='WIN' else '❌'})"
            txt.append(line)
    send("\n".join(txt))

def weekly_report():
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    start = (now - timedelta(days=6)).date()
    items = [x for x in STATE.get("history", []) if date.fromisoformat(x["date"]) >= start]
    txt = ["📅 *Недельная сводка* (последние 7 дней)"]
    if not items:
        txt.append("Данных пока нет.")
    else:
        txt.append(summarize(items))
    send("\n".join(txt))

def monthly_report():
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    first_day = now.replace(day=1).date()
    items = [x for x in STATE.get("history", []) if date.fromisoformat(x["date"]) >= first_day]
    txt = ["🗓 *Месячная сводка* (текущий месяц)"]
    if not items:
        txt.append("Данных пока нет.")
    else:
        txt.append(summarize(items))
    send("\n".join(txt))

# ================= Скан по стратегии =================
def scan_and_signal_today():
    """
    1) Получаем список матчей на сегодня (status NS/Not Started)
    2) Фильтруем по стратегии:
       - последний очный — ТБ2.5
       - у каждой из команд 2 последних матча — ТБ2.5
       - (опц.) кэф на ТБ2.5 в диапазоне
    3) Шлём сигналы и сохраняем в STATE
    """
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).strftime("%Y-%m-%d")

    # сбросить "сегодня" если дата поменялась
    last_date_in_state = STATE.get("signals", [{}])[-1].get("date") if STATE.get("signals") else None
    if last_date_in_state and last_date_in_state != today:
        reset_today()

    url = "https://v3.football.api-sports.io/fixtures"
    data = api_get(url, {"date": today})
    if not data:
        return
    fixtures = data.get("response", []) or []

    matches = []
    for m in fixtures:
        try:
            if m["fixture"]["status"]["short"] != "NS":
                continue
            home_id = m["teams"]["home"]["id"]
            away_id = m["teams"]["away"]["id"]
            home   = m["teams"]["home"]["name"]
            away   = m["teams"]["away"]["name"]
            fid    = m["fixture"]["id"]

            # Проверки стратегии
            if not last_h2h_is_tb25(home_id, away_id):
                continue
            if not last_k_is_tb25_for_team(home_id, k=2):
                continue
            if not last_k_is_tb25_for_team(away_id, k=2):
                continue

            # (Опционально) фильтр по кэфу ТБ2.5
            odds = odds_over25_for_fixture(fid)
            if (ODDS_MIN is not None and (odds is None or odds < ODDS_MIN)):
                continue
            if (ODDS_MAX is not None and (odds is None or odds > ODDS_MAX)):
                continue

            matches.append((m, odds))
        except Exception as e:
            log.error(f"scan item err: {e}")

    if not matches:
        send("ℹ️ Скан 08:00: подходящих матчей по стратегии сегодня не найдено.")
        return

    # Сигналы
    lines = ["🔥 *Сигналы на сегодня (предматч)*", "_Стратегия: H2H ТБ2.5 + 2/2 ТБ2.5 у обеих_"]
    for m, odds in matches:
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        fid  = m["fixture"]["id"]
        league = f"{m['league']['country']} — {m['league']['name']}"
        time_ = m["fixture"]["date"]  # ISO
        o_str = f"{odds:.2f}" if isinstance(odds, float) else "нет данных"

        # сохраняем сигнал (для отчёта и фиксации результата вечером)
        STATE["signals"].append({
            "date": today,
            "fixture_id": fid,
            "home": home,
            "away": away,
            "league": league,
            "odds_over25": odds,
            "result": None,
        })

        lines.append(
            f"• {league}\n  {home} — {away}\n  Кэф ТБ2.5: *{o_str}*"
        )

    save_state()
    send("\n".join(lines))

# ================= Главный цикл =================
def main_loop():
    load_state()
    send("🚀 Бот запущен (предматч, Render-ready).")
    send("ℹ️ График: скан в 08:00; отчёт 23:30; неделя — вс 23:50; месяц — в последний день 23:50.")

    last_scan_date = None
    tz = pytz.timezone(TIMEZONE)

    while True:
        try:
            now = datetime.now(tz)

            # Скан в 08:00 (раз в день)
            if now.hour == 8 and now.minute == 0:
                if last_scan_date != now.date():
                    scan_and_signal_today()
                    last_scan_date = now.date()
                    time.sleep(60)  # чтобы не дергало несколько раз

            # Дневной отчёт 23:30
            if now.hour == 23 and now.minute == 30:
                day_report()
                time.sleep(60)

            # Недельный отчёт по воскресеньям 23:50
            if now.hour == 23 and now.minute == 50 and now.weekday() == 6:
                weekly_report()
                time.sleep(60)

            # Месячный отчёт в последний день месяца 23:50
            tomorrow = now + timedelta(days=1)
            if now.hour == 23 and now.minute == 50 and tomorrow.month != now.month:
                monthly_report()
                time.sleep(60)

            time.sleep(LOOP_SECONDS)
        except Exception as e:
            log.error(f"main loop error: {e}")
            time.sleep(LOOP_SECONDS)

# ================= RUN =================
if __name__ == "__main__":
    # 1) HTTP для Render
    Thread(target=run_http, daemon=True).start()
    # 2) Логика
    main_loop()
