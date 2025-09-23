# -*- coding: utf-8 -*-
"""
Эконом-бот: сигналы при 2/3 голах на ~20' + отчёты.
- Каждые 15 минут: 1 запрос /fixtures?live=all
- Сигнал: только на ~20' (19..22 мин), если ровно 2 или 3 гола
- Отчёт: ежедневно в 23:30 по Europe/Warsaw (окно 23:30..23:35),
         плюс недельная и месячная сводки.
- Ручные команды в Telegram: /status, /report, /test_signal
- Render-friendly: Flask healthcheck + infinity_polling в отдельном потоке
"""

import os, sys, time, json, logging
from datetime import datetime, timedelta
from threading import Thread

import pytz
import requests
import telebot
from flask import Flask

# ================== Конфиг / Параметры ==================

# Секреты из окружения
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")

if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

CHAT_ID   = int(CHAT_ID)

# Временная зона — Польша
TIMEZONE = "Europe/Warsaw"

# Эконом-режим опроса
POLL_SECONDS = 15 * 60            # 1 запрос раз в 15 минут ≈ 96/сутки
WINDOW_20    = range(19, 23)      # окно «~20'» (19..22, включительно)

# «Линия» по нашему правилу:
#   2 гола -> ставим ТБ3, выигрыш если итог >= 4
#   3 гола -> ставим ТБ4, выигрыш если итог >= 5
ODDS_MIN = 1.29                   # нижняя граница кэфа (если позже подключишь реальные)
ODDS_MAX = 2.00                   # верхняя граница

# Условная ставка для отчёта (в единицах, ты можешь поставить 1)
STAKE_UNITS = 1

# Файлы
LOG_FILE   = "bot.log"
STATE_FILE = "signals.json"

# ================== Логирование ==================

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("econ-bot")

# ================== Telegram / Flask ==================

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

def run_telebot():
    # отдельный поток для приёма команд
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)

# ================== API-Football ==================

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 15

def get_live():
    """1 запрос — все лайвы по футболу"""
    try:
        r = API.get("https://v3.football.api-sports.io/fixtures?live=all", timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("fixtures HTTP %s %s", r.status_code, r.text[:200])
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error(f"get_live error: {e}")
        return []

def get_fixture_result(fid: int):
    """1 запрос — итог конкретного матча."""
    try:
        r = API.get(f"https://v3.football.api-sports.io/fixtures?id={fid}", timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        resp = r.json().get("response", []) or []
        if not resp:
            return None
        m = resp[0]
        st = m["fixture"]["status"]["short"]
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return st, gh, ga
    except Exception as e:
        log.error(f"get_fixture_result({fid}) error: {e}")
        return None

def get_over_odds(fid: int, line: str):
    """
    Заглушка под будущую интеграцию источника кэфов.
    line: 'ТБ3' или 'ТБ4'
    Верни float или None, если не можешь получить.
    """
    return None

# ================== Память / Состояние ==================

signals = []          # [{...}, ...]
signaled_ids = set()  # чтобы не дублировать

def load_state():
    global signals, signaled_ids
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        signals = data.get("signals", [])
        signaled_ids = set(data.get("signaled_ids", []))
    except Exception as e:
        log.error(f"load_state error: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"signals": signals, "signaled_ids": list(signaled_ids)}, f, ensure_ascii=False)
    except Exception as e:
        log.error(f"save_state error: {e}")

# ================== Утилиты ==================

def now_local():
    return datetime.now(pytz.timezone(TIMEZONE))

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ================== Сканер лайвов ==================

def format_signal_msg(rec):
    gh, ga = rec["goals_home"], rec["goals_away"]
    total = gh + ga
    line  = rec["bet_line"]
    odds  = rec.get("odds")
    minute = rec["minute"]
    return (
        "⚪ *Ставка!*\n"
        f"🏆 {rec['country']} — {rec['league']}\n"
        f"{rec['home']} {gh} — {ga} {rec['away']}\n"
        f"⏱ ~{minute}'  (всего: {total}, линия: {line})\n"
        f"{'💬 Кф: ' + str(odds) if odds else ''}\n"
        "─────────────"
    )

def scan_and_signal():
    live = get_live()
    for m in live:
        try:
            f = m["fixture"]; t = m["teams"]; g = m["goals"]; L = m["league"]
            fid = f["id"]
            elapsed = f["status"]["elapsed"] or 0

            if fid in signaled_ids:
                continue

            if elapsed in WINDOW_20:
                gh, ga = g["home"] or 0, g["away"] or 0
                total = gh + ga
                if total in (2, 3):
                    # Линия по правилу
                    bet_line = "ТБ3" if total == 2 else "ТБ4"

                    # если подключишь источник кэфов — сюда:
                    odds = get_over_odds(fid, bet_line)  # None на текущем тарифе

                    # Если нужны фильтры кф — раскомментируй:
                    # if odds is not None and not (ODDS_MIN <= odds <= ODDS_MAX):
                    #     continue

                    rec = {
                        "fixture_id": fid,
                        "home": t["home"]["name"],
                        "away": t["away"]["name"],
                        "league": L["name"],
                        "country": L["country"],
                        "minute": int(elapsed),
                        "goals_home": gh,
                        "goals_away": ga,
                        "total_at_signal": total,
                        "bet_line": bet_line,
                        "odds": odds,
                        "ts": int(now_local().timestamp())
                    }
                    signals.append(rec)
                    signaled_ids.add(fid)
                    save_state()

                    send(format_signal_msg(rec))
                    log.info("Signal sent for %s - %s (fid=%s)", rec['home'], rec['away'], fid)
        except Exception as e:
            log.error(f"scan_and_signal item error: {e}")

# ================== Отчёты ==================

def settle_one(rec):
    """
    Возвращает (is_settled, is_win, gh, ga, final_status)
    """
    res = get_fixture_result(rec["fixture_id"])
    if not res:
        return False, False, None, None, None
    st, gh, ga = res
    if st != "FT":
        return False, False, gh, ga, st

    total = (gh or 0) + (ga or 0)
    if rec["bet_line"] == "ТБ3":
        win = total >= 4
    else:  # ТБ4
        win = total >= 5

    return True, win, gh, ga, st

def make_summary(records):
    """
    Считает сводку для списка сигналов (уже отфильтрованных по периоду).
    Для каждой записи запрашиваем итог и считаем P/L.
    """
    played = 0
    wins   = 0
    losses = 0
    pnl    = 0.0
    avg_odds_accum = 0.0
    avg_odds_count = 0

    lines = []
    for i, rec in enumerate(records, 1):
        ok, win, gh, ga, st = settle_one(rec)
        if not ok:
            lines.append(f"#{i:02d} ⏳ {rec['home']} — {rec['away']} | статус: {st or 'нет данных'}")
            continue

        played += 1
        if win:
            wins += 1
            pnl += STAKE_UNITS
            mark = "✅✅✅"
        else:
            losses += 1
            pnl -= STAKE_UNITS
            mark = "❌❌❌"

        odds_str = f" ({rec['odds']})" if rec.get("odds") else ""

        lines.append(
            f"#{i:02d} {mark} — {STAKE_UNITS:+} ед. "
            f"{rec['country_flag'] if 'country_flag' in rec else ''}"
            f"{odds_str}"
        )

        if rec.get("odds"):
            avg_odds_accum += float(rec["odds"])
            avg_odds_count += 1

    passrate = (wins / played * 100.0) if played else 0.0
    avg_odds = (avg_odds_accum / avg_odds_count) if avg_odds_count else None

    header = [
        f"{wins}✅ / {losses}❌ / {played - wins - losses}⚪",
        f"🧮 Проходимость: {passrate:.0f}%",
        f"💰 Прибыль: {pnl:.2f} ед.",
        f"🧩 Средний кф: {avg_odds:.2f}" if avg_odds else "🧩 Средний кф: n/a",
        "─────────────"
    ]
    return "\n".join(header + lines), played, pnl

def send_daily_report():
    tz = pytz.timezone(TIMEZONE)
    today = now_local().date()

    today_records = []
    for rec in signals:
        ts = rec.get("ts")
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz)
        if dt.date() == today:
            today_records.append(rec)

    title = "📊 *Отчёт за день*"
    body, played, pnl = make_summary(today_records)
    send(f"{title}\n{body}")

def send_weekly_monthly_reports():
    tz = pytz.timezone(TIMEZONE)
    now = now_local()

    # последние 7 дней (включая сегодня)
    week_start = (now - timedelta(days=6)).date()
    week_records = []
    for rec in signals:
        ts = rec.get("ts")
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz)
        if dt.date() >= week_start:
            week_records.append(rec)
    body_week, _, _ = make_summary(week_records)
    send(f"📈 *Неделя (последние 7 дней)*\n{body_week}")

    # текущий календарный месяц
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_records = []
    for rec in signals:
        ts = rec.get("ts")
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz)
        if dt >= month_start:
            month_records.append(rec)
    body_month, _, _ = make_summary(month_records)
    send(f"📅 *Месяц (текущий)*\n{body_month}")

# ================== Команды Telegram ==================

@bot.message_handler(commands=['status'])
def cmd_status(message):
    try:
        now = now_local()
        tz = TIMEZONE
        today = now.date()
        today_count = 0
        for rec in signals:
            ts = rec.get("ts")
            if not ts:
                continue
            dt = datetime.fromtimestamp(ts, pytz.timezone(tz))
            if dt.date() == today:
                today_count += 1

        text = [
            "🩺 *Статус бота*",
            f"⏱ Время (локально): {now.strftime('%Y-%m-%d %H:%M')}",
            f"🌍 TIMEZONE: {TIMEZONE}",
            f"🔎 Окно: ~20' (19..22 мин)",
            f"🎯 Фильтр голов: 2/3",
            f"💵 Кф фильтр (если доступен): {ODDS_MIN:.2f}–{ODDS_MAX:.2f}",
            f"🧾 Сигналов за сегодня: {today_count}",
            f"📁 signals.json: {'есть' if os.path.exists(STATE_FILE) else 'нет'}",
        ]
        bot.reply_to(message, "\n".join(text), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"Ошибка /status: {e}")

@bot.message_handler(commands=['report'])
def cmd_report(message):
    try:
        send_daily_report()
        bot.reply_to(message, "📨 Отчёт за сегодня отправлен.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка /report: {e}")

@bot.message_handler(commands=['test_signal'])
def cmd_test_signal(message):
    try:
        now = now_local()
        fake = {
            "fixture_id": 999999,
            "home": "Test FC",
            "away": "Debug United",
            "league": "DEBUG League",
            "country": "DEBUG",
            "minute": 20,
            "goals_home": 1,
            "goals_away": 1,
            "total_at_signal": 2,
            "bet_line": "ТБ3",
            "odds": 1.75,
            "ts": int(now.timestamp())
        }
        signals.append(fake)
        signaled_ids.add(fake["fixture_id"])
        save_state()
        bot.reply_to(message, "✅ Тестовый сигнал добавлен (fid=999999). Запусти /report — увидишь в отчёте.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка /test_signal: {e}")

# ================== RUN ==================

if __name__ == "__main__":
    # Поднять HTTP-сервер + приём TG-команд в отдельных потоках
    Thread(target=run_http,    daemon=True).start()
    Thread(target=run_telebot, daemon=True).start()

    load_state()
    send("🚀 Бот запущен — новая версия!")
    send("✅ Режим: сигналы при 2/3 голах (~20'), отчёт 23:30.\nНедельная и месячная сводки включены.")

    while True:
        try:
            # Диагностический тик — видно, что цикл жив
            log.info(f"Tick: {now_local().strftime('%Y-%m-%d %H:%M')}")

            # Сканер лайвов
            scan_and_signal()

            # Отчёты по времени (окно 23:30..23:35)
            now = now_local()
            if now.hour == 23 and 30 <= now.minute <= 35:
                # дневной
                send_daily_report()

                # еженедельно: например, по воскресеньям
                if now.weekday() == 6:  # 0=Пн..6=Вс
                    send_weekly_monthly_reports()

                # очистим состояние на новый день
                # (если не хочешь очищать — можешь убрать)
                # signals.clear()
                # signaled_ids.clear()
                save_state()

                # чтобы не продублировать в этом же окне
                time.sleep(60)

            time.sleep(POLL_SECONDS)
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(POLL_SECONDS)
