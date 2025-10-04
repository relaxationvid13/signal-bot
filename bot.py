# -*- coding: utf-8 -*-
"""
Предматч-бот (Render-ready).
Сразу после старта делает разовый скан «сегодня», затем работает по расписанию.

Стратегия (без учёта коэффициентов):
- Берём все сегодняшние матчи (/fixtures?date=YYYY-MM-DD).
- Условие сигнала:
  1) В H2H последних H2H_LAST (=3) встречах было >=1 матчей с ТБ2.5.
  2) У каждой команды в последних LAST_FORM (=2) матчах было >=1 ТБ2.5.

Кэфы не обязательны. Если API отдаст кэфы на O2.5 — покажем, иначе «н/д».

Отчёты (Europe/Warsaw):
- Ежедневно 23:30 — по дню.
- По воскресеньям 23:50 — недельная сводка.
- В последний день месяца 23:50 — месячная сводка.

Команды:
- /scan_now — немедленный скан на сегодня.

Файлы:
- signals.json — состояние сигналов/результатов за разные даты.
- bot.log — логи.

"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta, date
from threading import Thread

import pytz
import requests
import telebot
from flask import Flask

# =================== Параметры/Константы ===================

API_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")
API_KEY     = os.getenv("API_FOOTBALL_KEY")

if not (API_TOKEN and CHAT_ID and API_KEY):
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")
CHAT_ID = int(CHAT_ID)

# Локальная TZ для расписания/отчётов
TIMEZONE     = os.getenv("TZ", "Europe/Warsaw")

# --- Стратегия:
H2H_LAST     = 3     # сколько последних очных считаем
LAST_FORM    = 2     # сколько последних матчей каждой команды проверяем
TB_THRESHOLD = 3     # «ТБ2.5» => total_goals >= 3

# --- Odds (не обязательны):
REQUIRE_ODDS = False     # сигналы отправляются даже без котировок
ODDS_MARKET  = "Over 2.5"  # просто метка для текста
# если всё же захочешь ограничить кэфы:
ODDS_MIN     = 1.01
ODDS_MAX     = 999.0

# --- Файлы/логи:
LOG_FILE   = "bot.log"
STATE_FILE = "signals.json"

# --- Сетевые настройки:
DEFAULT_TIMEOUT = 20

# --- Расписание:
DAILY_SCAN_H  = 8   # 08:00
DAILY_SCAN_M  = 0

DAILY_REPORT_H = 23 # 23:30
DAILY_REPORT_M = 30

WEEKLY_REPORT_H = 23 # вс 23:50
WEEKLY_REPORT_M = 50

MONTHLY_REPORT_H = 23 # последний день месяца 23:50
MONTHLY_REPORT_M = 50


# =================== Инициализация ===================

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("prematch-bot")

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})


# =================== Утилиты ===================

def tz_now():
    return datetime.now(pytz.timezone(TIMEZONE))

def dstr(d: date) -> str:
    return d.strftime("%Y-%m-%d")

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def api_get(url: str, params: dict) -> dict | None:
    try:
        r = API.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("HTTP %s %s %s", r.status_code, url, params)
        r.raise_for_status()
        js = r.json()
        return js
    except Exception as e:
        log.error(f"api_get error: {e}")
        return None

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"picks": {}, "results": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"load_state: {e}")
        return {"picks": {}, "results": {}}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_state: {e}")


# =================== API-Football обёртки ===================

BASE = "https://v3.football.api-sports.io"

def get_fixtures_by_date(d: date):
    """Все матчи на дату d (UTC ISO в ответе)."""
    js = api_get(f"{BASE}/fixtures", {"date": dstr(d)})
    if not js: return []
    return js.get("response", []) or []

def get_h2h(team1_id: int, team2_id: int, last_n: int):
    js = api_get(f"{BASE}/fixtures/headtohead", {"h2h": f"{team1_id}-{team2_id}", "last": last_n})
    if not js: return []
    return js.get("response", []) or []

def get_team_last(team_id: int, last_n: int):
    js = api_get(f"{BASE}/fixtures", {"team": team_id, "last": last_n})
    if not js: return []
    return js.get("response", []) or []

def get_fixture_result(fid: int):
    js = api_get(f"{BASE}/fixtures", {"id": fid})
    if not js: return None
    resp = js.get("response", []) or []
    if not resp: return None
    m = resp[0]
    st = m["fixture"]["status"]["short"]            # FT / AET / PST / TBD / NS ...
    gh = m["goals"]["home"] or 0
    ga = m["goals"]["away"] or 0
    return st, gh, ga

def get_odds_over25(fid: int):
    """
    Пытаемся достать котировки O2.5 (если API отдаст).
    Возвращаем одну усреднённую или None.
    """
    js = api_get(f"{BASE}/odds", {"fixture": fid})
    if not js:
        return None
    resp = js.get("response", []) or []
    if not resp:
        return None

    # структура сложная: пройдёмся по букмекерам/маркетам/селекшенам
    vals = []
    try:
        for item in resp:
            for bk in item.get("bookmakers", []) or []:
                for market in bk.get("bets", []) or []:
                    # Можем ориентироваться по market_name 'Over/Under'
                    # А ещё есть selections в стиле {'value':'Over 2.5','odd':'1.73'}
                    if market.get("name", "").lower().startswith("over"):
                        for sel in market.get("values", []) or []:
                            v = sel.get("value", "").replace("Over ", "").replace("over ", "").strip()
                            try:
                                if abs(float(v) - 2.5) < 1e-6:
                                    odd = float(sel.get("odd"))
                                    vals.append(odd)
                            except:
                                pass
    except Exception as e:
        log.warning(f"odds parse warn: {e}")

    if not vals:
        return None
    # усредняем
    return round(sum(vals)/len(vals), 2)


# =================== Логика стратегии ===================

def total_goals(m) -> int:
    gh = m["goals"]["home"] or 0
    ga = m["goals"]["away"] or 0
    return gh + ga

def count_tb(matches, threshold=TB_THRESHOLD):
    return sum(1 for mm in matches if total_goals(mm) >= threshold)

def is_signal_match(m) -> tuple[bool, dict]:
    """Проверяет один матч на соответствие стратегии. Возвращает (ok, pick_info)."""
    fid = m["fixture"]["id"]
    home_id = m["teams"]["home"]["id"]
    away_id = m["teams"]["away"]["id"]
    home = m["teams"]["home"]["name"]
    away = m["teams"]["away"]["name"]
    league = m["league"]["name"]
    country = m["league"]["country"]
    kickoff = m["fixture"]["date"]

    # 1) H2H
    h2h = get_h2h(home_id, away_id, H2H_LAST)
    h2h_tb_cnt = count_tb(h2h) if h2h else 0
    if h2h_tb_cnt < 1:
        return False, {}

    # 2) Форма команд
    last_home = get_team_last(home_id, LAST_FORM)
    last_away = get_team_last(away_id, LAST_FORM)
    if not last_home or not last_away:
        return False, {}

    if count_tb(last_home) < 1 or count_tb(last_away) < 1:
        return False, {}

    # Odds (опционально)
    odd = get_odds_over25(fid)  # может быть None
    if REQUIRE_ODDS:
        if odd is None or not (ODDS_MIN <= odd <= ODDS_MAX):
            return False, {}

    pick = {
        "fixture_id": fid,
        "home": home,
        "away": away,
        "league": league,
        "country": country,
        "date": kickoff,               # ISO
        "h2h_tb": h2h_tb_cnt,
        "form_home": count_tb(last_home),
        "form_away": count_tb(last_away),
        "market": "O2.5",
        "odd": odd,                    # может быть None
        "created_at": tz_now().isoformat(),
    }
    return True, pick


def scan_day(scan_date: date | None = None) -> list[dict]:
    """Сканирует день, возвращает список сигналов (pick dict)."""
    if scan_date is None:
        scan_date = tz_now().date()

    fixtures = get_fixtures_by_date(scan_date)
    log.info(f"Fixtures on {scan_date}: {len(fixtures)}")

    picks = []
    for m in fixtures:
        ok, pick = is_signal_match(m)
        if ok:
            picks.append(pick)
    return picks


def send_picks(picks: list[dict], title="Сигналы (предматч)"):
    if not picks:
        send("ℹ️ На сегодня подходящих матчей по фильтрам нет.")
        return

    for p in picks:
        odd_text = f"*{p['odd']:.2f}*" if p.get("odd") else "н/д"
        # красивая дата:
        dt = p["date"]
        msg = (
            f"⚽ *{title}*\n"
            f"🏆 {p['country']} — {p['league']}\n"
            f"{p['home']} — {p['away']}\n"
            f"⏰ {dt}\n"
            f"📈 H2H ТБ2.5: {p['h2h_tb']}/{H2H_LAST} | "
            f"форма: {p['form_home']} & {p['form_away']} (из {LAST_FORM})\n"
            f"🎯 Рынок: ТБ 2.5 | кэф: {odd_text}\n"
            "───────────────"
        )
        send(msg)


# =================== Учёт/Отчёты ===================

def date_key(dt: date) -> str:
    return dt.strftime("%Y-%m-%d")

def store_picks(scan_date: date, picks: list[dict]):
    state = load_state()
    key = date_key(scan_date)
    if key not in state["picks"]:
        state["picks"][key] = []
    # добавим только те, которых ещё нет по fixture_id
    known = {p["fixture_id"] for p in state["picks"][key]}
    for p in picks:
        if p["fixture_id"] not in known:
            state["picks"][key].append(p)
    save_state(state)

def evaluate_picks_for_date(day: date):
    """Проверяет результаты матчей из сигналов в этот день. Возвращает list с полями result:WIN/LOSS/NRES"""
    state = load_state()
    key = date_key(day)
    picks = state["picks"].get(key, [])
    results = []
    for p in picks:
        fid = p["fixture_id"]
        res = get_fixture_result(fid)
        if not res:
            results.append({**p, "result": "NRES"})
            continue
        st, gh, ga = res
        if st in ("FT", "AET", "PEN"):   # считаем завершённым
            total = gh + ga
            outcome = "WIN" if total >= TB_THRESHOLD else "LOSS"
            results.append({**p, "result": outcome, "final": f"{gh}-{ga}"})
        else:
            results.append({**p, "result": "NRES"})  # не завершён / перенесён
    return results

def send_daily_report(day: date | None = None):
    if day is None:
        day = tz_now().date()
    results = evaluate_picks_for_date(day)
    if not results:
        send("📊 Отчёт за день\nЗа сегодня сигналов не было.")
        return

    win = sum(1 for r in results if r["result"] == "WIN")
    loss = sum(1 for r in results if r["result"] == "LOSS")
    nres = sum(1 for r in results if r["result"] == "NRES")
    total = len(results)

    lines = [
        "📊 *Отчёт за день*",
        f"Дата: {date_key(day)}",
        f"Сигналов: {total} | ✅: {win} | ❌: {loss} | н/д: {nres}",
        "───────────────"
    ]
    for r in results[:20]:  # не засоряем чат длинным списком
        ftxt = r.get("final", "—")
        lines.append(f"{r['home']} — {r['away']} | {r['result']} | финал: {ftxt}")
    send("\n".join(lines))

def week_range_end(day: date) -> tuple[date, date]:
    # неделя: пн-вс (для отчёта берём прошедшую)
    # найдём понедельник текущей недели:
    wd = day.weekday()  # пн=0
    monday = day - timedelta(days=wd)
    sunday = monday + timedelta(days=6)
    return monday, sunday

def month_range(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    if day.month == 12:
        nxt = day.replace(year=day.year+1, month=1, day=1)
    else:
        nxt = day.replace(month=day.month+1, day=1)
    last = nxt - timedelta(days=1)
    return first, last

def aggregate_report(start: date, end: date):
    state = load_state()
    cur = start
    win = loss = nres = total = 0
    while cur <= end:
        key = date_key(cur)
        if key in state["picks"]:
            res = evaluate_picks_for_date(cur)
            total += len(res)
            win   += sum(1 for r in res if r["result"] == "WIN")
            loss  += sum(1 for r in res if r["result"] == "LOSS")
            nres  += sum(1 for r in res if r["result"] == "NRES")
        cur += timedelta(days=1)
    return total, win, loss, nres

def send_weekly_report():
    today = tz_now().date()
    # отчёт за прошедшую неделю (пн-вс), сегодня — воскресенье
    monday, sunday = week_range_end(today)
    # берём неделю, которая *заканчивается* сегодня
    start = monday
    end   = sunday
    total, win, loss, nres = aggregate_report(start, end)
    lines = [
        "🗓️ *Недельная сводка*",
        f"Период: {date_key(start)} — {date_key(end)}",
        f"Сигналов: {total} | ✅: {win} | ❌: {loss} | н/д: {nres}",
    ]
    send("\n".join(lines))

def send_monthly_report():
    today = tz_now().date()
    start, end = month_range(today)
    total, win, loss, nres = aggregate_report(start, end)
    lines = [
        "📅 *Месячная сводка*",
        f"Период: {date_key(start)} — {date_key(end)}",
        f"Сигналов: {total} | ✅: {win} | ❌: {loss} | н/д: {nres}",
    ]
    send("\n".join(lines))


# =================== Телеграм-команды ===================

@bot.message_handler(commands=["scan_now"])
def handle_scan_now(msg):
    sd = tz_now().date()
    send("⏳ Выполняю ручной скан на сегодня…")
    picks = scan_day(sd)
    store_picks(sd, picks)
    send_picks(picks, title="Сигналы (ручной запуск)")


# =================== Расписание (цикл) ===================

def is_last_day_of_month(d: date) -> bool:
    first, last = month_range(d)
    return d == last

def scheduler_loop():
    """
    Тикаем раз в ~30 сек, сравниваем локальное время (Europe/Warsaw),
    выполняем задачи в нужную минуту. Простейшая защита от повторов — запоминаем «последний запуск на дату».
    """
    last_daily_scan_key = ""
    last_daily_report_key = ""
    last_weekly_key = ""
    last_monthly_key = ""

    while True:
        try:
            now = tz_now()
            dkey = date_key(now.date())
            wd = now.weekday()  # пн=0 … вс=6

            # Ежедневный скан 08:00
            if now.hour == DAILY_SCAN_H and now.minute == DAILY_SCAN_M and last_daily_scan_key != dkey:
                picks = scan_day(now.date())
                store_picks(now.date(), picks)
                send_picks(picks, title="Сигналы (ежедневный скан)")
                last_daily_scan_key = dkey

            # Ежедневный отчёт 23:30
            if now.hour == DAILY_REPORT_H and now.minute == DAILY_REPORT_M and last_daily_report_key != dkey:
                send_daily_report(now.date())
                last_daily_report_key = dkey

            # Недельная сводка (воскресенье) 23:50
            if wd == 6 and now.hour == WEEKLY_REPORT_H and now.minute == WEEKLY_REPORT_M and last_weekly_key != dkey:
                send_weekly_report()
                last_weekly_key = dkey

            # Месячная сводка — в последний день 23:50
            if is_last_day_of_month(now.date()) and now.hour == MONTHLY_REPORT_H and now.minute == MONTHLY_REPORT_M and last_monthly_key != dkey:
                send_monthly_report()
                last_monthly_key = dkey

        except Exception as e:
            log.error(f"scheduler_loop: {e}")
        time.sleep(30)


# =================== Flask (Render keep-alive) ===================

app = Flask(__name__)

@app.get("/")
def health():
    return "ok"


def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


# =================== MAIN ===================

if __name__ == "__main__":
    # 1) поднимаем HTTP для Render
    Thread(target=run_http, daemon=True).start()

    # 2) приветствие + расписание
    send("🚀 Бот запущен (предматч, Render-ready). ❤️\n\nℹ️ График: скан в 08:00; отчёт 23:30; неделя — вс 23:50; месяц — в последний день 23:50.")

    # 3) разовый прогон «сейчас» (сигналы на сегодня) — по просьбе
    try:
        sd = tz_now().date()
        picks_now = scan_day(sd)
        store_picks(sd, picks_now)
        send_picks(picks_now, title="Сигналы (стартовый прогон)")
    except Exception as e:
        log.error(f"startup scan error: {e}")

    # 4) запускаем планировщик
    scheduler_loop()
