# -*- coding: utf-8 -*-
"""
Футбол-бот (эконом):
- сигналы при 2/3 голах (окно ~20') + дневной отчёт 23:30
- еженедельный отчёт (прошедшие 7 суток) — по понедельникам 00:05
- ежемесячный отчёт (прошедший календарный месяц) — в 00:10 первого числа
- все сигналы дня пишутся в signals_YYYY-MM-DD.json (переживает перезапуски)
- исходы добавляются в history.jsonl (1 запись JSON в строке)
"""

import os, sys, time, json, logging
from datetime import datetime, timedelta, date
from threading import Thread
from typing import Iterable

import pytz
import requests
import telebot
from flask import Flask

# -------- HTTP healthcheck для Render --------
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
# ---------------------------------------------

# ===== Секреты =====
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")
TIMEZONE  = "Europe/Warsaw"

if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

CHAT_ID = int(CHAT_ID)

# ===== Параметры =====
POLL_SECONDS = 15 * 60           # опрос раз в 15 минут (≈96/сутки)
WINDOW_20    = range(19, 23)     # окно «~20'»
STAKE_BR     = 1                 # условная ставка

LOG_FILE     = "bot.log"
HISTORY_FILE = "history.jsonl"   # история рассчитанных исходов

# ===== Логи =====
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("econ-bot")

# ===== Telegram/HTTP =====
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 15


# ===== Помощники времени/дат =====
def tz():
    return pytz.timezone(TIMEZONE)

def now_local() -> datetime:
    return datetime.now(tz())

def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")

def month_bounds_for_previous_month(ref: datetime) -> tuple[datetime, datetime]:
    """Возвращает (start, end) прошедшего календарного месяца в TZ."""
    first_this = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this - timedelta(seconds=1)
    start_prev = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_prev, last_prev.replace(hour=23, minute=59, second=59, microsecond=0)

# ===== Файлы сигналов дня =====
def signals_path(day: str | None = None) -> str:
    if not day:
        day = today_str()
    return f"signals_{day}.json"

def load_signals(day: str | None = None) -> list[dict]:
    path = signals_path(day)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error("load_signals error: %s", e)
        return []

def save_signals(signals: list[dict], day: str | None = None) -> None:
    path = signals_path(day)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(signals, f, ensure_ascii=False)
    except Exception as e:
        log.error("save_signals error: %s", e)

def append_signal(rec: dict) -> None:
    day = today_str()
    arr = load_signals(day)
    if any(x.get("fixture_id") == rec.get("fixture_id") for x in arr):
        return
    arr.append(rec)
    save_signals(arr, day)

# ===== История исходов =====
def append_history(entry: dict) -> None:
    """Запись исхода в history.jsonl (одна строка = один JSON)."""
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("append_history error: %s", e)

def read_history_iter() -> Iterable[dict]:
    """Итератор по строкам history.jsonl (если нет — пусто)."""
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception as e:
        log.error("read_history error: %s", e)

# ===== Отправка в TG =====
def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error("Telegram send error: %s", e)

# ===== API-Football =====
def get_live():
    try:
        r = API.get("https://v3.football.api-sports.io/fixtures?live=all", timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("fixtures HTTP %s %s", r.status_code, r.text[:200])
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error("get_live error: %s", e)
        return []

def get_fixture_result(fid: int):
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
        log.error("get_fixture_result(%s) error: %s", fid, e)
        return None

# ===== Сканер лайвов (сигналы) =====
def scan_and_signal():
    live = get_live()
    for m in live:
        try:
            f = m["fixture"]; t = m["teams"]; g = m["goals"]; L = m["league"]
            fid = int(f["id"])
            elapsed = int(f["status"]["elapsed"] or 0)

            if elapsed not in WINDOW_20:
                continue

            gh, ga = (g["home"] or 0), (g["away"] or 0)
            total_goals = gh + ga
            if total_goals not in (2, 3):
                continue

            line = 3 if total_goals == 2 else 4

            rec = {
                "fixture_id": fid,
                "utc": f["date"],
                "minute": elapsed,
                "home": t["home"]["name"],
                "away": t["away"]["name"],
                "league": L["name"],
                "country": L["country"],
                "goals_home": int(gh),
                "goals_away": int(ga),
                "total_goals": int(total_goals),
                "bet_line": f"ТБ {line}",
                "odds": None,  # кэфов в фри-API нет
            }

            append_signal(rec)
            send(
                "⚽️ *Ставка!*\n"
                f"🏆 {rec['country']} — {rec['league']}\n"
                f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                f"⏱ {elapsed}'  • {rec['bet_line']}\n"
                "─────────────"
            )
            log.info("Signal sent for %s - %s (fid=%s)", rec['home'], rec['away'], fid)

        except Exception as e:
            log.error("scan_and_signal item error: %s", e)

# ===== Ежедневный отчёт =====
def send_daily_report():
    day = today_str()
    signals = load_signals(day)

    if not signals:
        send("🗒 За сегодня ставок не было.")
        return

    wins = losses = 0
    pnl_total = 0

    lines = ["📊 *Отчёт за день*"]
    settled_entries = []  # что положим в history.jsonl

    for idx, rec in enumerate(signals, start=1):
        fid = rec["fixture_id"]
        line = 3 if rec["total_goals"] == 2 else 4
        res = get_fixture_result(fid)

        if not res:
            lines.append(f"#{idx} {rec['home']} — {rec['away']} | результат недоступен")
            continue

        st, gh, ga = res
        total = gh + ga

        entry = {
            "ts": now_local().isoformat(),
            "date": day,
            "fixture_id": fid,
            "home": rec["home"],
            "away": rec["away"],
            "league": rec["league"],
            "country": rec["country"],
            "bet_line": f"ТБ {line}",
            "result_score": f"{gh}-{ga}",
            "status": st,
            "pnl": 0
        }

        if st == "FT":
            if total > line:
                wins += 1
                pnl = +STAKE_BR
                entry["pnl"] = pnl
                entry["outcome"] = "win"
                lines.append(f"#{idx} ✅ {rec['home']} {gh}-{ga} {rec['away']} | {entry['bet_line']} | +{pnl}")
            else:
                losses += 1
                pnl = -STAKE_BR
                entry["pnl"] = pnl
                entry["outcome"] = "loss"
                lines.append(f"#{idx} ❌ {rec['home']} {gh}-{ga} {rec['away']} | {entry['bet_line']} | {pnl}")
            pnl_total += pnl
            settled_entries.append(entry)
        else:
            lines.append(f"#{idx} ⏳ {rec['home']} — {rec['away']} | статус: {st}")

    total_bets = wins + losses
    passrate = (wins / total_bets * 100) if total_bets else 0.0

    lines.append("─────────────")
    lines.append(f"Всего ставок: {total_bets}")
    lines.append(f"Проходимость: {passrate:.0f}%")
    lines.append(f"Прибыль: {pnl_total:+.0f} (ставка {STAKE_BR})")

    send("\n".join(lines))

    # записать историю и очистить файл дня
    for e in settled_entries:
        append_history(e)
    save_signals([], day)

# ===== Агрегация периодов из history.jsonl =====
def aggregate_history(start_dt: datetime, end_dt: datetime) -> dict:
    """Сводка по истории за период [start_dt, end_dt] в TZ."""
    s_utc = start_dt.astimezone(pytz.UTC)
    e_utc = end_dt.astimezone(pytz.UTC)

    bets = wins = losses = 0
    pnl_total = 0

    for row in read_history_iter() or []:
        try:
            ts = datetime.fromisoformat(row.get("ts"))
        except Exception:
            continue
        if not (s_utc <= ts.astimezone(pytz.UTC) <= e_utc):
            continue
        if row.get("outcome") not in ("win", "loss"):
            continue

        bets += 1
        pnl_total += int(row.get("pnl", 0))
        if row["outcome"] == "win":
            wins += 1
        else:
            losses += 1

    return {
        "bets": bets,
        "wins": wins,
        "losses": losses,
        "pnl": pnl_total,
        "passrate": (wins / bets * 100) if bets else 0.0
    }

def send_weekly_report():
    """Прошедшие 7 суток (понедельник 00:05)."""
    end_dt = now_local().replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=7)
    agg = aggregate_history(start_dt, end_dt)
    text = (
        "📅 *Отчёт за неделю*\n"
        f"Период: {start_dt.strftime('%d.%m %H:%M')} — {end_dt.strftime('%d.%m %H:%M')}\n"
        "─────────────\n"
        f"Ставок: {agg['bets']}\n"
        f"Сыграло: {agg['wins']}  |  Не сыграло: {agg['losses']}\n"
        f"Проходимость: {agg['passrate']:.0f}%\n"
        f"Итог: {agg['pnl']:+.0f} (ставка {STAKE_BR})"
    )
    send(text)

def send_monthly_report():
    """Прошедший календарный месяц (1-го числа 00:10)."""
    start_prev, end_prev = month_bounds_for_previous_month(now_local())
    agg = aggregate_history(start_prev, end_prev)
    text = (
        "🗓 *Отчёт за месяц*\n"
        f"Период: {start_prev.strftime('%d.%m')} — {end_prev.strftime('%d.%m')}\n"
        "─────────────\n"
        f"Ставок: {agg['bets']}\n"
        f"Сыграло: {agg['wins']}  |  Не сыграло: {agg['losses']}\n"
        f"Проходимость: {agg['passrate']:.0f}%\n"
        f"Итог: {agg['pnl']:+.0f} (ставка {STAKE_BR})"
    )
    send(text)

# ===== RUN =====
if __name__ == "__main__":
    Thread(target=run_http, daemon=True).start()

    send("🚀 Бот запущен — новая версия!")
    send("✅ Режим: сигналы при 2/3 голах (~20'), отчёт 23:30. Недельная и месячная сводки включены.")

    while True:
        try:
            scan_and_signal()

            now = now_local()

            # Ежедневный отчёт — 23:30
            if now.hour == 23 and now.minute == 30:
                send_daily_report()
                time.sleep(60)

            # Еженедельный — по понедельникам 00:05 (weekday()==0)
            if now.weekday() == 0 and now.hour == 0 and now.minute == 5:
                send_weekly_report()
                time.sleep(60)

            # Ежемесячный — 1-го числа 00:10
            if now.day == 1 and now.hour == 0 and now.minute == 10:
                send_monthly_report()
                time.sleep(60)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.error("Main loop error: %s", e)
            time.sleep(POLL_SECONDS)
