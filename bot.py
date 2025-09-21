# -*- coding: utf-8 -*-
"""
Сигнальный бот (эконом-режим) под Render.

Условия сигналов (новые):
- Если в лайве ровно 2 гола И минута в окне ~20' (19..22) → проверяем рынок ТБ(3)
- Если в лайве ровно 3 гола И минута в окне ~30' (28..33) → проверяем рынок ТБ(4)
- Сигнал шлём только если кэф Over в диапазоне [1.29 ; 2.00] (включительно)

Каждые 15 минут делаем 1 запрос на /fixtures?live=all.
В 23:30 по Минску — красивый отчёт за день.
"""

import os, sys, time, json, logging
from datetime import datetime, date
from threading import Thread

import pytz
import requests
import telebot
from flask import Flask

# ============ HTTP keepalive для Render ============
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ================== Секреты ========================
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")
TIMEZONE  = "Europe/Minsk"

if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")
CHAT_ID = int(CHAT_ID)

# ================ Параметры ========================
POLL_SECONDS   = 15 * 60            # 1 запрос раз в 15 мин ≈ 96/сутки
WINDOW_20      = range(19, 23)      # окно «~20'»
WINDOW_30      = range(28, 34)      # окно «~30'»
ODDS_MIN       = 1.29
ODDS_MAX       = 2.00
STAKE_BR       = 1               # условная ставка для отчёта

LOG_FILE       = "bot.log"
STATE_FILE     = "signals.json"

# ================ Логи =============================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("signal-bot")

# ================ Клиенты ==========================
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")
API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 20

# ============== Память за день =====================
signals = []          # список словарей (сохраняем матч, рынок и коэффициент)
signaled_ids = set()  # fixture_id с уже отосланным сигналом
current_day = date.today().isoformat()

# -------------- Утилиты времени --------------------
def now_local():
    return datetime.now(pytz.timezone(TIMEZONE))

def today_str():
    return now_local().date().isoformat()

# -------------- Telegram ---------------------------
def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# -------------- State (файл) -----------------------
def save_state():
    try:
        data = {
            "day": current_day,
            "signals": signals,
            "signaled_ids": list(signaled_ids),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.error(f"save_state error: {e}")

def load_state():
    global signals, signaled_ids, current_day
    if not os.path.exists(STATE_FILE):
        return
    try:
        data = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        file_day = data.get("day")
        # если файл за прошлый день — игнорим, начнём заново
        if file_day == today_str():
            signals = data.get("signals", [])
            signaled_ids = set(data.get("signaled_ids", []))
            current_day = file_day
    except Exception as e:
        log.error(f"load_state error: {e}")

def reset_daily_state():
    global signals, signaled_ids, current_day
    signals = []
    signaled_ids = set()
    current_day = today_str()
    save_state()

# -------------- API-Football -----------------------
def get_live_fixtures():
    """ 1 запрос — все лайвы """
    try:
        r = API.get("https://v3.football.api-sports.io/fixtures?live=all", timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("fixtures HTTP %s %s", r.status_code, r.text[:180])
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error(f"get_live_fixtures error: {e}")
        return []

def get_fixture_result(fid: int):
    """ Итог матча (для отчёта): FT/статус и финальные голы """
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

def get_over_total_odds(fid: int, line: int) -> float | None:
    """
    Получить кэф на Over(line) для конкретного матча.
    Используем /v3/odds?fixture=... и ищем Market 'Over/Under' со значением line (как '3' или '4'),
    берём исход 'Over'.
    Возвращает decimal-коэффициент или None.
    """
    try:
        r = API.get(f"https://v3.football.api-sports.io/odds?fixture={fid}", timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("odds HTTP %s %s", r.status_code, r.text[:180])
        r.raise_for_status()
        data = r.json().get("response", []) or []
        # Перебираем букмекерские конторы → маркеты → значения
        for book in data:
            for market in (book.get("bookmakers") or []):
                for mkt in (market.get("bets") or []):
                    # иногда Over/Under называется так же; у старого API — 'Goals Over/Under'
                    market_name = (mkt.get("name") or "").lower()
                    if "over" in market_name and "under" in market_name:
                        for val in (mkt.get("values") or []):
                            # ожидаем value == '3' или '4'; иногда '3.0'
                            v = (val.get("value") or "").strip()
                            if v in {str(line), f"{line}.0"}:
                                label = (val.get("odd") or "").strip()   # бывает odd здесь, а не price
                                # У разных интеграций структуру нужно проверить;
                                # если формат другой — лучше залогировать вал и продолжить:
                                try:
                                    odd = float(label)
                                except:
                                    continue
                                # нужно именно Over (в odds иногда 'Over 3' в другом поле)
                                # если values раздельные — тут уже именно та строка
                                # иначе можно проверить 'val["label"] == "Over"':
                                lab2 = (val.get("label") or "").lower()
                                if "over" in lab2:
                                    return odd
        return None
    except Exception as e:
        log.error(f"get_over_total_odds({fid}, {line}) error: {e}")
        return None

# -------------- Логика сигналов --------------------
def scan_and_signal():
    global current_day
    # если день сменился — сбросить накопленное (чтобы отчёт был за день)
    if current_day != today_str():
        reset_daily_state()

    live = get_live_fixtures()
    for m in live:
        try:
            f = m["fixture"]; t = m["teams"]; g = m["goals"]; L = m["league"]
            fid = f["id"]
            elapsed = int(f["status"]["elapsed"] or 0)
            gh, ga = g["home"] or 0, g["away"] or 0
            total_goals = gh + ga

            if fid in signaled_ids:
                continue

            target_line = None
            if total_goals == 2 and elapsed in WINDOW_20:
                target_line = 3
            elif total_goals == 3 and elapsed in WINDOW_30:
                target_line = 4

            if not target_line:
                continue

            # получить кэф Over(target_line)
            odds = get_over_total_odds(fid, target_line)
            if odds is None:
                log.info(f"[{fid}] odds not found for Over {target_line} at {elapsed}'  {t['home']['name']} - {t['away']['name']}")
                continue

            if ODDS_MIN <= odds <= ODDS_MAX:
                # формируем и отправляем сигнал
                rec = {
                    "fixture_id": fid,
                    "home": t["home"]["name"],
                    "away": t["away"]["name"],
                    "league": L["name"],
                    "country": L.get("country") or "",
                    "minute": elapsed,
                    "goals_home": gh,
                    "goals_away": ga,
                    "expected_goals": target_line,   # ТБ этого числа
                    "odds": round(float(odds), 2),
                }
                signals.append(rec)
                signaled_ids.add(fid)
                save_state()

                msg = (
                    f"*Ставка!*\n"
                    f"🏆 {rec['country']} — {rec['league']}\n"
                    f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                    f"⏱ {elapsed}'   •  ТБ {target_line}  •  кэф *{rec['odds']:.2f}*\n"
                    "─────────────"
                )
                send(msg)
                log.info("Signal sent: fid=%s  %s - %s  min=%s  O%s  @%.2f",
                         fid, rec['home'], rec['away'], elapsed, target_line, rec['odds'])

        except Exception as e:
            log.error(f"scan_and_signal item error: {e}")

# -------------- Красивый отчёт ----------------------
def send_daily_report():
    """
    В 23:30 по Минску — пройтись по сигналам, запросить итог каждого матча,
    посчитать прибыль/проходимость и отправить красивый отчёт.
    Считаем ставку фиксированной: 1000 Br
    Выигрыш = (odds - 1) * STAKE_BR, Проигрыш = -STAKE_BR
    """
    if not signals:
        send("📊 За сегодня ставок не было.")
        return

    done_win = 0
    done_lose = 0
    pendings = 0
    profit = 0.0
    used_odds = []

    lines = []

    for idx, rec in enumerate(signals, start=1):
        res = get_fixture_result(rec["fixture_id"])
        home, away = rec["home"], rec["away"]
        odds = float(rec.get("odds") or 0)
        exp  = int(rec.get("expected_goals") or 0)

        if not res:
            pendings += 1
            lines.append(f"#{idx} ❓ {home} — {away} | результат недоступен")
            continue

        st, gh, ga = res
        total = (gh or 0) + (ga or 0)

        if st == "FT":
            if total > exp:           # тотал пробит
                done_win += 1
                pnl = (odds - 1.0) * STAKE_BR
                profit += pnl
                used_odds.append(odds)
                lines.append(f"#{idx} ✅ +{int(pnl):,} Br  ({odds:.2f})  {home} {gh}-{ga} {away}".replace(",", " "))
            else:
                done_lose += 1
                pnl = -STAKE_BR
                profit += pnl
                used_odds.append(odds)
                lines.append(f"#{idx} ❌ {int(pnl):,} Br  ({odds:.2f})  {home} {gh}-{ga} {away}".replace(",", " "))
        else:
            pendings += 1
            lines.append(f"#{idx} ⏳ {home} — {away} | статус: {st}")

    total_bets = done_win + done_lose
    pass_rate = (done_win / total_bets * 100.0) if total_bets else 0.0
    avg_odds  = (sum(used_odds)/len(used_odds)) if used_odds else 0.0

    header = (
        f"{done_win} ✅ / {done_lose} ❌ / {pendings} ⏳\n"
        f"🧮 Проходимость: {pass_rate:.0f}%\n"
        f"💰 Прибыль: {int(profit):,} Br\n"
        f"📊 Средний кэф: {avg_odds:.2f}\n"
        "─────────────"
    ).replace(",", " ")

    send(header + "\n" + "\n".join(lines))

# =================== RUN ===========================
if __name__ == "__main__":
    # HTTP-«держатель» для Render Free Web Service
    Thread(target=run_http, daemon=True).start()

    load_state()
    send("🚀 Бот запущен — новая версия!")
    send("✅ Режим: сигналы при 2/3 голах и кэфе 1.29–2.00 (ТБ 3 / ТБ 4).")

    while True:
        try:
            scan_and_signal()

            # Ежедневный отчёт в 23:30 по Минску
            now = now_local()
            if now.hour == 23 and now.minute == 30:
                send_daily_report()
                # очистка на новый день — после отчёта
                reset_daily_state()
                time.sleep(60)  # чтобы не дублировать в ту же минуту

            time.sleep(POLL_SECONDS)
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(POLL_SECONDS)
