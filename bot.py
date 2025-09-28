# -*- coding: utf-8 -*-
"""
Футбольный бот:
- Окно активности (Europe/Warsaw): 16:00–23:29, опрос live каждые 5 минут
- Надёжный отчёт в 23:30 (даже если проспали момент)
- Недельная сводка (вс, 23:30) и месячная сводка (последний день месяца, 23:30)
- Стратегии:
  OVER-20: к ~20' уже 2/3 гола -> ТБ3/ТБ4 (коэф по желанию)
  UNDER-20: к ~20' 0:0 -> ТМ3 (коэф по желанию)
Рендер (Web Service, Free): поднимается легкий Flask-сервер для "живого" порта.
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, date, time as dtime, timedelta

import pytz
import requests
import telebot

# ---- Render keep-alive (Flask) ----
from threading import Thread
from flask import Flask

app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ========= Секреты/окружение =========
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")

# Часовой пояс: Варшава (Польша)
TIMEZONE  = "Europe/Warsaw"

if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")
CHAT_ID = int(CHAT_ID)

# ============= Параметры =============
# Активное окно (локальное)
ACTIVE_FROM = dtime(16, 0)   # 16:00
ACTIVE_TILL = dtime(23, 29)  # 23:29
POLL_SEC    = 5 * 60         # 5 минут внутри окна
IDLE_SEC    = 10 * 60        # 10 мин снаружи окна

# Окно "около 20-й минуты"
WINDOW_20   = range(19, 23)  # 19..22 включительно

# Коэффициенты (включаются флагами ниже)
# OVER-20: 2 гола -> ТБ3 (нужен >=4 мячей, чтобы пройти),
#          3 гола -> ТБ4 (нужен >=5 мячей, чтобы пройти)
USE_ODDS_OVER   = False   # включить проверку коэф для OVER-20
ODDS_MIN_OVER   = 1.29
ODDS_MAX_OVER   = 2.00

# UNDER-20: 0 голов -> ТМ3 (обычно <=2 гола, чтобы пройти)
USE_ODDS_UNDER  = False   # включить проверку коэф для UNDER-20
ODDS_MIN_UNDER  = 1.60

# Условная ставка для расчёта PnL в отчётах
STAKE          = 1.0

# Файлы логов/состояния
LOG_FILE   = "bot.log"
STATE_FILE = "signals.json"

# Время отчёта
REPORT_TIME = dtime(23, 30)  # 23:30

# ========= Настройка логов ===========
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("signal-bot")

# ========= Телеграм =========
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ========= API-Football =========
API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 15

def get_live_fixtures():
    """
    Возвращает список live-матчей: API-Football /fixtures?live=all
    """
    try:
        r = API.get(
            "https://v3.football.api-sports.io/fixtures?live=all",
            timeout=DEFAULT_TIMEOUT
        )
        if r.status_code != 200:
            log.warning("fixtures HTTP %s %s", r.status_code, r.text[:200])
        r.raise_for_status()
        return r.json().get("response", []) or []
    except Exception as e:
        log.error(f"get_live_fixtures error: {e}")
        return []

def get_fixture_by_id(fid: int):
    """
    Один матч по id, для получения финального счёта/статуса.
    """
    try:
        r = API.get(
            f"https://v3.football.api-sports.io/fixtures?id={fid}",
            timeout=DEFAULT_TIMEOUT
        )
        r.raise_for_status()
        resp = r.json().get("response", []) or []
        return resp[0] if resp else None
    except Exception as e:
        log.error(f"get_fixture_by_id({fid}) error: {e}")
        return None

# ВНИМАНИЕ: endpoint кэфов в API-Football может требовать расширенный тариф.
# Ниже — «заглушка» со схемой; сделайте реальный вызов и маппинг под ваш тариф.
def get_odds_for_market(fid: int, market_code: str):
    """
    Получить коэффициент для рынка.
    market_code примеры: 'over_3', 'over_4', 'under_3' — это ваши условные "ключи".
    Здесь вернём None (нет кэфа), чтобы не тратить квоту. Включите и реализуйте,
    когда будет доступ к odds endpoint.
    """
    # пример схему (закомментирован):
    # url = f"https://v3.football.api-sports.io/odds?fixture={fid}"
    # r = API.get(url, timeout=DEFAULT_TIMEOUT)
    # parse = ...
    return None  # отключено по умолчанию

# ========= Память/состояние =========
# signals: список словарей:
#  {
#     "date": "YYYY-MM-DD",
#     "fixture_id": int,
#     "home": str, "away": str,
#     "country": str, "league": str,
#     "snapshot_minute": int,
#     "snapshot_score_home": int, "snapshot_score_away": int,
#     "signal_type": "OVER20_TB3"|"OVER20_TB4"|"UNDER20_TM3",
#     "odds": float|null
#  }
signals = []
signaled_ids = set()  # чтобы не дублировать во время дня
_last_report_for_date = None  # отметка отчётов

def load_state():
    global signals, signaled_ids, _last_report_for_date
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        signals = data.get("signals", [])
        signaled_ids = set(data.get("signaled_ids", []))
        rep = data.get("last_report_date")
        _last_report_for_date = date.fromisoformat(rep) if rep else None
    except Exception as e:
        log.error(f"load_state error: {e}")

def save_state():
    try:
        data = {
            "signals": signals,
            "signaled_ids": list(signaled_ids),
            "last_report_date": _last_report_for_date.isoformat() if _last_report_for_date else None
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.error(f"save_state error: {e}")

# ========= Время/окна =========
def now_local() -> datetime:
    return datetime.now(pytz.timezone(TIMEZONE))

def in_active_window(now: datetime) -> bool:
    t = now.time()
    return ACTIVE_FROM <= t <= ACTIVE_TILL

def is_last_day_of_month(d: date) -> bool:
    next_day = d + timedelta(days=1)
    return next_day.day == 1

# ======== Отчёты: robust-triggers ========
def need_daily_report(now: datetime) -> bool:
    """
    True, если отчёт за 'сегодня' ещё не отправлялся и локальное время >= 23:30,
    или если уже наступил новый день (00:xx), а вчерашний отчёт не был отправлен (догоняем).
    """
    global _last_report_for_date
    today = now.date()

    # если сегодня >= 23:30 и отчёта ещё не было
    if _last_report_for_date != today and now.time() >= REPORT_TIME:
        return True

    # если уже новый день, а за вчера мы не отчитались (бот перезапустили/проспали)
    yesterday = today - timedelta(days=1)
    if _last_report_for_date != yesterday and now.time() < REPORT_TIME:
        return True

    return False

def mark_report_sent(now: datetime):
    global _last_report_for_date
    _last_report_for_date = now.date()
    save_state()

# ======== Стратегии: детект & фиксация сигналов ========
def push_signal(now: datetime, m, sig_type: str, odds: float | None):
    """
    Сохраняем сигнал (один раз на fixture_id в рамках дня), отправляем в Telegram.
    """
    f = m["fixture"]
    L = m["league"]
    g = m["goals"]
    t = m["teams"]

    fid = f["id"]
    elapsed = f["status"]["elapsed"] or 0
    gh, ga = g["home"] or 0, g["away"] or 0

    rec = {
        "date": now.date().isoformat(),
        "fixture_id": fid,
        "home": t["home"]["name"],
        "away": t["away"]["name"],
        "country": L["country"],
        "league": L["name"],
        "snapshot_minute": int(elapsed),
        "snapshot_score_home": gh,
        "snapshot_score_away": ga,
        "signal_type": sig_type,
        "odds": odds
    }
    signals.append(rec)
    signaled_ids.add(fid)
    save_state()

    text = []
    text.append("⚪ *Сигнал!*")
    text.append(f"🏆 {rec['country']} — {rec['league']}")
    text.append(f"{rec['home']} {gh} — {ga} {rec['away']}")
    text.append(f"⏱ ~{elapsed}'")
    if sig_type == "OVER20_TB3":
        text.append("Стратегия: OVER-20 → *ТБ3*")
    elif sig_type == "OVER20_TB4":
        text.append("Стратегия: OVER-20 → *ТБ4*")
    elif sig_type == "UNDER20_TM3":
        text.append("Стратегия: UNDER-20 → *ТМ3*")
    if odds:
        text.append(f"Коэф: *{odds:.2f}*")
    text.append("─────────────")
    send("\n".join(text))

def check_over20(m) -> tuple[bool, str | None, float | None]:
    """
    Если к ~20' уже 2 или 3 гола:
     - 2 гола -> ТБ3 (возвращаем sig_type='OVER20_TB3')
     - 3 гола -> ТБ4 (sig_type='OVER20_TB4')
    Возвращает (ok, sig_type, odds).
    При включённом USE_ODDS_OVER дополнительно проверяет окно коэффициентов.
    """
    f = m["fixture"]
    g = m["goals"]
    elapsed = f["status"]["elapsed"] or 0
    if elapsed not in WINDOW_20:
        return False, None, None

    gh, ga = g["home"] or 0, g["away"] or 0
    total = gh + ga
    if total == 2:
        sig_type = "OVER20_TB3"
        odds = None
        if USE_ODDS_OVER:
            odds = get_odds_for_market(f["id"], "over_3")
            if odds is None or not (ODDS_MIN_OVER <= odds <= ODDS_MAX_OVER):
                return False, None, None
        return True, sig_type, odds

    if total == 3:
        sig_type = "OVER20_TB4"
        odds = None
        if USE_ODDS_OVER:
            odds = get_odds_for_market(f["id"], "over_4")
            if odds is None or not (ODDS_MIN_OVER <= odds <= ODDS_MAX_OVER):
                return False, None, None
        return True, sig_type, odds

    return False, None, None

def check_under20(m) -> tuple[bool, str | None, float | None]:
    """
    Если к ~20' счёт 0:0 — ставка ТМ3 (UNDER-20).
    Возвращает (ok, 'UNDER20_TM3', odds).
    При включённом USE_ODDS_UNDER дополнительно проверяет, что коэф >= ODDS_MIN_UNDER.
    """
    f = m["fixture"]
    g = m["goals"]
    elapsed = f["status"]["elapsed"] or 0
    if elapsed not in WINDOW_20:
        return False, None, None

    gh, ga = g["home"] or 0, g["away"] or 0
    if (gh + ga) != 0:
        return False, None, None

    sig_type = "UNDER20_TM3"
    odds = None
    if USE_ODDS_UNDER:
        odds = get_odds_for_market(f["id"], "under_3")
        if odds is None or odds < ODDS_MIN_UNDER:
            return False, None, None
    return True, sig_type, odds

def scan_and_signal(now: datetime) -> bool:
    """
    Возвращает True, если был отправлен хотя бы один сигнал (для быстрой паузы).
    """
    live = get_live_fixtures()
    sent_any = False
    for m in live:
        try:
            f = m["fixture"]
            fid = f["id"]

            # не дублировать сигнал для этого матча в текущий день
            if fid in signaled_ids:
                continue

            ok, sig_type, odds = check_over20(m)
            if ok:
                push_signal(now, m, sig_type, odds)
                sent_any = True
                continue

            ok, sig_type, odds = check_under20(m)
            if ok:
                push_signal(now, m, sig_type, odds)
                sent_any = True
                continue

        except Exception as e:
            log.error(f"scan_and_signal item error: {e}")
    return sent_any

# ========= Подсчёт результатов =========
def settle_signal(rec: dict) -> tuple[bool, int]:
    """
    Возвращает (is_win, required_goals_for_win)
    OVER20_TB3 -> выигрыш если финальный total >= 4
    OVER20_TB4 -> выигрыш если финальный total >= 5
    UNDER20_TM3 -> выигрыш если финальный total <= 2
    """
    fid = rec["fixture_id"]
    snap_total = rec["snapshot_score_home"] + rec["snapshot_score_away"]
    sig = rec["signal_type"]

    data = get_fixture_by_id(fid)
    if not data:
        return False, -999  # неизвестно

    st = data["fixture"]["status"]["short"]
    gh = data["goals"]["home"] or 0
    ga = data["goals"]["away"] or 0
    final_total = gh + ga

    if st != "FT":  # матч не завершён, считаем как "нет данных"
        return False, -998

    if sig == "OVER20_TB3":
        return (final_total >= 4), 4
    if sig == "OVER20_TB4":
        return (final_total >= 5), 5
    if sig == "UNDER20_TM3":
        return (final_total <= 2), 3  # ТМ3 => выигрыш при финале <=2

    return False, -997

# ========= Отчёты =========
def daily_report(now: datetime):
    """
    Отчёт за сегодня (по дате из rec['date'] == сегодня).
    """
    today = now.date().isoformat()
    today_signals = [r for r in signals if r["date"] == today]

    wins = loses = unknown = 0
    pnl = 0.0
    lines = ["📊 *Отчёт за день*"]

    if not today_signals:
        lines.append("За сегодня ставок не было.")
        send("\n".join(lines))
        return

    for i, rec in enumerate(today_signals, 1):
        ok, needed = settle_signal(rec)
        home = rec["home"]; away = rec["away"]
        sig  = rec["signal_type"]
        odds = rec.get("odds")

        # итоги
        if ok is True:
            wins += 1
            pnl += +STAKE
            mark = "✅"
        elif ok is False and needed in (-997, -998, -999):
            unknown += 1
            mark = "…"
        else:
            loses += 1
            pnl += -STAKE
            mark = "❌"

        oddstxt = f" | {odds:.2f}" if odds else ""
        # краткий формат
        lines.append(f"#{i} {mark} {home} — {away} | {sig}{oddstxt}")

    total = len(today_signals)
    passrate = (wins / total * 100.0) if total else 0.0

    lines.append("")
    lines.append(f"Итого: {wins} / {total}  | Проходимость: {passrate:.1f}%")
    lines.append(f"Профит (ставка={STAKE:g}): {pnl:+.2f}")
    send("\n".join(lines))

def _aggregate_by_period(dfrom: date, dto: date):
    subset = [r for r in signals if dfrom <= date.fromisoformat(r["date"]) <= dto]
    wins = loses = unknown = 0
    pnl = 0.0
    for rec in subset:
        ok, _ = settle_signal(rec)
        if ok is True:
            wins += 1
            pnl += +STAKE
        elif ok is False and _ in (-997, -998, -999):
            unknown += 1
        else:
            loses += 1
            pnl += -STAKE
    total = len(subset)
    rate = (wins / total * 100.0) if total else 0.0
    return total, wins, loses, unknown, pnl, rate

def weekly_report(now: datetime):
    # отчёт за ISO-неделю: понедельник..воскресенье (включ.)
    end = now.date()
    start = end - timedelta(days=6)
    total, wins, loses, unknown, pnl, rate = _aggregate_by_period(start, end)
    lines = [
        "📅 *Недельная сводка*",
        f"Период: {start.isoformat()} — {end.isoformat()}",
        f"Ставок: {total}, Вин: {wins}, Луз: {loses}, Н/д: {unknown}",
        f"Проходимость: {rate:.1f}%",
        f"Профит (ставка={STAKE:g}): {pnl:+.2f}"
    ]
    send("\n".join(lines))

def monthly_report(now: datetime):
    end = now.date()
    start = end.replace(day=1)
    total, wins, loses, unknown, pnl, rate = _aggregate_by_period(start, end)
    lines = [
        "🗓️ *Месячная сводка*",
        f"Период: {start.isoformat()} — {end.isoformat()}",
        f"Ставок: {total}, Вин: {wins}, Луз: {loses}, Н/д: {unknown}",
        f"Проходимость: {rate:.1f}%",
        f"Профит (ставка={STAKE:g}): {pnl:+.2f}"
    ]
    send("\n".join(lines))

# ========= RUN =========
if __name__ == "__main__":
    # HTTP для Render
    Thread(target=run_http, daemon=True).start()

    load_state()

    send("🚀 Бот запущен — новая версия!")
    send(
        "✅ Активное окно: 16:00–23:29 (PL), опрос каждые 5 минут.\n"
        "Отчёты — в 23:30.\n"
        "Стратегии: OVER-20 (2/3 гола → ТБ3/4), UNDER-20 (0–0 → ТМ3).\n"
        f"Фильтры коэфов: OVER={USE_ODDS_OVER} "
        f"(окно {ODDS_MIN_OVER:.2f}–{ODDS_MAX_OVER:.2f}), "
        f"UNDER={USE_ODDS_UNDER} (≥ {ODDS_MIN_UNDER:.2f})."
    )

    while True:
        try:
            now = now_local()

            # --- отчётный блок, устойчивый к "проспали минуту" ---
            if need_daily_report(now):
                daily_report(now)
                # недельная — по воскресеньям
                if now.weekday() == 6:
                    weekly_report(now)
                # месячная — в последний день месяца
                if is_last_day_of_month(now.date()):
                    monthly_report(now)
                mark_report_sent(now)
                time.sleep(60)    # чтобы не задвоить отчёт
                continue

            # --- основная логика опроса ---
            if in_active_window(now):
                was = scan_and_signal(now)
                # если «горячо» — слегка передохнём
                time.sleep(60 if was else POLL_SEC)
            else:
                time.sleep(IDLE_SEC)

        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(POLL_SEC)
