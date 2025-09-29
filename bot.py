# -*- coding: utf-8 -*-
"""
Pre-match bot (API-Football) + расписание:
- Скан в 08:00 (Europe/Warsaw) -> сигналы на сегодня+завтра:
    1) 3/3 очных встреч >= 3 гола
    2) 3/3 последних матчей каждой команды >= 3 гола
    3) (опция) коэффициент на ТБ 2.5 >= MIN_ODDS
- Ежедневный отчёт: 23:30
- Недельный отчёт: воскресенье 23:50
- Месячный отчёт: последний день месяца 23:50

ENV:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  API_FOOTBALL_KEY
  TZ (например Europe/Warsaw; по умолчанию Europe/Warsaw)
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timedelta, date, time as dtime

import pytz
import requests
import telebot

# ==================== Настройки ====================

TIMEZONE       = os.getenv("TZ", "Europe/Warsaw")  # локальная TZ
SCAN_TIME      = dtime(8, 0)     # 08:00 — один дневной прогон
DAILY_TIME     = dtime(23, 30)   # 23:30 — дневной отчёт
WEEKLY_TIME    = dtime(23, 50)   # 23:50 — недельный (вс)
MONTHLY_TIME   = dtime(23, 50)   # 23:50 — месячный (последний день)

SLEEP_SEC      = 600             # шаг цикла 10 мин (можно увеличить/уменьшить)
LOOK_DAYS      = 2               # сканируем сегодня+завтра

# фильтр по кэфу на ТБ 2.5 (если odds доступны на вашем плане)
MIN_ODDS_CHECK = True
MIN_ODDS       = 1.70

STAKE_UNIT   = 1.0               # для профита в отчётах
LOG_FILE     = "bot.log"
STATE_FILE   = "signals.json"

# ==================== API и токены ====================

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")
if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

CHAT_ID = int(CHAT_ID)

# ==================== Логи ====================

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("prematch-bot")

# ==================== Telegram ====================

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error("Telegram send error: %s", e)

# ==================== API-Football ====================

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 20

def api_get(path, params=None):
    url = f"https://v3.football.api-sports.io/{path}"
    try:
        r = API.get(url, params=params or {}, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("HTTP %s for %s %s", r.status_code, path, r.text[:200])
        r.raise_for_status()
        data = r.json()
        return data.get("response", []) or []
    except Exception as e:
        log.error("api_get %s error: %s", path, e)
        return []

def get_fixtures_by_date(date_str):
    """Возвращает не начатые (NS) матчи на дату."""
    resp = api_get("fixtures", {"date": date_str})
    return [m for m in resp if m["fixture"]["status"]["short"] == "NS"]

def total_goals(m) -> int:
    try:
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return int(gh) + int(ga)
    except Exception:
        return 0

def get_h2h_last3_over3(home_id: int, away_id: int) -> bool:
    resp = api_get("fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 3})
    return len(resp) >= 3 and all(total_goals(x) >= 3 for x in resp)

def get_team_last3_over3(team_id: int) -> bool:
    resp = api_get("fixtures", {"team": team_id, "last": 3})
    return len(resp) >= 3 and all(total_goals(x) >= 3 for x in resp)

def get_over25_odds(fixture_id: int):
    """Пробуем вытащить кэф на Over 2.5 (если доступен)."""
    resp = api_get("odds", {"fixture": fixture_id})
    try:
        for market in resp:
            for bm in market.get("bookmakers", []):
                for b in bm.get("bets", []):
                    name = (b.get("name") or "").lower()
                    if "over/under" in name:
                        for v in b.get("values", []):
                            val = (v.get("value") or "").strip().lower()
                            if "over 2.5" in val:
                                odd_str = v.get("odd")
                                if odd_str:
                                    return float(odd_str.replace(",", "."))
        return None
    except Exception as e:
        log.error("get_over25_odds error for fixture %s: %s", fixture_id, e)
        return None

def get_fixture_by_id(fid: int):
    resp = api_get("fixtures", {"id": fid})
    return resp[0] if resp else None

# ==================== Время/утилиты ====================

def tz(): return pytz.timezone(TIMEZONE)
def now_local(): return datetime.now(tz())

def fmt_dt_utc_to_local(utc_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        local = dt.astimezone(tz())
        return local.strftime("%d.%m %H:%M")
    except Exception:
        return utc_iso

def is_last_day_of_month(d: date) -> bool:
    return (d + timedelta(days=1)).day == 1

# ==================== Состояние ====================

state = {
    "signaled_fixtures": [],  # fixture_id уже сигнален
    "signals": [],            # история сигналов
    "last_scan_date": "",     # YYYY-MM-DD — когда сделали дневной скан
    "last_daily": "",         # YYYY-MM-DD — последний дневной отчёт
    "last_weekly": "",        # YYYY-WW — последняя ISO-неделя отчёта
    "last_monthly": ""        # YYYY-MM — последний месячный отчёт
}

def load_state():
    global state
    if not os.path.exists(STATE_FILE): return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in state.keys():
                if k in data: state[k] = data[k]
    except Exception as e:
        log.error("load_state error: %s", e)

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        log.error("save_state error: %s", e)

# ==================== Стратегия и сигналы ====================

def should_signal_fixture(m) -> tuple[bool, dict]:
    f = m["fixture"]; t = m["teams"]; L = m["league"]
    fid      = f["id"]
    home     = t["home"]["name"]
    away     = t["away"]["name"]
    home_id  = t["home"]["id"]
    away_id  = t["away"]["id"]
    league   = L["name"]
    country  = L["country"]
    kickoff  = fmt_dt_utc_to_local(f["date"])

    if not get_h2h_last3_over3(home_id, away_id): return False, {}
    if not get_team_last3_over3(home_id): return False, {}
    if not get_team_last3_over3(away_id): return False, {}

    odds = get_over25_odds(fid)
    if MIN_ODDS_CHECK and (odds is not None) and odds < MIN_ODDS:
        return False, {}

    info = {
        "fixture_id": fid, "home": home, "away": away,
        "league": league, "country": country,
        "kickoff": kickoff, "odds_over25": odds
    }
    return True, info

def build_signal_message(info: dict) -> str:
    odds_txt = f"{info['odds_over25']:.2f}" if info["odds_over25"] is not None else "н/д"
    return (
        "📢 *Пре-матч сигнал*\n"
        f"🏆 {info['country']} — {info['league']}\n"
        f"⚽ {info['home']} — {info['away']}\n"
        f"🕒 Начало: {info['kickoff']} ({TIMEZONE})\n\n"
        "Условия:\n"
        "• 3/3 очных встреч: тотал ≥ 3\n"
        "• 3/3 последних матчей каждой команды: тотал ≥ 3\n\n"
        f"🎯 Рекомендация: *ТБ 2.5*  @ *{odds_txt}*\n"
        f"ID: `{info['fixture_id']}`"
    )

def scan_upcoming_and_signal():
    # сегодня + завтра
    dates = []
    now = now_local()
    for d in range(LOOK_DAYS):
        dates.append((now + timedelta(days=d)).strftime("%Y-%m-%d"))

    fixtures = []
    for ds in dates:
        fixtures.extend(get_fixtures_by_date(ds))

    new_cnt = 0
    for m in fixtures:
        fid = m["fixture"]["id"]
        if fid in state["signaled_fixtures"]:
            continue
        ok, info = should_signal_fixture(m)
        if not ok: continue

        send(build_signal_message(info))
        state["signaled_fixtures"].append(fid)

        # сохраняем запись для отчётов/результатов
        state["signals"].append({
            "fixture_id": info["fixture_id"],
            "date_signal": now.strftime("%Y-%m-%d"),  # локальный день отправки
            "home": info["home"], "away": info["away"],
            "league": info["league"], "country": info["country"],
            "kickoff": info["kickoff"],
            "odds": info["odds_over25"],
            "bet": "OVER25",
            "result": None,          # WIN/LOSE/None
            "final_total": None,
            "closed": False
        })
        new_cnt += 1

    if new_cnt:
        save_state()
        log.info("New signals: %s", new_cnt)

# ==================== Послесчёт результатов ====================

def settle_open_signals():
    changed = False
    for s in state["signals"]:
        if s.get("closed"): continue
        m = get_fixture_by_id(s["fixture_id"])
        if not m: continue
        status = (m["fixture"]["status"]["short"] or "")
        if status in ("FT", "AET", "PEN"):
            tot = total_goals(m)
            s["final_total"] = tot
            s["result"] = "WIN" if tot >= 3 else "LOSE"
            s["closed"] = True
            changed = True
    if changed:
        save_state()
        log.info("Settled signals")

# ==================== Отчёты ====================

def calc_profit(odds, result) -> float:
    if result == "WIN":
        return (float(odds) - 1.0) if odds is not None else 1.0
    if result == "LOSE":
        return -1.0
    return 0.0

def build_report(signals):
    n = len(signals)
    wins = sum(1 for s in signals if s.get("result") == "WIN")
    loses = sum(1 for s in signals if s.get("result") == "LOSE")
    pend = sum(1 for s in signals if s.get("result") is None)
    winrate = (wins / max(1, wins + loses)) * 100.0
    profit = sum(calc_profit(s.get("odds"), s.get("result")) for s in signals if s.get("result") is not None)
    lines = [
        f"Ставок: {n}, Вин: {wins}, Луз: {loses}, Н/д: {pend}",
        f"Проходимость: {winrate:.1f}%",
        f"Профит (ставка=1): {profit:+.2f}"
    ]
    return "\n".join(lines)

def send_daily_report(now):
    key = now.strftime("%Y-%m-%d")
    if state.get("last_daily") == key: return
    todays = [s for s in state["signals"] if s.get("date_signal") == key]
    text = "📊 *Отчёт за день*\n" + (build_report(todays) if todays else "За сегодня сигналов не было.")
    send(text)
    state["last_daily"] = key
    save_state()

def send_weekly_report(now):
    # только по воскресеньям
    if now.weekday() != 6: return
    y, w, _ = now.isocalendar()
    key = f"{y}-{w:02d}"
    if state.get("last_weekly") == key: return
    # последние 7 дат
    period = set()
    cur = now.date() - timedelta(days=6)
    for _ in range(7):
        period.add(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    week_sigs = [s for s in state["signals"] if s.get("date_signal") in period]
    text = "📅 *Недельная сводка*\n" + (build_report(week_sigs) if week_sigs else "Сигналов не было.")
    send(text)
    state["last_weekly"] = key
    save_state()

def send_monthly_report(now):
    # только последний день месяца
    if not is_last_day_of_month(now.date()): return
    key = now.strftime("%Y-%m")
    if state.get("last_monthly") == key: return
    month_key = now.strftime("%Y-%m")
    month_sigs = [s for s in state["signals"] if (s.get("date_signal") or "").startswith(month_key)]
    text = "🗓 *Месячная сводка*\n" + (build_report(month_sigs) if month_sigs else "Сигналов не было.")
    send(text)
    state["last_monthly"] = key
    save_state()

# ==================== Триггеры по времени ====================

def should_run_once_per_day(now: datetime, at_time: dtime, last_key_name: str) -> bool:
    """
    Универсальная проверка "один раз в день после заданного времени".
    Используем для SCAN (08:00) и DAILY (23:30).
    last_key_name: 'last_scan_date' или 'last_daily'
    """
    today = now.strftime("%Y-%m-%d")
    if state.get(last_key_name) == today:  # уже делали сегодня
        return False
    return now.time() >= at_time

# ==================== RUN ====================

if __name__ == "__main__":
    load_state()

    send(
        "🚀 Бот запущен (прематч H2H+форма ≥3).\n"
        f"🕗 Скан: {SCAN_TIME.strftime('%H:%M')}  |  🗓 TZ: {TIMEZONE}\n"
        f"📅 Дневной отчёт: {DAILY_TIME.strftime('%H:%M')}\n"
        f"📅 Недельный: вс {WEEKLY_TIME.strftime('%H:%M')}  |  Месячный: {MONTHLY_TIME.strftime('%H:%M')}\n"
        + (f"🔎 Фильтр по кэф ТБ2.5: ≥ {MIN_ODDS:.2f}" if MIN_ODDS_CHECK else "🔎 Фильтр по кэф ТБ2.5: отключён.")
    )

    while True:
        try:
            now = now_local()

            # 1) Ежедневный СКАН в 08:00 (один раз в день)
            if should_run_once_per_day(now, SCAN_TIME, "last_scan_date"):
                scan_upcoming_and_signal()
                state["last_scan_date"] = now.strftime("%Y-%m-%d")
                save_state()

            # 2) Обновляем результаты иногда (каждый час в :00)
            if now.minute == 0:
                settle_open_signals()

            # 3) Дневной отчёт 23:30 (один раз в день)
            if now.time().hour == DAILY_TIME.hour and now.time().minute == DAILY_TIME.minute:
                send_daily_report(now)
                time.sleep(60)  # анти-дубль на эту же минуту

            # 4) Недельный отчёт вс 23:50
            if now.time().hour == WEEKLY_TIME.hour and now.time().minute == WEEKLY_TIME.minute:
                send_weekly_report(now)
                time.sleep(60)

            # 5) Месячный отчёт (последний день) 23:50
            if now.time().hour == MONTHLY_TIME.hour and now.time().minute == MONTHLY_TIME.minute:
                send_monthly_report(now)
                time.sleep(60)

            time.sleep(SLEEP_SEC)

        except Exception as e:
            log.error("Main loop error: %s", e)
            time.sleep(SLEEP_SEC)
