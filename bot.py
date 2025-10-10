# -*- coding: utf-8 -*-
"""
Render-ready Telegram bot (pre-match scanner).
Стратегия: если есть явный фаворит по 1X2, даём сигнал на "1-й тайм ТБ 0.5".
- Авто-скан в 08:00 (Europe/Warsaw)
- Дневной отчёт в 23:30
- Ручной запуск: /scan
- Открываем HTTP-порт (Flask), чтобы Render не гасил процесс
- Берём ВСЕ лиги (без фильтра стран), как ты просила для теста
"""

import os
import sys
import time
import json
import logging
from datetime import datetime

import pytz
import requests
import telebot
from threading import Thread
from flask import Flask

# ========= ТВОИ ДАННЫЕ (можно править тут или через ENV) =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7980925106:AAG-kjxNUWjRGN0YzyUEaS_Wyd2rfWmQ6Nc")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "1244865850"))
API_FOOTBALL_KEY   = os.getenv("API_FOOTBALL_KEY", "a5643739fb001333ba7b99b5bb67508e")
TZ                 = os.getenv("TZ", "Europe/Warsaw")

# Стратегия (можно поднять/опустить пороги)
FAVORITE_MAX_ODDS = float(os.getenv("FAVORITE_MAX_ODDS", "1.70"))  # макс. кэф фаворита по 1X2
FH_O05_MIN_ODDS   = float(os.getenv("FH_O05_MIN_ODDS", "1.20"))    # мин. кэф на ТБ0.5 (1Т), если есть
FH_O05_MAX_ODDS   = float(os.getenv("FH_O05_MAX_ODDS", "1.80"))    # макс. кэф на ТБ0.5 (1Т), если есть

# Сразу сделать скан после запуска (удобно для теста)
RUN_ON_START = os.getenv("RUN_ON_START", "1") == "1"

# ========= Логи/состояние =========
LOG_FILE   = "bot.log"
STATE_FILE = "signals.json"

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("scanner")

# ========= HTTP (Render health) =========
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ========= Telegram =========
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not API_FOOTBALL_KEY:
    sys.exit("❌ Нужны TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_FOOTBALL_KEY")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="Markdown")

def send(text: str):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ========= API-Football =========
API = requests.Session()
API.headers.update({"x-apisports-key": API_FOOTBALL_KEY})
DEFAULT_TIMEOUT = 20
BASE = "https://v3.football.api-sports.io"

def api_get(endpoint: str, params: dict):
    url = f"{BASE}/{endpoint}"
    try:
        r = API.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("HTTP %s %s %s", r.status_code, url, r.text[:200])
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error(f"api_get {endpoint} err: {e}")
        return []

# ========= Память за день =========
signals_today = []
signaled_fixtures = set()

def load_state():
    global signals_today, signaled_fixtures
    if not os.path.exists(STATE_FILE):
        return
    try:
        data = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        if data.get("day") == now_local().strftime("%Y-%m-%d"):
            signals_today = data.get("signals", [])
            signaled_fixtures = set(data.get("signaled", []))
    except Exception as e:
        log.error(f"load_state error: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "day": now_local().strftime("%Y-%m-%d"),
                "signals": signals_today,
                "signaled": list(signaled_fixtures)
            }, f, ensure_ascii=False)
    except Exception as e:
        log.error(f"save_state error: {e}")

# ========= Утилиты =========
def now_local():
    return datetime.now(pytz.timezone(TZ))

def fmt_team(t):  # team dict -> name
    return (t.get("name") or "").strip()

def choose_favorite_from_1x2(bet: dict):
    """
    bet = {'name': 'Match Winner', 'values': [{'value':'Home','odd':'1.45'}, {'value':'Draw',...}, {'value':'Away','odd':'6.50'}]}
    -> ('Home'/'Away', odd) или (None, None)
    """
    if not bet or "values" not in bet:
        return None, None
    home_odd = away_odd = None
    for v in bet["values"]:
        val = (v.get("value") or "").lower()
        try:
            odd = float(v.get("odd"))
        except:
            continue
        if val in ("home", "1"):
            home_odd = odd
        elif val in ("away", "2"):
            away_odd = odd
    if home_odd is None and away_odd is None:
        return None, None
    if home_odd is not None and (away_odd is None or home_odd <= away_odd):
        return "Home", home_odd
    if away_odd is not None:
        return "Away", away_odd
    return None, None

def find_bet(bets: list, key_words: list):
    """ Найти bet по ключевым словам в названии (без учёта регистра). """
    for b in bets or []:
        name = (b.get("name") or "").lower()
        if all(kw.lower() in name for kw in key_words):
            return b
    return None

def get_fh_over05_odds(bets: list):
    """
    Ищем коэффициент на 'Over 0.5 - 1st Half'
    """
    candidate = None
    for b in bets or []:
        name = (b.get("name") or "").lower()
        if ("over" in name and "under" in name and "1st" in name and "half" in name) or ("1st half" in name and "goals" in name):
            candidate = b
            break
    if not candidate:
        return None
    for v in candidate.get("values", []):
        label = (v.get("value") or "").lower().replace(" ", "")
        if "over" in label and ("0.5" in label or "0,5" in label):
            try:
                return float(v.get("odd"))
            except:
                return None
    return None

def odds_for_fixture(fixture_id: int):
    """
    Возвращаем:
      fav_side ('Home'/'Away') или None,
      fav_odds,
      fh_over05_odds (или None)
    """
    data = api_get("odds", {"fixture": fixture_id})
    if not data:
        return None, None, None

    fav_side = None
    fav_odds = None
    fh_o05 = None

    for bm in data:
        bets = bm.get("bets") or []
        bet_1x2 = find_bet(bets, ["match", "winner"]) or find_bet(bets, ["1x2"])
        fside, fodd = choose_favorite_from_1x2(bet_1x2)
        if fside and fodd:
            if (fav_odds is None) or (fodd < fav_odds):
                fav_side = fside
                fav_odds = fodd
        fh = get_fh_over05_odds(bets)
        if fh is not None:
            if fh_o05 is None or fh < fh_o05:
                fh_o05 = fh

    return fav_side, fav_odds, fh_o05

def fixtures_today():
    """
    Все матчи на сегодня (статусы: NS/TBD/PST), без ограничений по лигам/странам.
    """
    d = now_local().strftime("%Y-%m-%d")
    data = api_get("fixtures", {"date": d, "timezone": TZ})
    out = []
    for m in data:
        status = ((m.get("fixture") or {}).get("status") or {}).get("short")
        if status in ("NS", "TBD", "PST"):
            out.append(m)
    return out

def build_signal_text(fix, fav_side, fav_odds, fh_o05_odds):
    f = fix["fixture"]; l = fix["league"]; t = fix["teams"]
    dt = datetime.fromtimestamp(f["timestamp"], pytz.timezone(TZ)).strftime("%H:%M")
    home = fmt_team(t["home"]); away = fmt_team(t["away"])
    league_line = f"🏆 {l['country']} — {l['name']} (сезон {l['season']})"
    fav_line = f"⭐ Фаворит: {'Дом' if fav_side=='Home' else 'Гости'} @ {fav_odds:.2f}"
    o05_line = f"⏱ 1Т ТБ 0.5: {fh_o05_odds:.2f}" if fh_o05_odds else "⏱ 1Т ТБ 0.5: нет котировок"
    return (
        "⚪ *Сигнал (прематч)*\n"
        f"{league_line}\n"
        f"{home} — {away}  |  {dt}\n"
        f"{fav_line}\n"
        f"{o05_line}\n"
        "─────────────"
    )

def passes_strategy(fav_side, fav_odds, fh_o05_odds):
    """
    Условия:
      1) есть фаворит и его 1х2 <= FAVORITE_MAX_ODDS
      2) если есть котировка на 1Т ТБ0.5 — она в [FH_O05_MIN_ODDS ; FH_O05_MAX_ODDS]
         если котировки нет — сигнал всё равно отправляем (по фавориту).
    """
    if not fav_side or not fav_odds:
        return False
    if fav_odds > FAVORITE_MAX_ODDS:
        return False
    if fh_o05_odds is None:
        return True
    return FH_O05_MIN_ODDS <= fh_o05_odds <= FH_O05_MAX_ODDS

# ========= Скан/отчёт =========
def run_scan():
    count_checked = 0
    count_signals = 0
    fixtures = fixtures_today()
    for m in fixtures:
        try:
            fid = m["fixture"]["id"]
        except:
            continue
        if fid in signaled_fixtures:
            continue
        fav_side, fav_odds, fh_o05 = odds_for_fixture(fid)
        if passes_strategy(fav_side, fav_odds, fh_o05):
            send(build_signal_text(m, fav_side, fav_odds, fh_o05))
            rec = {
                "fixture_id": fid,
                "home": fmt_team(m["teams"]["home"]),
                "away": fmt_team(m["teams"]["away"]),
                "league": m["league"]["name"],
                "country": m["league"]["country"],
                "fav_side": fav_side,
                "fav_odds": fav_odds,
                "fh_o05": fh_o05,
                "kickoff": m["fixture"]["timestamp"],
            }
            signals_today.append(rec)
            signaled_fixtures.add(fid)
            count_signals += 1
        count_checked += 1

    save_state()
    send(f"🔎 Скан закончен: проверено {count_checked}, сигналов {count_signals}.")

def send_daily_report():
    lines = ["📊 *Отчёт за день (прематч)*"]
    lines.append(f"Дата: {now_local().strftime('%Y-%m-%d')}")
    lines.append(f"Сигналов отправлено: {len(signals_today)}")
    if not signals_today:
        lines.append("За сегодня сигналов не было.")
        send("\n".join(lines)); return

    for i, s in enumerate(signals_today, 1):
        tm = datetime.fromtimestamp(s["kickoff"], pytz.timezone(TZ)).strftime("%H:%M")
        fav = "Дом" if s["fav_side"] == "Home" else "Гости"
        o05 = f"{s['fh_o05']:.2f}" if s["fh_o05"] else "нет"
        lines.append(f"{i:02d}. {s['home']} — {s['away']} [{tm}] | fav {fav} @{s['fav_odds']:.2f} | 1Т ТБ0.5: {o05}")

    send("\n".join(lines))

# ========= Команды =========
@bot.message_handler(commands=["start", "help"])
def on_help(msg):
    send(
        "Привет! Я сканирую прематч по стратегии *фаворит → 1Т ТБ 0.5*.\n\n"
        "Команды:\n"
        "• /scan — запустить скан сейчас\n"
        "• /status — статус и параметры\n"
        f"Скан ежедневно в 08:00, отчёт в 23:30 (TZ: {TZ})."
    )

@bot.message_handler(commands=["status"])
def on_status(msg):
    lines = [
        "ℹ️ *Статус*",
        f"TZ: {TZ}",
        f"Фаворит по 1х2 ≤ {FAVORITE_MAX_ODDS:.2f}",
        f"1Т ТБ0.5 коридор: [{FH_O05_MIN_ODDS:.2f} ; {FH_O05_MAX_ODDS:.2f}]",
        f"Сегодня сигналов: {len(signals_today)}",
    ]
    send("\n".join(lines))

@bot.message_handler(commands=["scan"])
def on_scan(msg):
    send("Запустил сканирование ✅")
    try:
        run_scan()
    except Exception as e:
        log.error(f"/scan error: {e}")
        send("❌ Ошибка при сканировании, см. логи.")

# ========= Планировщик (простая проверка времени) =========
def timers_loop():
    last_scan_mark = None
    last_report_mark = None
    tz = pytz.timezone(TZ)

    while True:
        try:
            now = datetime.now(tz)
            day_key = now.strftime("%Y-%m-%d")

            # 08:00 — авто-скан (один раз в день)
            if now.hour == 8 and now.minute == 0:
                if last_scan_mark != day_key:
                    send("⏰ 08:00 — авто-скан запускается.")
                    run_scan()
                    last_scan_mark = day_key

            # 23:30 — дневной отчёт
            if now.hour == 23 and now.minute == 30:
                if last_report_mark != day_key:
                    send_daily_report()
                    last_report_mark = day_key

        except Exception as e:
            log.error(f"timers_loop error: {e}")

        time.sleep(30)

# ========= ENTRY =========
if __name__ == "__main__":
    # HTTP для Render
    Thread(target=run_http, daemon=True).start()

    load_state()

    send("🚀 Бот запущен (прематч, фаворит → 1Т ТБ 0.5).")
    send("ℹ️ Авто-скан 08:00, отчёт 23:30. Для теста: /scan")

    # Разовый скан сразу после старта (удобно проверить)
    if RUN_ON_START:
        try:
            send("↻ RUN_ON_START=1 — делаю разовый скан сейчас.")
            run_scan()
        except Exception as e:
            log.error(f"RUN_ON_START scan error: {e}")

    Thread(target=timers_loop, daemon=True).start()

    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        log.error(f"bot.infinity_polling error: {e}")
