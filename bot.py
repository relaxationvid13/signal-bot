# -*- coding: utf-8 -*-
"""
Стратегия: до 20' счёт 0–0 и кэф на ТМ 3.0 >= 1.60 → отправляем сигнал.

Отчёты:
- Дневной: 23:30 (Europe/Warsaw)
- Недельный: по воскресеньям в 23:30
- Месячный: в последний день месяца в 23:30

Опрос API строго по «кварталам» часа (00/15/30/45), чтобы не drift'ить во времени.
Для Render поднят HTTP healthcheck на '/' чтобы держать инстанс бодрым.

✅ Обновление: учтён ПУШ для ТМ 3.0 — если итоговый тотал ровно 3, считаем возврат (♻ 0.00).
"""

import os, sys, time, json, logging
from datetime import datetime, timedelta, timezone

import pytz
import requests
import telebot

# ---------- Flask healthcheck (Render friendly) ----------
from threading import Thread
from flask import Flask
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
# ---------------------------------------------------------


# ============== Секреты / окружение ======================
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")
if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")
CHAT_ID = int(CHAT_ID)

# ============== Параметры стратегии ======================
TIMEZONE       = "Europe/Warsaw"   # отчёты по польскому времени
MAX_MINUTE     = 20                # учитываем только до/включая 20-ю минуту
POLL_ALIGN     = True              # опрашиваем по 00/15/30/45 (иначе 15 минут от сна)
STAKE_UNITS    = 1                 # условная ставка в отчётах (+1/-1)
LINE_U3        = 3                 # линия ТМ 3.0
ODDS_MIN_U3    = 1.60              # кэф на ТМ 3.0 должен быть >= 1.60

# odds (для получения кэфов нужен платный доступ в API-Football)
ODDS_ENABLED         = True        # если нет доступа — просто не будет сигналов
ODDS_BOOKMAKER_NAME  = None        # можно ограничить конкретным букмекером (строкой), иначе любой

LOG_FILE       = "bot.log"
STATE_FILE     = "signals.json"

# ============== Логи =====================================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("u3-00-bot")

# ============== Telegram =================================
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ============== API-Football ==============================
API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 15

def get_live():
    try:
        r = API.get("https://v3.football.api-sports.io/fixtures?live=all", timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error(f"get_live error: {e}")
        return []

def get_fixture_result(fid: int):
    try:
        r = API.get(f"https://v3.football.api-sports.io/fixtures?id={fid}", timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        resp = r.json().get("response", []) or []
        if not resp:
            return None
        m = resp[0]
        st = (m["fixture"]["status"]["short"] or "").upper()
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return st, gh, ga
    except Exception as e:
        log.error(f"get_fixture_result({fid}) error: {e}")
        return None

def get_under3_odds(fid: int):
    """
    Пытаемся достать кэф на ТМ 3.0.
    Требует odds-доступ в API-Football. Если недоступно или нет рынка — вернёт None.
    """
    if not ODDS_ENABLED:
        return None
    try:
        r = API.get(f"https://v3.football.api-sports.io/odds?fixture={fid}", timeout=DEFAULT_TIMEOUT)
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        resp = r.json().get("response", []) or []

        best = None
        for bk in resp:
            b_name = ""
            if isinstance(bk.get("bookmaker"), dict):
                b_name = (bk["bookmaker"].get("name") or "")
            elif "bookmaker" in bk:
                b_name = str(bk.get("bookmaker") or "")
            if ODDS_BOOKMAKER_NAME and ODDS_BOOKMAKER_NAME.lower() not in b_name.lower():
                continue

            for bet in bk.get("bets", []) or []:
                name = (bet.get("name") or "").lower()
                if "over" in name and "under" in name:  # рынок Over/Under
                    for v in bet.get("values", []) or []:
                        val = (v.get("value") or "").replace(" ", "").lower()
                        if val in ("under3", "under3.0"):
                            try:
                                price = float(v.get("odd"))
                                if best is None or price > best:
                                    best = price
                            except Exception:
                                pass
        return best
    except Exception as e:
        log.warning(f"get_under3_odds({fid}) warn: {e}")
        return None

# ============== Состояние (переживает перезапуск) =========
signals = []          # [{fixture_id, ts_utc, home, away, league, country, minute, goals_home, goals_away, market, line, odds}]
signaled_ids = set()  # чтобы не дублировать один и тот же матч

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"signals": signals, "signaled_ids": list(signaled_ids)}, f, ensure_ascii=False)
    except Exception as e:
        log.error(f"save_state error: {e}")

def load_state():
    global signals, signaled_ids
    if not os.path.exists(STATE_FILE):
        return
    try:
        data = json.load(open(STATE_FILE, "r", encoding="utf-8"))
        signals = data.get("signals", [])
        signaled_ids = set(data.get("signaled_ids", []))
    except Exception as e:
        log.error(f"load_state error: {e}")

# ============== Время / расписание ========================
def tz():
    return pytz.timezone(TIMEZONE)

def now_local():
    return datetime.now(tz())

def sleep_to_next_quarter():
    """Спим до ближайших 00/15/30/45 минут."""
    n = now_local()
    q = (n.minute // 15 + 1) * 15
    if q >= 60:
        target = n.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        target = n.replace(minute=q, second=0, microsecond=0)
    sec = max(1, int((target - n).total_seconds()))
    log.info(f"Сплю до {target.strftime('%H:%M')} (~{sec} сек)")
    time.sleep(sec)

# ============== Сканер: U3 (0-0 и odds >= 1.60) ===========
def scan_u3_with_odds():
    """
    Сигнал только если:
      - elapsed <= 20
      - счёт 0-0
      - odds(ТМ 3.0) >= 1.60
    """
    live = get_live()
    for m in live:
        try:
            f = m["fixture"]; t = m["teams"]; g = m["goals"]; L = m["league"]
            fid = f["id"]
            elapsed = int(f["status"]["elapsed"] or 0)
            if elapsed > MAX_MINUTE:
                continue
            if fid in signaled_ids:
                continue

            gh, ga = g["home"] or 0, g["away"] or 0
            if (gh + ga) != 0:
                continue  # нужно 0-0

            odds_u3 = get_under3_odds(fid)
            if odds_u3 is None:
                log.info(f"fixture={fid} нет кэфа U3 → пропуск")
                continue
            if odds_u3 < ODDS_MIN_U3:
                log.info(f"fixture={fid} кэф {odds_u3:.2f} < {ODDS_MIN_U3} → пропуск")
                continue

            rec = {
                "fixture_id": fid,
                "ts_utc": datetime.utcnow().isoformat(),
                "home": t["home"]["name"], "away": t["away"]["name"],
                "league": L["name"], "country": L["country"],
                "minute": elapsed, "goals_home": gh, "goals_away": ga,
                "market": "under", "line": LINE_U3, "odds": round(float(odds_u3), 2),
            }
            signals.append(rec)
            signaled_ids.add(fid)
            save_state()

            send(
                "⚪ *Сигнал (U3 новая стратегия)*\n"
                f"🏆 {rec['country']} — {rec['league']}\n"
                f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                f"⏱ {elapsed}'  |  *ТМ {LINE_U3}*  |  кэф: *{rec['odds']:.2f}*\n"
                "─────────────"
            )
            log.info("Signal U3 sent: fid=%s  %s-%s  min=%d  U3@%.2f",
                     fid, rec['home'], rec['away'], elapsed, rec['odds'])

        except Exception as e:
            log.error(f"scan_u3_with_odds item error: {e}")

# ============== Отчёты (с учётом ПУША на 3.0) =============
def summarize_period(items, title):
    """
    +1/0/−1 ед. прибыли:
      - ТМ 3.0: win при тотале < 3, push при тотале == 3, loss при тотале > 3.
      - (если позже добавишь другие рынки — аналогично можно учесть их правила)
    """
    total = len(items)
    if total == 0:
        return f"{title}\nСегодня ставок не было."

    wins = losses = pushes = 0
    lines_out = []

    for i, rec in enumerate(items, 1):
        res = get_fixture_result(rec["fixture_id"])
        if not res:
            lines_out.append(f"#{i:02d} ❓ {rec['home']} — {rec['away']} | результат недоступен")
            continue

        st, gh, ga = res
        tot = (gh or 0) + (ga or 0)
        market = rec.get("market")
        line = int(rec.get("line", 3))
        odds = rec.get("odds", "n/a")

        if market == "under" and line == 3:
            if tot < 3:
                wins += 1
                pnl = +STAKE_UNITS
                lines_out.append(f"#{i:02d} ✅ +{pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | ТМ 3.0 @ {odds}")
            elif tot == 3:
                pushes += 1
                pnl = 0.0
                lines_out.append(f"#{i:02d} ♻ {pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | ТМ 3.0 @ {odds}")
            else:  # tot > 3
                losses += 1
                pnl = -STAKE_UNITS
                lines_out.append(f"#{i:02d} ❌ {pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | ТМ 3.0 @ {odds}")
        else:
            # на будущее, если появятся другие рынки
            ok = tot < line if market == "under" else tot > line
            if ok:
                wins += 1
                pnl = +STAKE_UNITS
                tag = f"ТМ {line}" if market == "under" else f"ТБ {line}"
                lines_out.append(f"#{i:02d} ✅ +{pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | {tag} @ {odds}")
            else:
                losses += 1
                pnl = -STAKE_UNITS
                tag = f"ТМ {line}" if market == "under" else f"ТБ {line}"
                lines_out.append(f"#{i:02d} ❌ {pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | {tag} @ {odds}")

    profit = wins*STAKE_UNITS - losses*STAKE_UNITS  # пуши по 0
    played = wins + losses + pushes
    pass_rate = int(round((wins / max(1, wins + losses)) * 100.0))  # успехи из решённых (без пушей)

    head = [
        title,
        f"{wins} ✅ / {losses} ❌ / {pushes} ♻",
        f"📈 Проходимость: {pass_rate}%",
        f"💰 Прибыль (ед.): {profit:.2f}",
        "─────────────",
    ]
    return "\n".join(head + lines_out)

def daily_report():
    tz = pytz.timezone(TIMEZONE)
    today = now_local().date()
    day_items = []
    for r in signals:
        ts = datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc).astimezone(tz)
        if ts.date() == today:
            day_items.append(r)
    send(summarize_period(day_items, "📅 *Дневной отчёт*"))

def weekly_report():
    tz = pytz.timezone(TIMEZONE)
    today = now_local().date()
    start_of_week = today - timedelta(days=today.weekday())  # понедельник
    week_items = []
    for r in signals:
        ts = datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc).astimezone(tz)
        if start_of_week <= ts.date() <= today:
            week_items.append(r)
    send(summarize_period(week_items, "🗓 *Недельный отчёт*"))

def monthly_report():
    tz = pytz.timezone(TIMEZONE)
    today = now_local().date()
    start_of_month = today.replace(day=1)
    month_items = []
    for r in signals:
        ts = datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc).astimezone(tz)
        if start_of_month <= ts.date() <= today:
            month_items.append(r)
    send(summarize_period(month_items, "📆 *Месячный отчёт*"))

def is_last_day_of_month(d):
    return (d + timedelta(days=1)).day == 1

# ============== RUN ======================================
if __name__ == "__main__":
    Thread(target=run_http, daemon=True).start()
    load_state()

    send("🚀 Бот запущен — новая версия!")
    send(f"✅ Стратегия: до 20' счёт 0–0 → *ТМ {LINE_U3}*, кэф ≥ *{ODDS_MIN_U3:.2f}* (по odds API).\n"
         f"Пуш на ровной 3.0 учитывается как ♻ 0.00 в отчётах.")

    while True:
        try:
            # скан
            scan_u3_with_odds()

            # отчёты в 23:30 по Варшаве
            now = now_local()
            if now.hour == 23 and now.minute == 30:
                daily_report()
                if now.weekday() == 6:           # воскресенье
                    weekly_report()
                if is_last_day_of_month(now.date()):
                    monthly_report()
                time.sleep(60)  # антидубль

            # расписание опроса
            if POLL_ALIGN:
                sleep_to_next_quarter()
            else:
                time.sleep(15 * 60)

        except Exception as e:
            log.error(f"Main loop error: {e}")
            if POLL_ALIGN:
                sleep_to_next_quarter()
            else:
                time.sleep(15 * 60)
