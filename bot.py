# -*- coding: utf-8 -*-
"""
Предматч-сканер (API-FOOTBALL):
 - Ежедневный скан в 08:00 по TZ (Europe/Warsaw по умолчанию).
 - Условия сигнала:
    1) H2H: в последних 3 очных матчах >=2 были TB2.5
    2) Форма: у каждой команды в последних 2 играх хотя бы 1 матч TB2.5
    3) Коэффициент TB2.5: 1.29 <= k <= 2.00 (берём максимум по букмекерам)
 - В 23:30 дневной отчёт, в вс 23:50 недельный, в последний день месяца 23:50 — месячный.
 - Для Render поднимаем HTTP (здоровье) на PORT.
"""

import os, sys, time, json, logging
from datetime import datetime, date, timedelta
import pytz, requests, telebot

# --- Render: HTTP health (не убираем) ---
from threading import Thread
from flask import Flask
app = Flask(__name__)
@app.get("/")
def health(): return "ok"
def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# === Секреты/настройки ===
API_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
API_KEY    = os.getenv("API_FOOTBALL_KEY")
TIMEZONE   = os.getenv("TZ", "Europe/Warsaw")

if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

CHAT_ID = int(CHAT_ID)

# Порог коэффициента TB2.5
ODDS_MIN = 1.29
ODDS_MAX = 2.00

# H2H: сколько последних брать и сколько из них должны быть TB2.5
H2H_LAST = 3
H2H_REQUIRE_TB = 2

# Сколько последних матчей формы каждой команды анализируем
FORM_LAST = 2
FORM_REQUIRE_TB = 1   # «в последних 2 — хотя бы 1 TB2.5»

# График
SCAN_HH, SCAN_MM = 8, 0     # 08:00
DAILY_HH, DAILY_MM = 23, 30
WEEKLY_HH, WEEKLY_MM = 23, 50
MONTHLY_HH, MONTHLY_MM = 23, 50

# Файлы
LOG_FILE = "bot.log"
STATE_FILE = "signals.json"

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("prematch")

bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 25

# Состояние
state = {
    "planned": [],     # [{fixture_id, when_iso, league, home, away, odds, reason}]
    "history": []      # записи о результатах для отчётов
}

# --- Утилиты времени/состояния ---
def now_local():
    return datetime.now(pytz.timezone(TIMEZONE))

def today_str():
    return now_local().strftime("%Y-%m-%d")

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            log.error(f"load_state: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_state: {e}")

def send(msg):
    try:
        bot.send_message(CHAT_ID, msg)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# --- Вызовы API-FOOTBALL ---
BASE = "https://v3.football.api-sports.io"

def api_get(path, params):
    try:
        r = API.get(BASE + path, params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        return j.get("response", []) or []
    except Exception as e:
        log.error(f"GET {path} {params} error: {e}")
        return []

def fixtures_today():
    return api_get("/fixtures", {"date": today_str(), "status": "NS"})

def h2h_total_goals(home_id, away_id, last_n=H2H_LAST):
    """Возвращает список totals по последним h2h матчам."""
    rows = api_get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": last_n})
    totals = []
    for m in rows:
        gh = m.get("goals", {}).get("home") or 0
        ga = m.get("goals", {}).get("away") or 0
        totals.append(gh+ga)
    return totals

def team_last_totals(team_id, n=FORM_LAST):
    rows = api_get("/fixtures", {"team": team_id, "last": n})
    totals = []
    for m in rows:
        gh = m.get("goals", {}).get("home") or 0
        ga = m.get("goals", {}).get("away") or 0
        totals.append(gh+ga)
    return totals

def get_odds_tb25(fixture_id):
    """
    Возвращает лучшую (максимальную) котировку на Over 2.5,
    если market=Over/Under и line="2.5" присутствуют.
    """
    rows = api_get("/odds", {"fixture": fixture_id})
    best = None
    for book in rows:
        bookmakers = book.get("bookmakers") or []
        for bm in bookmakers:
            bets = bm.get("bets") or []
            for bet in bets:
                if (bet.get("name") or "").lower().startswith("over/under"):
                    for v in bet.get("values") or []:
                        line = (v.get("value") or "").strip()
                        odd  = v.get("odd")
                        if line == "Over 2.5" and odd:
                            try:
                                k = float(odd)
                            except:
                                continue
                            if best is None or k > best:
                                best = k
    return best

def fixture_score(fixture_id):
    row = api_get("/fixtures", {"id": fixture_id})
    if not row: 
        return None, None
    m = row[0]
    st = m.get("fixture", {}).get("status", {}).get("short")
    gh = m.get("goals", {}).get("home") or 0
    ga = m.get("goals", {}).get("away") or 0
    return st, gh+ga

# --- Правила фильтра ---
def pass_h2h(home_id, away_id):
    totals = h2h_total_goals(home_id, away_id, H2H_LAST)
    tb = sum(1 for t in totals if t >= 3)
    return tb >= H2H_REQUIRE_TB, totals

def pass_form(team_id):
    totals = team_last_totals(team_id, FORM_LAST)
    tb = sum(1 for t in totals if t >= 3)
    return tb >= FORM_REQUIRE_TB, totals

def scan_day():
    """Сканирует сегодняшние матчи и формирует сигналы."""
    fixtures = fixtures_today()
    if not fixtures:
        send("ℹ️ Сегодня нет матчей для сканирования (или API пуст).")
        return

    cnt_ok = 0
    cnt_total = 0

    for f in fixtures:
        try:
            cnt_total += 1
            fixture_id = f.get("fixture", {}).get("id")
            league = f.get("league", {})
            leageline = f"{league.get('country','')} — {league.get('name','')}"
            teams = f.get("teams", {})
            home = teams.get("home", {}).get("name", "Home")
            away = teams.get("away", {}).get("name", "Away")
            hid  = teams.get("home", {}).get("id")
            aid  = teams.get("away", {}).get("id")

            # 1) H2H
            ok_h2h, h2h_totals = pass_h2h(hid, aid)
            if not ok_h2h:
                log.info(f"[{home}-{away}] skip: H2H totals={h2h_totals}")
                continue

            # 2) Форма
            ok_form_home, form_h = pass_form(hid)
            ok_form_away, form_a = pass_form(aid)
            if not (ok_form_home and ok_form_away):
                log.info(f"[{home}-{away}] skip: form H={form_h} A={form_a}")
                continue

            # 3) Коэффициенты TB2.5
            k = get_odds_tb25(fixture_id)
            if not k:
                log.info(f"[{home}-{away}] skip: нет котировки TB2.5 от API")
                continue
            if not (ODDS_MIN <= k <= ODDS_MAX):
                log.info(f"[{home}-{away}] skip: k={k} вне диапазона")
                continue

            # Если дошли сюда — сигнал ✅
            cnt_ok += 1
            dt_iso = f.get("fixture", {}).get("date")  # UTC ISO
            state["planned"].append({
                "fixture_id": fixture_id,
                "when_iso": dt_iso,
                "league": leageline,
                "home": home,
                "away": away,
                "odds": k,
                "h2h_totals": h2h_totals,
                "form_home": form_h,
                "form_away": form_a
            })
            save_state()

            msg = (
                "⚽ <b>СИГНАЛ (TB2.5)</b>\n"
                f"🏆 {leageline}\n"
                f"{home} — {away}\n"
                f"🕒 {dt_iso}\n"
                f"📊 H2H: {h2h_totals} (треб: ≥{H2H_REQUIRE_TB} из {H2H_LAST} с TB2.5)\n"
                f"📈 Форма: {home} {form_h}, {away} {form_a}\n"
                f"💸 TB2.5: <b>{k:.2f}</b>\n"
                "─────────────"
            )
            send(msg)

        except Exception as e:
            log.error(f"scan_day item: {e}")

    send(f"✅ Скан завершён. Найдено сигналов: <b>{cnt_ok}</b> из {cnt_total} матчей.")

def daily_report():
    """Проверяем все planned, формируем дневной отчёт, чистим список."""
    if not state["planned"]:
        send("📊 Отчёт за день\nСегодня сигналов не было.")
        return

    won = lost = 0
    lines = ["📊 <b>Отчёт за день</b>"]

    for p in state["planned"]:
        fid = p["fixture_id"]; home=p["home"]; away=p["away"]
        st, total = fixture_score(fid)
        if st == "FT":
            if total >= 3:
                won += 1
                lines.append(f"✅ {home} — {away} | {total} | TB2.5 OK | k={p['odds']:.2f}")
                state["history"].append({"fid":fid,"res":"W","odds":p["odds"],"when":today_str()})
            else:
                lost += 1
                lines.append(f"❌ {home} — {away} | {total} | TB2.5 fail | k={p['odds']:.2f}")
                state["history"].append({"fid":fid,"res":"L","odds":p["odds"],"when":today_str()})
        else:
            # матч ещё не завершён — переносим на завтра
            lines.append(f"⏳ {home} — {away} | статус {st} | переносим проверку")
            # возвращаем обратно, не удаляем
            continue

    # удаляем из planned только те, что FT (история уже записана)
    new_planned = []
    for p in state["planned"]:
        st, _ = fixture_score(p["fixture_id"])
        if st != "FT":
            new_planned.append(p)
    state["planned"] = new_planned
    save_state()

    lines.append("─────────────")
    lines.append(f"Итого: {won} ✅ / {lost} ❌")
    send("\n".join(lines))

def weekly_report():
    """Отчёт по истории последних 7 дней."""
    cutoff = date.today() - timedelta(days=7)
    items = [h for h in state["history"]
             if datetime.fromisoformat(h["when"]) .date() >= cutoff]
    if not items:
        send("📅 Недельная сводка: нет данных.")
        return
    w = sum(1 for x in items if x["res"]=="W")
    l = sum(1 for x in items if x["res"]=="L")
    send(f"📅 <b>Недельная сводка</b>\nЗа 7 дней: {w} ✅ / {l} ❌")

def monthly_report():
    """Отчёт по истории за текущий месяц."""
    today = date.today()
    month_items = [h for h in state["history"]
                   if datetime.fromisoformat(h["when"]).date().month == today.month]
    if not month_items:
        send("🗓 Месячная сводка: нет данных.")
        return
    w = sum(1 for x in month_items if x["res"]=="W")
    l = sum(1 for x in month_items if x["res"]=="L")
    send(f"🗓 <b>Месячная сводка</b>\nЗа месяц: {w} ✅ / {l} ❌")

# --- Флаги, чтобы не дублировать рассылки ---
sent_flags = {"scan": None, "daily": None, "weekly": None, "monthly": None}

def tick_scheduler():
    """Ежеминутный таймер: запускает задачи по времени."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    # 08:00 — скан
    if now.hour==SCAN_HH and now.minute==SCAN_MM:
        k = now.strftime("%Y-%m-%d:%H%M")
        if sent_flags["scan"] != k:
            sent_flags["scan"] = k
            scan_day()

    # 23:30 — дневной отчёт
    if now.hour==DAILY_HH and now.minute==DAILY_MM:
        k = now.strftime("%Y-%m-%d:%H%M")
        if sent_flags["daily"] != k:
            sent_flags["daily"] = k
            daily_report()

    # вс 23:50 — недельный
    if now.weekday()==6 and now.hour==WEEKLY_HH and now.minute==WEEKLY_MM:
        k = now.strftime("%Y-%m-%d:%H%M")
        if sent_flags["weekly"] != k:
            sent_flags["weekly"] = k
            weekly_report()

    # последний день месяца 23:50 — месячный
    tomorrow = now.date() + timedelta(days=1)
    last_day = (tomorrow.day == 1)  # значит сегодня — последний день месяца
    if last_day and now.hour==MONTHLY_HH and now.minute==MONTHLY_MM:
        k = now.strftime("%Y-%m-%d:%H%M")
        if sent_flags["monthly"] != k:
            sent_flags["monthly"] = k
            monthly_report()

# === RUN ===
if __name__ == "__main__":
    Thread(target=run_http, daemon=True).start()
    load_state()

    send("🚀 Бот запущен (предматч, Render-ready).")
    send(f"ℹ️ График: скан в {SCAN_HH:02d}:{SCAN_MM:02d}; отчёт {DAILY_HH:02d}:{DAILY_MM:02d}; "
         f"неделя — вс {WEEKLY_HH:02d}:{WEEKLY_MM:02d}; месяц — в последний день {MONTHLY_HH:02d}:{MONTHLY_MM:02d}.")

    # главный петлевой планировщик
    while True:
        try:
            tick_scheduler()
            time.sleep(60)   # ежеминутно достаточно
        except Exception as e:
            log.error(f"main loop: {e}")
            time.sleep(60)
