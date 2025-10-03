# -*- coding: utf-8 -*-
"""
Предматчевый сканер на API-Football.
Условия сигнала:
  - H2H: из последних 3 очных матчей >= 2 были ТБ2.5
  - Есть котировка ТБ2.5 в диапазоне [ODDS_MIN; ODDS_MAX]
Форма команд выключена (CHECK_FORM=False), можно включить.

График:
  - скан: 08:00 по TZ
  - дневной отчёт: 23:30
  - недельный отчёт: вс 23:50
  - месячный отчёт: последний день месяца 23:50
"""

import os, sys, json, time, logging
from datetime import datetime, timedelta, date
import calendar
import pytz
import requests
import telebot

# --- keep-alive для Render (web service) ---
from threading import Thread
from flask import Flask
app = Flask(__name__)

@app.get("/")
def health():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
# -------------------------------------------

# ====== Параметры ======
API_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
API_KEY    = os.getenv("API_FOOTBALL_KEY")
TIMEZONE   = os.getenv("TZ", "Europe/Warsaw")
if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нужно задать TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

CHAT_ID = int(CHAT_ID)

# Диапазон котировок на ТБ2.5
ODDS_MIN = 1.29
ODDS_MAX = 2.00

# H2H правило
H2H_LAST = 3
H2H_REQUIRE_TB = 2  # из 3 минимум 2 матча с тоталом >=3

# Проверка "формы" команд (по умолчанию выключена)
CHECK_FORM = False
FORM_LAST = 5
FORM_REQUIRE_TB = 2

# Ставка-единица для отчётов
STAKE = 1.0

# Файлы
LOG_FILE    = "bot.log"
STATE_FILE  = "signals.json"  # здесь храним все сигналы и расчёт по датам

# Время задач (часы/минуты в TZ)
SCAN_HR, SCAN_MIN = (8, 0)           # 08:00 скан на сегодня
DAILY_HR, DAILY_MIN = (23, 30)       # 23:30 отчёт за день
WEEKLY_HR, WEEKLY_MIN = (23, 50)     # 23:50 отчёт за неделю (вс)
MONTHLY_HR, MONTHLY_MIN = (23, 50)   # 23:50 отчёт за месяц (посл. день)

# ====== Логгер ======
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("prematch-bot")

# ====== Telegram ======
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# Команда ручного скана
@bot.message_handler(commands=['scan_now'])
def cmd_scan_now(m):
    try:
        dt = now_local().date()
        cnt = scan_day(dt)
        send(f"🔎 Ручной скан выполнен: найдено сигналов: *{cnt}*.")
    except Exception as e:
        send(f"❌ Ошибка скана: {e}")
        log.exception("scan_now failed")

def telebot_polling():
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            log.error(f"telebot polling error: {e}")
            time.sleep(5)

# ====== API-Football ======
API_BASE = "https://v3.football.api-sports.io"
SESS = requests.Session()
SESS.headers.update({"x-apisports-key": API_KEY})

def api_get(path, params=None, timeout=20):
    url = f"{API_BASE}/{path}"
    r = SESS.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data.get("response", []) or []

# ====== Вспомогательные ======
def now_local():
    return datetime.now(pytz.timezone(TIMEZONE))

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"picks": {}}   # picks: { "YYYY-MM-DD": [ {...}, ... ] }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"load_state err: {e}")
        return {"picks": {}}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_state err: {e}")

def append_pick(state, d: date, pick: dict):
    ds = d.isoformat()
    state.setdefault("picks", {}).setdefault(ds, []).append(pick)
    save_state(state)

def list_picks_between(state, d1: date, d2: date):
    """Все сигналы/пики в диапазоне дат включительно."""
    res = []
    p = state.get("picks", {})
    cur = d1
    while cur <= d2:
        res.extend(p.get(cur.isoformat(), []))
        cur += timedelta(days=1)
    return res

def settle_pick(fx):
    """Вернуть (done, win, gh, ga). done==True если финальный счёт; win==True если ТБ2.5."""
    st = fx["fixture"]["status"]["short"]
    if st not in ("FT", "AET", "PEN"):
        return False, None, None, None
    gh = fx["goals"]["home"] or 0
    ga = fx["goals"]["away"] or 0
    win = (gh + ga) > 2.5
    return True, win, gh, ga

# ====== Логика отбора ======
def goals_total_3plus(m) -> bool:
    gh = m["goals"]["home"] or 0
    ga = m["goals"]["away"] or 0
    return (gh + ga) >= 3

def pass_h2h(home_id, away_id):
    resp = api_get("fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": H2H_LAST})
    cnt = sum(1 for m in resp if goals_total_3plus(m))
    return cnt >= H2H_REQUIRE_TB, cnt

def count_tb25_in_last(team_id, last_n):
    resp = api_get("fixtures", {"team": team_id, "last": last_n})
    return sum(1 for m in resp if goals_total_3plus(m))

def pass_form(team_id):
    cnt = count_tb25_in_last(team_id, FORM_LAST)
    return cnt >= FORM_REQUIRE_TB, cnt

def find_over25_odds(fixture_id):
    """Возвращает список котировок (по букмекерам) на 'Over 2.5'."""
    odds_resp = api_get("odds", {"fixture": fixture_id})
    res = []
    for book in odds_resp:
        for market in (book.get("bookmakers") or []):
            # new format: book['bookmakers'] is a list; each has 'bets'
            # в API-Football market 'Over/Under'
            for bet in market.get("bets", []):
                if bet.get("name", "").lower() in ("over/under", "over-under", "total", "totals"):
                    for v in bet.get("values", []):
                        # ищем Over 2.5
                        val_name = (v.get("value") or "").strip().lower()
                        if val_name in ("over 2.5", "o 2.5", "2.5 over", "over2.5"):
                            odd = float(v.get("odd", 0))
                            if odd > 0:
                                res.append(odd)
    # альтернативный древний формат
    if not res:
        # odds_resp может быть списком bookmakers на верхнем уровне:
        for b in odds_resp:
            for mkt in b.get("bets", []):
                if mkt.get("name", "").lower() in ("over/under", "over-under", "total", "totals"):
                    for v in mkt.get("values", []):
                        val_name = (v.get("value") or "").strip().lower()
                        if val_name in ("over 2.5", "o 2.5", "2.5 over", "over2.5"):
                            odd = float(v.get("odd", 0))
                            if odd > 0:
                                res.append(odd)
    return res

def pass_odds_range(odds_list):
    """Есть ли хотя бы одна котировка в диапазоне [ODDS_MIN; ODDS_MAX]."""
    for x in odds_list:
        if ODDS_MIN <= x <= ODDS_MAX:
            return True, x
    return False, None

# ====== Скан дня ======
def scan_day(d: date) -> int:
    """Сканируем все матчи на дату d, отправляем сигналы и пишем в базу."""
    state = load_state()
    total_signals = 0

    # забираем все матчи за день
    fixtures = api_get("fixtures", {"date": d.isoformat(), "timezone": TIMEZONE})

    for m in fixtures:
        try:
            fid = m["fixture"]["id"]
            home = m["teams"]["home"]["name"]
            away = m["teams"]["away"]["name"]
            hid = m["teams"]["home"]["id"]
            aid = m["teams"]["away"]["id"]

            # H2H
            ok_h2h, cnt_h2h = pass_h2h(hid, aid)
            if not ok_h2h:
                log.info(f"[{home}-{away}] skip: h2h cntTB={cnt_h2h}/{H2H_LAST}")
                continue

            # Форма (по умолчанию выключена)
            if CHECK_FORM:
                ok_home, form_home = pass_form(hid)
                ok_away, form_away = pass_form(aid)
                if not (ok_home and ok_away):
                    log.info(f"[{home}-{away}] skip: form H={form_home}/{FORM_LAST} A={form_away}/{FORM_LAST}")
                    continue

            # Котировки на Овер 2.5
            odds = find_over25_odds(fid)
            ok_odds, chosen_odd = pass_odds_range(odds)
            if not ok_odds:
                log.info(f"[{home}-{away}] skip: no odds O2.5 in [{ODDS_MIN};{ODDS_MAX}] (found: {odds[:5]}...)")
                continue

            # сигнал!
            total_signals += 1
            pick = {
                "fixture_id": fid,
                "home": home,
                "away": away,
                "league": m["league"]["name"],
                "country": m["league"]["country"],
                "date": d.isoformat(),
                "kickoff": m["fixture"]["date"],  # ISO
                "h2h_tb_cnt": cnt_h2h,
                "odd": chosen_odd,
                "market": "O2.5",
                "created_at": now_local().isoformat(),
            }
            append_pick(state, d, pick)

            msg = (
                "⚽ *Сигнал (предматч)*\n"
                f"🏆 {pick['country']} — {pick['league']}\n"
                f"{home} — {away}\n"
                f"⏰ {pick['kickoff']}\n"
                f"📈 H2H ТБ2.5: {cnt_h2h}/{H2H_LAST}\n"
                f"🎯 Рынок: ТБ 2.5 | кэф ~ *{chosen_odd:.2f}*\n"
                "───────────────"
            )
            send(msg)
            log.info(f"signal: {home}-{away} | odd {chosen_odd}")

            time.sleep(0.2)  # чтобы не спамить API слишком резко
        except Exception as e:
            log.exception("scan_day item error")

    return total_signals

# ====== Отчёты ======
def fetch_fixture(fid):
    resp = api_get("fixtures", {"id": fid})
    return resp[0] if resp else None

def settle_and_summary(picks):
    """Возвращает (played, win, lose, open_, profit, lines[])"""
    played = win = lose = open_ = 0
    profit = 0.0
    lines = []
    for p in picks:
        fx = fetch_fixture(p["fixture_id"])
        if not fx:
            lines.append(f"{p['home']} — {p['away']} | нет данных")
            continue
        done, iswin, gh, ga = settle_pick(fx)
        if not done:
            open_ += 1
            lines.append(f"{p['home']} {gh}-{ga} {p['away']} | ⏳ ещё идёт")
            continue
        played += 1
        if iswin:
            win += 1
            profit += STAKE * (p.get("odd", 1.0) - 1.0)
            lines.append(f"{p['home']} {gh}-{ga} {p['away']} | ✅ +{STAKE*(p.get('odd',1.0)-1.0):.2f}")
        else:
            lose += 1
            profit -= STAKE
            lines.append(f"{p['home']} {gh}-{ga} {p['away']} | ❌ -{STAKE:.2f}")
        time.sleep(0.2)
    return played, win, lose, open_, profit, lines

def report_day(d: date):
    state = load_state()
    picks = list_picks_between(state, d, d)
    played, win, lose, open_, profit, lines = settle_and_summary(picks)
    msg = [
        "📊 *Отчёт за день*",
        f"Дата: {d.isoformat()}",
        f"Ставок: {len(picks)}, Сыграло: {win}, Не сыграло: {lose}, Открыто: {open_}",
        f"Профит (ставка={STAKE}): *{profit:+.2f}*",
        "───────────────",
    ]
    if lines:
        msg.extend(lines[:40])   # чтобы не перегружать
    else:
        msg.append("За сегодня сигналов не было.")
    send("\n".join(msg))

def report_week(d: date):
    # неделя: с пон-по вск включительно
    start = d - timedelta(days=d.weekday())  # понедельник
    end = start + timedelta(days=6)
    state = load_state()
    picks = list_picks_between(state, start, end)
    played, win, lose, open_, profit, lines = settle_and_summary(picks)
    msg = [
        "📊 *Недельная сводка*",
        f"Период: {start.isoformat()} — {end.isoformat()}",
        f"Ставок: {len(picks)}, Сыграло: {win}, Не сыграло: {lose}, Открыто: {open_}",
        f"Профит (ставка={STAKE}): *{profit:+.2f}*",
    ]
    send("\n".join(msg))

def report_month(d: date):
    year, month = d.year, d.month
    last_day = calendar.monthrange(year, month)[1]
    start = date(year, month, 1)
    end = date(year, month, last_day)
    state = load_state()
    picks = list_picks_between(state, start, end)
    played, win, lose, open_, profit, lines = settle_and_summary(picks)
    msg = [
        "📊 *Месячная сводка*",
        f"Период: {start.isoformat()} — {end.isoformat()}",
        f"Ставок: {len(picks)}, Сыграло: {win}, Не сыграло: {lose}, Открыто: {open_}",
        f"Профит (ставка={STAKE}): *{profit:+.2f}*",
    ]
    send("\n".join(msg))

# ====== Планировщик ======
def is_sunday(d: date) -> bool:
    return d.weekday() == 6

def is_last_day_of_month(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]

def main_loop():
    last_scan_date = None
    last_daily_date = None
    last_weekly = None  # (year, week)
    last_month = None   # (year, month)

    # приветствие
    send("🚀 Бот запущен (предматч, Render-ready). ❤️")
    send("ℹ️ График: скан в 08:00; отчёт 23:30; неделя — вс 23:50; месяц — в последний день 23:50.")

    while True:
        try:
            now = now_local()
            d = now.date()

            # Скан в 08:00  (один раз в день)
            if (now.hour, now.minute) == (SCAN_HR, SCAN_MIN) and last_scan_date != d:
                cnt = scan_day(d)
                send(f"✅ Скан на {d.isoformat()} завершён. Найдено сигналов: *{cnt}*.")
                last_scan_date = d

            # Дневной отчёт 23:30
            if (now.hour, now.minute) == (DAILY_HR, DAILY_MIN) and last_daily_date != d:
                report_day(d)
                last_daily_date = d

            # Недельный — по воскресеньям 23:50
            year, week, _ = now.isocalendar()
            if is_sunday(d) and (now.hour, now.minute) == (WEEKLY_HR, WEEKLY_MIN):
                if last_weekly != (year, week):
                    report_week(d)
                    last_weekly = (year, week)

            # Месячный — в последний день 23:50
            if is_last_day_of_month(d) and (now.hour, now.minute) == (MONTHLY_HR, MONTHLY_MIN):
                ym = (d.year, d.month)
                if last_month != ym:
                    report_month(d)
                    last_month = ym

            time.sleep(1)
        except Exception as e:
            log.exception("main_loop error")
            time.sleep(5)

# ====== RUN ======
if __name__ == "__main__":
    # web-keepalive для Render
    Thread(target=run_http, daemon=True).start()
    # телеграм-поллинг
    Thread(target=telebot_polling, daemon=True).start()
    # основной цикл
    main_loop()
