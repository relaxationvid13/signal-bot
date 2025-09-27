# -*- coding: utf-8 -*-
"""
Режим опроса:
- Активное окно: 16:00–23:29 (Europe/Warsaw), опрос каждые 5 минут
- В 23:30 — дневной отчёт, + недельный (вс) и месячный (последний день)

Стратегии (обе включены):
1) OVER-20: до 20' забито 2 или 3 → ТБ 3 / ТБ 4 (по умолчанию без кэфов)
2) UNDER-20: до 20' счёт 0–0 → ТМ 3.0 с кэфом >= 1.60 (если odds доступны)

Для Render — поднят HTTP healthcheck на '/'.
"""

import os, sys, time, json, logging
from datetime import datetime, timedelta, timezone

import pytz
import requests
import telebot

# ---------- Flask healthcheck ----------
from threading import Thread
from flask import Flask
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
# --------------------------------------


# ===== Секреты / окружение =====
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")
if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")
CHAT_ID = int(CHAT_ID)

# ===== Параметры =====
TIMEZONE        = "Europe/Warsaw"
MAX_MINUTE      = 20

# Активное окно и частота опроса
ACTIVE_START_H  = 16     # с 16:00
ACTIVE_END_H    = 23     # до 23:29 (отчёты в 23:30)
POLL_SEC        = 5 * 60 # каждые 5 минут

# Ставка-единица для отчётов (+1/0/-1)
STAKE_UNITS     = 1

# --- Стратегия 1: OVER-20 ---
STRAT_OVER_20   = True
ODDS_ENABLED_O  = False       # если True — фильтруем по кэфам
ODDS_MIN_O      = 1.29
ODDS_MAX_O      = 2.00
LINE_O3         = 3.0         # при 2 голах
LINE_O4         = 4.0         # при 3 голах

# --- Стратегия 2: UNDER-20 ---
STRAT_UNDER_20  = True
ODDS_ENABLED_U  = True        # кэф обязателен
ODDS_MIN_U3     = 1.60
LINE_U3         = 3.0

# odds доступны только на платных планах API-Football
ODDS_BOOKMAKER_NAME = None    # можно ограничить, например "Pinnacle"

LOG_FILE        = "bot.log"
STATE_FILE      = "signals.json"

# ===== Логи =====
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("signals-bot")

# ===== Telegram =====
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ===== API-Football =====
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

# ===== Работа с кэфами (Over/Under) =====
def _extract_ou_price(odds_response, target_kind: str, target_line: float):
    """
    target_kind: 'over' | 'under'
    target_line: 3.0 / 4.0
    Возвращает лучший (максимальный) найденный кэф.
    """
    best = None
    try:
        for item in odds_response or []:
            # возможные ключи: bookmaker{name}, bets[{name, values[{value, odd}]}]
            bookmakers = item.get("bookmakers") or [item]  # на разных аккаунтах структура отличается
            for bk in bookmakers:
                bname = ""
                if isinstance(bk.get("bookmaker"), dict):
                    bname = (bk["bookmaker"].get("name") or "")
                elif "bookmaker" in bk:
                    bname = str(bk.get("bookmaker") or "")
                if ODDS_BOOKMAKER_NAME and ODDS_BOOKMAKER_NAME.lower() not in bname.lower():
                    continue

                for bet in bk.get("bets", []) or []:
                    name = (bet.get("name") or "").lower()
                    if "over" in name and "under" in name:
                        for v in bet.get("values", []) or []:
                            raw = (v.get("value") or "").strip().lower().replace(" ", "")
                            # допускаем варианты типа "over3", "over3.0", "under4", ...
                            if raw in (f"{target_kind}{int(target_line)}", f"{target_kind}{target_line:g}"):
                                try:
                                    price = float(v.get("odd"))
                                    if best is None or price > best:
                                        best = price
                                except:
                                    pass
        return best
    except Exception as e:
        log.error(f"_extract_ou_price error: {e}")
        return None

def get_over_odds(fid: int, line: float):
    """Кэф на ТБ line (3/4)."""
    try:
        # пробуем odds-live
        r = API.get(f"https://v3.football.api-sports.io/odds-live?fixture={fid}", timeout=DEFAULT_TIMEOUT)
        if r.ok:
            resp = r.json().get("response", []) or []
            p = _extract_ou_price(resp, "over", line)
            if p is not None:
                return p
        # fallback: статичный odds
        r = API.get(f"https://v3.football.api-sports.io/odds?fixture={fid}", timeout=DEFAULT_TIMEOUT)
        if not r.ok:
            return None
        resp = r.json().get("response", []) or []
        return _extract_ou_price(resp, "over", line)
    except Exception as e:
        log.warning(f"get_over_odds({fid},{line}) warn: {e}")
        return None

def get_under3_odds(fid: int):
    """Кэф на ТМ 3.0."""
    try:
        r = API.get(f"https://v3.football.api-sports.io/odds-live?fixture={fid}", timeout=DEFAULT_TIMEOUT)
        if r.ok:
            resp = r.json().get("response", []) or []
            p = _extract_ou_price(resp, "under", 3.0)
            if p is not None:
                return p
        r = API.get(f"https://v3.football.api-sports.io/odds?fixture={fid}", timeout=DEFAULT_TIMEOUT)
        if not r.ok:
            return None
        resp = r.json().get("response", []) or []
        return _extract_ou_price(resp, "under", 3.0)
    except Exception as e:
        log.warning(f"get_under3_odds({fid}) warn: {e}")
        return None

# ===== Состояние =====
signals = []          # [{fixture_id, ts_utc, home, away, league, country, minute, goals_home, goals_away, market, line, odds}]
signaled_ids = set()  # ключи: "<fid>-OVER" / "<fid>-UNDER"

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

# ===== Время =====
def tz():
    return pytz.timezone(TIMEZONE)

def now_local():
    return datetime.now(tz())

def in_active_window(n: datetime) -> bool:
    """true, если 16:00 ≤ time ≤ 23:29 (по Europe/Warsaw)."""
    hh, mm = n.hour, n.minute
    if hh < ACTIVE_START_H:
        return False
    if hh > ACTIVE_END_H:
        return False
    if hh < ACTIVE_END_H:
        return True
    # hh == 23 → до 23:29
    return mm <= 29

# ===== Скан обеих стратегий =====
def scan_and_signal():
    live = get_live()
    for m in live:
        try:
            f = m["fixture"]; t = m["teams"]; g = m["goals"]; L = m["league"]
            fid = f["id"]
            elapsed = int(f["status"]["elapsed"] or 0)
            if elapsed > MAX_MINUTE:
                continue

            gh, ga = g["home"] or 0, g["away"] or 0
            total = gh + ga

            # ---------- OVER-20 (2/3 гола) ----------
            if STRAT_OVER_20 and total in (2, 3):
                key_over = f"{fid}-OVER"
                if key_over not in signaled_ids:
                    line = LINE_O3 if total == 2 else LINE_O4
                    if ODDS_ENABLED_O:
                        odds = get_over_odds(fid, line)
                        if odds is None:
                            log.info("OVER: fixture=%s line=%.0f нет кэфа → пропуск", fid, line)
                        elif not (ODDS_MIN_O <= odds <= ODDS_MAX_O):
                            log.info("OVER: fixture=%s odds %.2f вне [%s,%s] → пропуск", fid, odds, ODDS_MIN_O, ODDS_MAX_O)
                        else:
                            rec = {
                                "fixture_id": fid, "ts_utc": datetime.utcnow().isoformat(),
                                "home": t["home"]["name"], "away": t["away"]["name"],
                                "league": L["name"], "country": L["country"],
                                "minute": elapsed, "goals_home": gh, "goals_away": ga,
                                "market": "over", "line": line, "odds": round(float(odds), 2),
                            }
                            signals.append(rec); signaled_ids.add(key_over); save_state()
                            send(
                                "⚪ *Сигнал (OVER)*\n"
                                f"🏆 {rec['country']} — {rec['league']}\n"
                                f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                                f"⏱ {elapsed}'  |  *ТБ {line:.0f}*  |  кэф: *{rec['odds']:.2f}*\n"
                                "─────────────"
                            )
                    else:
                        rec = {
                            "fixture_id": fid, "ts_utc": datetime.utcnow().isoformat(),
                            "home": t["home"]["name"], "away": t["away"]["name"],
                            "league": L["name"], "country": L["country"],
                            "minute": elapsed, "goals_home": gh, "goals_away": ga,
                            "market": "over", "line": line, "odds": "n/a",
                        }
                        signals.append(rec); signaled_ids.add(key_over); save_state()
                        send(
                            "⚪ *Сигнал (OVER)*\n"
                            f"🏆 {rec['country']} — {rec['league']}\n"
                            f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                            f"⏱ {elapsed}'  |  *ТБ {line:.0f}*\n"
                            "─────────────"
                        )

            # ---------- UNDER-20 (0–0 и U3 >= 1.60) ----------
            if STRAT_UNDER_20 and total == 0:
                key_under = f"{fid}-UNDER"
                if key_under not in signaled_ids:
                    if ODDS_ENABLED_U:
                        u3 = get_under3_odds(fid)
                        if u3 is None:
                            log.info("UNDER: fixture=%s нет кэфа U3 → пропуск", fid)
                        elif u3 < ODDS_MIN_U3:
                            log.info("UNDER: fixture=%s кэф %.2f < %.2f → пропуск", fid, u3, ODDS_MIN_U3)
                        else:
                            rec = {
                                "fixture_id": fid, "ts_utc": datetime.utcnow().isoformat(),
                                "home": t["home"]["name"], "away": t["away"]["name"],
                                "league": L["name"], "country": L["country"],
                                "minute": elapsed, "goals_home": gh, "goals_away": ga,
                                "market": "under", "line": LINE_U3, "odds": round(float(u3), 2),
                            }
                            signals.append(rec); signaled_ids.add(key_under); save_state()
                            send(
                                "⚪ *Сигнал (UNDER)*\n"
                                f"🏆 {rec['country']} — {rec['league']}\n"
                                f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                                f"⏱ {elapsed}'  |  *ТМ {LINE_U3:.0f}*  |  кэф: *{rec['odds']:.2f}*\n"
                                "─────────────"
                            )
                    else:
                        rec = {
                            "fixture_id": fid, "ts_utc": datetime.utcnow().isoformat(),
                            "home": t["home"]["name"], "away": t["away"]["name"],
                            "league": L["name"], "country": L["country"],
                            "minute": elapsed, "goals_home": gh, "goals_away": ga,
                            "market": "under", "line": LINE_U3, "odds": "n/a",
                        }
                        signals.append(rec); signaled_ids.add(key_under); save_state()
                        send(
                            "⚪ *Сигнал (UNDER)*\n"
                            f"🏆 {rec['country']} — {rec['league']}\n"
                            f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                            f"⏱ {elapsed}'  |  *ТМ {LINE_U3:.0f}*\n"
                            "─────────────"
                        )

        except Exception as e:
            log.error(f"scan_and_signal item error: {e}")

# ===== Отчёты (ТМ3 — с учётом пуша) =====
def summarize_period(items, title):
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
        market = rec["market"]
        line = int(rec["line"])
        odds = rec.get("odds", "n/a")

        if market == "under" and line == 3:
            if tot < 3:
                wins += 1; pnl = +STAKE_UNITS; tag = "ТМ 3.0"
                lines_out.append(f"#{i:02d} ✅ +{pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | {tag} @ {odds}")
            elif tot == 3:
                pushes += 1; pnl = 0.0; tag = "ТМ 3.0"
                lines_out.append(f"#{i:02d} ♻ {pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | {tag} @ {odds}")
            else:
                losses += 1; pnl = -STAKE_UNITS; tag = "ТМ 3.0"
                lines_out.append(f"#{i:02d} ❌ {pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | {tag} @ {odds}")
        else:
            ok = tot > line if market == "over" else tot < line
            tag = f"ТБ {line}" if market == "over" else f"ТМ {line}"
            if ok:
                wins += 1; pnl = +STAKE_UNITS
                lines_out.append(f"#{i:02d} ✅ +{pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | {tag} @ {odds}")
            else:
                losses += 1; pnl = -STAKE_UNITS
                lines_out.append(f"#{i:02d} ❌ {pnl:.2f} | {rec['home']} {gh}-{ga} {rec['away']} | {tag} @ {odds}")

    profit = wins*STAKE_UNITS - losses*STAKE_UNITS
    solved = wins + losses  # пуши не считаем в знаменатель
    pass_rate = int(round(wins * 100.0 / max(1, solved)))

    head = [
        title,
        f"{wins} ✅ / {losses} ❌ / {pushes} ♻",
        f"📈 Проходимость: {pass_rate}%",
        f"💰 Прибыль (ед.): {profit:.2f}",
        "─────────────",
    ]
    return "\n".join(head + lines_out)

def daily_report():
    tzloc = tz()
    today = now_local().date()
    day_items = []
    for r in signals:
        ts = datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc).astimezone(tzloc)
        if ts.date() == today:
            day_items.append(r)
    send(summarize_period(day_items, "📅 *Дневной отчёт*"))

def weekly_report():
    tzloc = tz()
    today = now_local().date()
    start_of_week = today - timedelta(days=today.weekday())  # понедельник
    week_items = []
    for r in signals:
        ts = datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc).astimezone(tzloc)
        if start_of_week <= ts.date() <= today:
            week_items.append(r)
    send(summarize_period(week_items, "🗓 *Недельный отчёт*"))

def monthly_report():
    tzloc = tz()
    today = now_local().date()
    start_of_month = today.replace(day=1)
    month_items = []
    for r in signals:
        ts = datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc).astimezone(tzloc)
        if start_of_month <= ts.date() <= today:
            month_items.append(r)
    send(summarize_period(month_items, "📆 *Месячный отчёт*"))

def is_last_day_of_month(d):
    return (d + timedelta(days=1)).day == 1

# ===== RUN =====
if __name__ == "__main__":
    Thread(target=run_http, daemon=True).start()
    load_state()

    send("🚀 Бот запущен — новая версия!")
    send("✅ Активное окно: 16:00–23:29 (PL), опрос каждые 5 минут. Отчёты — в 23:30.\n"
         "Стратегии: OVER-20 (2/3 гола → ТБ3/4), UNDER-20 (0–0 → ТМ3, кэф ≥ 1.60).")

    while True:
        try:
            now = now_local()

            # отчёты в 23:30
            if now.hour == 23 and now.minute == 30:
                daily_report()
                if now.weekday() == 6:           # воскресенье
                    weekly_report()
                if is_last_day_of_month(now.date()):
                    monthly_report()
                time.sleep(60)  # антидубль минута

            # активное окно опроса: 16:00..23:29
            if in_active_window(now):
                scan_and_signal()
            else:
                log.info("Вне активного окна (%s), сплю...", now.strftime("%H:%M"))

            time.sleep(POLL_SEC)

        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(POLL_SEC)
