# -*- coding: utf-8 -*-
"""
Эконом-бот: сигналы на ~20' (ровно 2 гола) + отчёт в 23:30 с прибылью.
— Каждые 15 минут: 1 запрос /fixtures?live=all
— Сигналы: только на ~20'
— В отчёте: для каждого сигнала 1 запрос /fixtures?id=... чтобы узнать финальный счёт
"""

import os, sys, time, json, logging
from datetime import datetime
import pytz, requests, telebot

# ===== Секреты из окружения =====
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")
TIMEZONE  = "Europe/Minsk"
if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")
CHAT_ID = int(CHAT_ID)

# ===== Параметры эконом-режима =====
POLL_SECONDS = 15 * 60              # 1 запрос раз в 15 минут ≈ 96/сутки
WINDOW_20 = range(19, 23)           # считаем «около 20'»

# ===== Логи/файлы =====
LOG_FILE = "bot.log"
SIGNALS_FILE = "signals.json"       # храним сигналы за текущий день

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("econ-bot")

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 15

# Память за день
signals = []          # [{fixture_id, home, away, league, country, minute, goals_home, goals_away}]
signaled_ids = set()  # чтобы не дублировать сигнал

# ===== Утилиты =====
def now_local():
    return datetime.now(pytz.timezone(TIMEZONE))

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def save_state():
    try:
        with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump({"signals": signals, "signaled_ids": list(signaled_ids)}, f, ensure_ascii=False)
    except Exception as e:
        log.error(f"save_state error: {e}")

def load_state():
    global signals, signaled_ids
    if not os.path.exists(SIGNALS_FILE):
        return
    try:
        data = json.load(open(SIGNALS_FILE, "r", encoding="utf-8"))
        signals = data.get("signals", [])
        signaled_ids = set(data.get("signaled_ids", []))
    except Exception as e:
        log.error(f"load_state error: {e}")

def get_live():
    """1 запрос — все лайвы"""
    try:
        r = API.get("https://v3.football.api-sports.io/fixtures?live=all", timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("fixtures HTTP %s %s", r.status_code, r.text[:160])
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error(f"get_live error: {e}")
        return []

def get_fixture_result(fid: int):
    """1 запрос — итог конкретного матча"""
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

# ===== Основная логика =====
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
                if gh + ga == 2:
                    # Сигнал — записываем и шлём
                    rec = {
                        "fixture_id": fid,
                        "home": t["home"]["name"],
                        "away": t["away"]["name"],
                        "league": L["name"],
                        "country": L["country"],
                        "minute": int(elapsed),
                        "goals_home": gh,
                        "goals_away": ga,
                    }
                    signals.append(rec)
                    signaled_ids.add(fid)
                    save_state()

                    send(
                        "⚪ *Ставка!*\n"
                        f"🏆 {rec['country']} — {rec['league']}\n"
                        f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                        f"⏱ ~{elapsed}'  (условие: ровно 2 гола)\n"
                        "─────────────"
                    )
                    log.info("Signal sent for %s - %s (fid=%s)", rec['home'], rec['away'], fid)
        except Exception as e:
            log.error(f"scan_and_signal item error: {e}")

def send_daily_report():
    """В 23:30: пройтись по сигналам, запросить итог каждого матча и посчитать прибыль."""
    played = not_played = 0
    profit = 0.0
    lines = ["📊 *Отчёт за день*"]

    for rec in signals:
        res = get_fixture_result(rec["fixture_id"])
        if not res:
            lines.append(f"{rec['home']} — {rec['away']} | результат недоступен")
            continue
        st, gh, ga = res
        total = gh + ga
        if st == "FT":
            if total > 3:
                played += 1
                pnl = +1.0  # считаем при фиксированной ставке 1 ед. прибыли
                lines.append(f"{rec['home']} {gh}-{ga} {rec['away']} | ✅ Сыграло | +{pnl:.2f}")
            else:
                not_played += 1
                pnl = -1.0
                lines.append(f"{rec['home']} {gh}-{ga} {rec['away']} | ❌ Не сыграло | {pnl:.2f}")
            profit += pnl
        else:
            lines.append(f"{rec['home']} — {rec['away']} | статус: {st}")

    lines.append("─────────────")
    lines.append(f"Всего ставок: {len(signals)}")
    lines.append(f"Сыграло: {played}  |  Не сыграло: {not_played}")
    lines.append(f"Итог: {profit:+.2f}")

    send("\n".join(lines))

# ===== RUN =====
if __name__ == "__main__":
    load_state()
    send("🚀 Бот запущен — новая версия!")
    send("✅ Бот запущен (эконом: сигнал на ~20', отчёт в 23:30).")
    while True:
        ...
        try:
            scan_and_signal()

            # Разовый отчёт в 23:30 по Минску
            now = now_local()
            if now.hour == 23 and now.minute == 30:
                send_daily_report()
                # очистим состояние на новый день
                signals.clear()
                signaled_ids.clear()
                save_state()
                time.sleep(60)  # чтобы не отправить отчёт дважды в ту же минуту

            time.sleep(POLL_SECONDS)
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(POLL_SECONDS)
