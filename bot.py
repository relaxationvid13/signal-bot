# -*- coding: utf-8 -*-
"""
Сигнальный бот:
- Условие: уже забито 2 или 3 гола.
  * при 2 голах проверяем ТБ(3)
  * при 3 голах проверяем ТБ(4)
- Кэф должен быть от LOW_ODDS до HIGH_ODDS (включ.)
- Эконом-режим: один запрос /fixtures?live=all раз в POLL_SECONDS, odds только по кандидатам.
- В 23:30 по Минску — отчёт за день (прибыль по каждому сигналу: win=odds-1, lose=-1).
"""

import os, sys, time, json, logging
from datetime import datetime
from threading import Thread

import pytz
import requests
import telebot

# ---------- Render keep-alive (мини HTTP-сервер) ----------
from flask import Flask
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
# ----------------------------------------------------------


# ======== Секреты и окружение ========
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")
TIMEZONE  = "Europe/Minsk"

if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")
CHAT_ID = int(CHAT_ID)

# ======== Параметры ========
# Как часто тянуть лайвы (минимизируем расход лимита API)
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "900"))   # 900 = 15 минут

# Фильтр по времени (около 20-й). Если не нужен — USE_20_WINDOW=False
USE_20_WINDOW = os.getenv("USE_20_WINDOW", "true").lower() in ("1", "true", "yes")
WINDOW_20 = range(19, 23)   # 19–22'

# Диапазон по коэффициенту
LOW_ODDS  = float(os.getenv("LOW_ODDS",  "1.29"))
HIGH_ODDS = float(os.getenv("HIGH_ODDS", "2.00"))

# ======== Логи и файлы ========
LOG_FILE     = "bot.log"
SIGNALS_FILE = "signals.json"   # сохраняем сигналы и уже отправленные fixture_id

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("signals-bot")

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 15

# ======== Память за день ========
# Сигналы со всеми атрибутами, чтобы потом корректно посчитать профит
signals = []          # [{fixture_id, home, away, league, country, minute, goals_home, goals_away, target_total, odds, bookmaker}]
signaled_ids = set()  # чтобы не слать сигнал по одному матчу дважды


# ======== Утилиты ========
def now_local() -> datetime:
    return datetime.now(pytz.timezone(TIMEZONE))

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def save_state():
    try:
        with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"signals": signals, "signaled_ids": list(signaled_ids)},
                f,
                ensure_ascii=False
            )
    except Exception as e:
        log.error(f"save_state error: {e}")

def load_state():
    global signals, signaled_ids
    if not os.path.exists(SIGNALS_FILE):
        return
    try:
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        signals = data.get("signals", [])
        signaled_ids = set(data.get("signaled_ids", []))
    except Exception as e:
        log.error(f"load_state error: {e}")

def get_live():
    """1 запрос — все матчи в лайве"""
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
    """1 запрос — итог конкретного матча (для отчёта)"""
    try:
        r = API.get(f"https://v3.football.api-sports.io/fixtures?id={fid}", timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        resp = r.json().get("response", []) or []
        if not resp:
            return None
        m = resp[0]
        st = m["fixture"]["status"]["short"]   # e.g. "FT"
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return st, gh, ga
    except Exception as e:
        log.error(f"get_fixture_result({fid}) error: {e}")
        return None

def get_over_odds_for_line(fixture_id: int, target_total: int):
    """
    Возвращает максимальный кэф по рынку Over/Under для "Over {target_total}"
    и имя букмекера. Если данных нет — (None, None).
    """
    try:
        r = API.get(f"https://v3.football.api-sports.io/odds?fixture={fixture_id}",
                    timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        resp = r.json().get("response", []) or []

        best_odd = None
        best_book = None

        # Структура: response -> [{bookmakers: [{name, bets: [{name:"Over/Under", values:[{value:"Over 3", odd:"1.45"}, ...]}]}]}]
        for item in resp:
            for bm in item.get("bookmakers", []):
                bm_name = bm.get("name")
                for bet in bm.get("bets", []):
                    if bet.get("name") != "Over/Under":
                        continue
                    for val in bet.get("values", []):
                        if val.get("value") == f"Over {target_total}":
                            try:
                                odd = float(val.get("odd"))
                            except (TypeError, ValueError):
                                continue
                            if best_odd is None or odd > best_odd:
                                best_odd = odd
                                best_book = bm_name

        return best_odd, best_book
    except Exception as e:
        log.error(f"get_over_odds_for_line({fixture_id},{target_total}) error: {e}")
        return None, None


# ======== Основная логика ========
def scan_and_signal():
    live = get_live()
    for m in live:
        try:
            f = m["fixture"]; t = m["teams"]; g = m["goals"]; L = m["league"]
            fid = f["id"]
            elapsed = f["status"]["elapsed"] or 0

            if fid in signaled_ids:
                continue

            # (опционально) фильтр на «около 20-й»
            if USE_20_WINDOW and elapsed not in WINDOW_20:
                continue

            gh, ga = g["home"] or 0, g["away"] or 0
            total_goals = gh + ga

            # Интересуют только 2 или 3 гола
            if total_goals not in (2, 3):
                continue

            # Выбираем линию: при 2 голах ТБ(3), при 3 голах ТБ(4)
            target_total = 3 if total_goals == 2 else 4

            # Запрос коэффициентов на выбранную линию
            odds, bookmaker = get_over_odds_for_line(fid, target_total)
            if odds is None:
                continue

            # Диапазон по кэфу
            if not (LOW_ODDS <= odds <= HIGH_ODDS):
                continue

            # Условия выполнены — сигнал
            rec = {
                "fixture_id": fid,
                "home": t["home"]["name"],
                "away": t["away"]["name"],
                "league": L["name"],
                "country": L["country"],
                "minute": int(elapsed),
                "goals_home": gh,
                "goals_away": ga,
                "target_total": target_total,
                "odds": float(odds),
                "bookmaker": bookmaker,
            }
            signals.append(rec)
            signaled_ids.add(fid)
            save_state()

            line = f"ТБ({target_total})"
            send(
                "⚪ *Ставка!*\n"
                f"🏆 {rec['country']} — {rec['league']}\n"
                f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                f"⏱ {elapsed}'   |   {line} @ *{rec['odds']:.2f}*"
                + (f"  ({bookmaker})" if bookmaker else "") +
                "\n─────────────"
            )
            log.info("Signal sent: %s - %s, %s @ %.2f (fid=%s)",
                     rec['home'], rec['away'], line, rec['odds'], fid)

        except Exception as e:
            log.error(f"scan_and_signal item error: {e}")


def send_daily_report():
    """
    В 23:30: пройтись по сигналам, запросить итог каждого матча и посчитать прибыль.
    Если финальный total > target_total -> win (прибыль = odds-1), иначе loss (-1).
    """
    total_profit = 0.0
    played = lost = pending = 0
    lines = ["📊 *Отчёт за день*"]

    for rec in signals:
        res = get_fixture_result(rec["fixture_id"])
        if not res:
            pending += 1
            lines.append(f"{rec['home']} — {rec['away']} | результат недоступен")
            continue

        st, gh, ga = res
        match_total = (gh or 0) + (ga or 0)
        target_total = rec.get("target_total", 3)
        odds = float(rec.get("odds", 1.0))

        if st == "FT":
            if match_total > target_total:
                pnl = odds - 1.0
                played += 1
                lines.append(
                    f"{rec['home']} {gh}-{ga} {rec['away']} | ✅ Сыграло | {pnl:+.2f}  (ТБ({target_total}) @ {odds:.2f})"
                )
            else:
                pnl = -1.0
                lost += 1
                lines.append(
                    f"{rec['home']} {gh}-{ga} {rec['away']} | ❌ Не сыграло | {pnl:+.2f}  (ТБ({target_total}) @ {odds:.2f})"
                )
            total_profit += pnl
        else:
            pending += 1
            lines.append(f"{rec['home']} — {rec['away']} | статус: {st}")

    lines.append("─────────────")
    lines.append(f"Всего сигналов: {len(signals)}")
    lines.append(f"Сыграло: {played}  |  Не сыграло: {lost}  |  В ожидании: {pending}")
    lines.append(f"Итог за день: {total_profit:+.2f}")

    send("\n".join(lines))


# ================== RUN ==================
if __name__ == "__main__":
    # Render: поднимаем HTTP, чтобы инстанс считался «живым» на Free-плане
    Thread(target=run_http, daemon=True).start()

    load_state()
    send(
        "🚀 Бот запущен — новая версия!\n"
        f"Фильтр: {'~20 мин' if USE_20_WINDOW else 'без ограничения по минуте'} | "
        f"кэф {LOW_ODDS:.2f}–{HIGH_ODDS:.2f} | опрос каждые {POLL_SECONDS//60} мин."
    )

    while True:
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
