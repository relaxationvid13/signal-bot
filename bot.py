# -*- coding: utf-8 -*-
"""
Render-ready Telegram bot (pre-match scanner).
Стратегия: явный фаворит по 1X2 -> сигнал на "1-й тайм ТБ 0.5".
- Автоскан 08:00 Europe/Warsaw
- Дневной отчёт 23:30
- /scan (ручной), /status, /debug
- Правильный парсинг odds: response[0..]["bookmakers"][..]["bets"][..]["values"][..]
"""

import os, sys, time, json, logging
from datetime import datetime
from threading import Thread

import pytz, requests, telebot
from flask import Flask

# ====== ТВОИ ДАННЫЕ (для теста оставляю захардкожено; позже лучше перенести в ENV) ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7980925106:AAG-kjxNUWjRGN0YzyUEaS_Wyd2rfWmQ6Nc")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "1244865850"))
API_FOOTBALL_KEY   = os.getenv("API_FOOTBALL_KEY", "a5643739fb001333ba7b99b5bb67508e")
TZ                 = os.getenv("TZ", "Europe/Warsaw")

# Порог фаворита и коридор для 1Т ТБ0.5
FAVORITE_MAX_ODDS = float(os.getenv("FAVORITE_MAX_ODDS", "1.80"))
FH_O05_MIN_ODDS   = float(os.getenv("FH_O05_MIN_ODDS", "1.15"))
FH_O05_MAX_ODDS   = float(os.getenv("FH_O05_MAX_ODDS", "1.85"))

# Сразу запустить скан после старта
RUN_ON_START = os.getenv("RUN_ON_START", "1") == "1"

# ====== Логи/состояние ======
LOG_FILE, STATE_FILE = "bot.log", "signals.json"
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("scanner")

# ====== HTTP для Render ======
app = Flask(__name__)
@app.get("/")
def healthcheck(): return "ok"
def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ====== Telegram ======
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not API_FOOTBALL_KEY:
    sys.exit("❌ Нужны TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_FOOTBALL_KEY")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="Markdown")
def send(txt: str):
    try: bot.send_message(TELEGRAM_CHAT_ID, txt)
    except Exception as e: log.error(f"Telegram send error: {e}")

# ====== API-Football ======
API = requests.Session()
API.headers.update({"x-apisports-key": API_FOOTBALL_KEY})
DEFAULT_TIMEOUT = 25
BASE = "https://v3.football.api-sports.io"

def api_get(endpoint, params):
    url = f"{BASE}/{endpoint}"
    try:
        r = API.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("HTTP %s %s %s", r.status_code, url, r.text[:200])
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error(f"api_get {endpoint} error: {e}")
        return []

# ====== Память на день ======
signals_today, signaled_ids = [], set()

def now_local(): return datetime.now(pytz.timezone(TZ))
def fmt_team(t): return (t.get("name") or "").strip()

def load_state():
    global signals_today, signaled_ids
    if not os.path.exists(STATE_FILE): return
    try:
        data = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        if data.get("day") == now_local().strftime("%Y-%m-%d"):
            signals_today = data.get("signals", [])
            signaled_ids = set(data.get("signaled", []))
    except Exception as e:
        log.error(f"load_state error: {e}")

def save_state():
    try:
        json.dump({
            "day": now_local().strftime("%Y-%m-%d"),
            "signals": signals_today,
            "signaled": list(signaled_ids)
        }, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        log.error(f"save_state error: {e}")

# ====== Парсинг коэффициентов (исправлено) ======
def choose_favorite_from_1x2(bet):
    """ bet['values'] -> [{'value':'Home','odd':'1.45'},{'value':'Draw',...},{'value':'Away','odd':'6.50'}] """
    if not bet or "values" not in bet: return None, None
    home_odd = away_odd = None
    for v in bet["values"]:
        label = (v.get("value") or "").lower()
        try: odd = float(v.get("odd"))
        except: continue
        if label in ("home","1"): home_odd = odd
        elif label in ("away","2"): away_odd = odd
    if home_odd is None and away_odd is None: return None, None
    if home_odd is not None and (away_odd is None or home_odd <= away_odd): return "Home", home_odd
    if away_odd is not None: return "Away", away_odd
    return None, None

def get_fh_over05_odds_from_bet(bet):
    """ ищем Over 0.5 в 1st Half внутри одного bet """
    for v in bet.get("values", []):
        label = (v.get("value") or v.get("name") or "").lower().replace(" ", "")
        if "over" in label and ("0.5" in label or "0,5" in label):
            try: return float(v.get("odd") or v.get("price"))
            except: return None
    return None

def odds_for_fixture(fixture_id: int):
    """
    Возвращаем:
      fav_side ('Home'/'Away') или None,
      fav_odds,
      fh_over05_odds (или None)
    Структура ответа: response[*]['bookmakers'][*]['bets'][*]['values'][*]
    """
    data = api_get("odds", {"fixture": fixture_id})
    if not data: return None, None, None

    fav_side, fav_odds, fh_o05 = None, None, None

    for root in data:
        for bm in root.get("bookmakers", []) or []:
            bets = bm.get("bets") or []
            # 1x2 / Match Winner
            bet_1x2 = None
            for b in bets:
                name = (b.get("name") or "").lower()
                if ("match" in name and "winner" in name) or ("1x2" in name):
                    bet_1x2 = b; break
            fs, fo = choose_favorite_from_1x2(bet_1x2)
            if fs and fo and (fav_odds is None or fo < fav_odds):
                fav_side, fav_odds = fs, fo
            # 1st half Over/Under
            for b in bets:
                name = (b.get("name") or "").lower()
                if (("over" in name and "under" in name) or "total" in name or "goals" in name) and ("1st" in name and "half" in name):
                    val = get_fh_over05_odds_from_bet(b)
                    if val is not None and (fh_o05 is None or val < fh_o05):
                        fh_o05 = val

    return fav_side, fav_odds, fh_o05

# ====== Матчи на сегодня ======
def fixtures_today():
    """ Все NS/TBD/PST на сегодня, без фильтра лиг. """
    d = now_local().strftime("%Y-%m-%d")
    arr = api_get("fixtures", {"date": d, "timezone": TZ})
    out = []
    for m in arr:
        st = ((m.get("fixture") or {}).get("status") or {}).get("short")
        if st in ("NS","TBD","PST"):
            out.append(m)
    return out

# ====== Логика отбора ======
def passes_strategy(fav_side, fav_odds, fh_o05_odds):
    # 1) обязателен фаворит по 1X2
    if not fav_side or not fav_odds: return False
    if fav_odds > FAVORITE_MAX_ODDS: return False
    # 2) если котировка на 1Т ТБ0.5 есть — проверяем коридор
    if fh_o05_odds is None: return True
    return (FH_O05_MIN_ODDS <= fh_o05_odds <= FH_O05_MAX_ODDS)

def build_signal_text(fix, fav_side, fav_odds, fh_o05_odds):
    f, l, t = fix["fixture"], fix["league"], fix["teams"]
    dt = datetime.fromtimestamp(f["timestamp"], pytz.timezone(TZ)).strftime("%H:%M")
    home, away = fmt_team(t["home"]), fmt_team(t["away"])
    league_line = f"🏆 {l['country']} — {l['name']} (сезон {l['season']})"
    fav_line = f"⭐ Фаворит: {'Дом' if fav_side=='Home' else 'Гости'} @ {fav_odds:.2f}"
    o05_line = f"⏱ 1Т ТБ 0.5: {fh_o05_odds:.2f}" if fh_o05_odds else "⏱ 1Т ТБ 0.5: нет котировок"
    return ("⚪ *Сигнал (прематч)*\n"
            f"{league_line}\n"
            f"{home} — {away}  |  {dt}\n"
            f"{fav_line}\n"
            f"{o05_line}\n"
            "─────────────")

# ====== Скан/отчёт/диагностика ======
def run_scan():
    fixtures = fixtures_today()
    checked = with_1x2 = with_fh = 0
    made = 0

    for m in fixtures:
        fid = (m.get("fixture") or {}).get("id")
        if not fid or fid in signaled_ids: continue

        fav_side, fav_odds, fh_o05 = odds_for_fixture(fid)
        checked += 1
        if fav_side and fav_odds: with_1x2 += 1
        if fh_o05 is not None: with_fh += 1

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
            signaled_ids.add(fid)
            made += 1

        time.sleep(0.1)  # щадим бесплатный тариф

    save_state()
    send(f"🔎 Скан завершён: матчей {len(fixtures)}, проверено {checked}, c 1X2: {with_1x2}, c 1Т О0.5: {with_fh}, сигналов: {made}.")

def send_daily_report():
    lines = ["📊 *Отчёт за день (прематч)*",
             f"Дата: {now_local().strftime('%Y-%m-%d')}",
             f"Сигналов отправлено: {len(signals_today)}"]
    if not signals_today:
        send("\n".join(lines)); return
    for i, s in enumerate(signals_today, 1):
        tm = datetime.fromtimestamp(s["kickoff"], pytz.timezone(TZ)).strftime("%H:%M")
        fav = "Дом" if s["fav_side"] == "Home" else "Гости"
        o05 = f"{s['fh_o05']:.2f}" if s["fh_o05"] else "нет"
        lines.append(f"{i:02d}. {s['home']} — {s['away']} [{tm}] | fav {fav} @{s['fav_odds']:.2f} | 1Т ТБ0.5: {o05}")
    send("\n".join(lines))

# ====== Telegram команды ======
@bot.message_handler(commands=["start","help"])
def on_help(m):
    send("Привет! Сканирую прематч по стратегии *фаворит → 1Т ТБ0.5*.\n"
         "Команды:\n• /scan — запустить скан сейчас\n• /status — статус и пороги\n• /debug — диагностика API\n"
         f"Автоскан 08:00, отчёт 23:30 (TZ: {TZ}).")

@bot.message_handler(commands=["status"])
def on_status(m):
    send("ℹ️ *Статус*\n"
         f"TZ: {TZ}\n"
         f"Фаворит по 1х2 ≤ {FAVORITE_MAX_ODDS:.2f}\n"
         f"1Т ТБ0.5 коридор: [{FH_O05_MIN_ODDS:.2f} ; {FH_O05_MAX_ODDS:.2f}]\n"
         f"Сегодня сигналов: {len(signals_today)}")

@bot.message_handler(commands=["debug"])
def on_debug(m):
    d = now_local().strftime("%Y-%m-%d")
    fixtures = fixtures_today()
    total = len(fixtures)
    with1x2 = withfh = 0
    for fx in fixtures:
        fid = (fx.get("fixture") or {}).get("id")
        if not fid: continue
        fs, fo, fh = odds_for_fixture(fid)
        if fs and fo: with1x2 += 1
        if fh is not None: withfh += 1
        time.sleep(0.05)
    send(f"🛠 Debug {d}: матчей={total}, с 1X2={with1x2}, с 1Т О0.5={withfh}. "
         "Если 1X2 мало — это ограничение покрытия odds на бесплатном тарифе.")

@bot.message_handler(commands=["scan"])
def on_scan(m):
    send("Запускаю сканирование ✅")
    try: run_scan()
    except Exception as e:
        log.error(f"/scan error: {e}")
        send("❌ Ошибка при сканировании, см. логи.")

# ====== Планировщик ======
def timers_loop():
    last_scan_key = last_report_key = None
    tz = pytz.timezone(TZ)
    while True:
        try:
            now = datetime.now(tz); key = now.strftime("%Y-%m-%d")
            if now.hour == 8 and now.minute == 0 and last_scan_key != key:
                send("⏰ 08:00 — авто-скан.")
                run_scan(); last_scan_key = key
            if now.hour == 23 and now.minute == 30 and last_report_key != key:
                send_daily_report(); last_report_key = key
        except Exception as e:
            log.error(f"timers error: {e}")
        time.sleep(30)

# ====== RUN ======
if __name__ == "__main__":
    Thread(target=run_http, daemon=True).start()
    load_state()
    send("🚀 Бот запущен (прематч, фаворит → 1Т ТБ0.5).")
    send("ℹ️ Автоскан 08:00, отчёт 23:30. Для теста: /scan, диагностика: /debug")
    if RUN_ON_START:
        try:
            send("↻ RUN_ON_START=1 — запускаю разовый скан.")
            run_scan()
        except Exception as e:
            log.error(f"startup scan error: {e}")
    Thread(target=timers_loop, daemon=True).start()
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        log.error(f"polling error: {e}")
