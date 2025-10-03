# -*- coding: utf-8 -*-
"""
Pre-match signals bot for Render (sleep-resilient) + Telegram commands:
Стратегия сигнала:
  • Последний очный матч команд — ТБ2.5 (≥3 гола)
  • Два последних матча КАЖДОЙ команды — ТБ2.5 (оба)
  • (опционально) кэф на ТБ2.5 в диапазоне [ODDS_MIN, ODDS_MAX]

Расписание (Europe/Warsaw):
  • Скан ≥ 08:00 — 1 раз/день (если сервис проспал — выполнит при пробуждении)
  • Дневной отчёт ≥ 23:30 — 1 раз/день
  • Недельный отчёт (вс) ≥ 23:50 — 1 раз/неделю
  • Месячный отчёт (последний день) ≥ 23:50 — 1 раз/месяц

Команды:
  /scan /report /weekly /monthly /status /help
"""

import os, json, time, logging
from datetime import datetime, timedelta, date
from threading import Thread, Lock

import pytz
import requests
import telebot
from flask import Flask

# ========= ENV / SETTINGS =========
API_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID")
API_KEY     = os.getenv("API_FOOTBALL_KEY")
TIMEZONE    = os.getenv("TZ", "Europe/Warsaw")

# Порог коэффициента (опционально). Не задавайте — фильтр отключён.
def _f(name):
    v = os.getenv(name, "")
    try:
        return float(v) if v else None
    except: return None

ODDS_MIN = _f("ODDS_MIN")   # например 1.29
ODDS_MAX = _f("ODDS_MAX")   # например 2.00

STORAGE_FILE = "signals.json"
LOG_FILE     = "bot.log"
REQUEST_TIMEOUT = 15

if not API_TOKEN or not CHAT_ID_RAW or not API_KEY:
    raise SystemExit("❌ Need TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

try: CHAT_ID = int(CHAT_ID_RAW)
except: CHAT_ID = CHAT_ID_RAW

# ========= LOGGING =========
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("prematch-bot")

# ========= TELEGRAM & HTTP =========
bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")
API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})

# Tiny HTTP for Render
app = Flask(__name__)
@app.get("/")
def healthcheck(): return "ok"
def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ========= HELPERS =========
def tz(): return pytz.timezone(TIMEZONE)
def now_local(): return datetime.now(tz())

def send(msg: str):
    try: bot.send_message(CHAT_ID, msg)
    except Exception as e: log.error(f"Telegram send error: {e}")

def load_store():
    if not os.path.exists(STORAGE_FILE):
        return {"meta":{
                    "last_scan_date":None,
                    "last_daily_report_date":None,
                    "last_weekly_yrwk":None,
                    "last_monthly_yrmo":None
                },
                "days":{}}
    try:
        with open(STORAGE_FILE,"r",encoding="utf-8") as f: d=json.load(f)
        d.setdefault("meta",{})
        d.setdefault("days",{})
        return d
    except Exception as e:
        log.error(f"load_store error: {e}")
        return {"meta":{},"days":{}}

def save_store(d):
    try:
        with open(STORAGE_FILE,"w",encoding="utf-8") as f:
            json.dump(d,f,ensure_ascii=False,indent=2)
    except Exception as e: log.error(f"save_store error: {e}")

def api_get(url, params=None):
    try:
        r = API.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"GET {url} error: {e}")
        return {}

def total_from_fixture(m):
    try:
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return gh, ga, gh+ga
    except: return 0,0,0

def finished(short): return short in ("FT","AET","PEN")
def is_tb25(total): return total is not None and total >= 3

# ========= STRATEGY CHECKS =========
def last_h2h_is_tb25(home_id, away_id):
    data = api_get("https://v3.football.api-sports.io/fixtures/headtohead",
                   {"h2h": f"{home_id}-{away_id}", "last": 1})
    resp = data.get("response") or []
    if not resp: return False
    _,_,tot = total_from_fixture(resp[0])
    return is_tb25(tot)

def team_last2_all_tb25(team_id):
    data = api_get("https://v3.football.api-sports.io/fixtures",
                   {"team": team_id, "last": 2})
    resp = data.get("response") or []
    if len(resp)<2: return False
    for m in resp:
        _,_,tot = total_from_fixture(m)
        if not is_tb25(tot): return False
    return True

def odds_over25(fixture_id):
    data = api_get("https://v3.football.api-sports.io/odds", {"fixture": fixture_id})
    resp = data.get("response") or []
    for item in resp:
        for book in item.get("bookmakers", []):
            for bet in book.get("bets", []):
                name = (bet.get("name") or "").lower()
                if "over/under" in name:
                    for v in bet.get("values", []):
                        ln = (v.get("value") or "").replace(" ", "").lower()
                        if ln in ("over2.5","2.5"):
                            try: return float(v.get("odd"))
                            except: pass
    return None

# ========= DAILY SCAN =========
def scan_today():
    today = now_local().strftime("%Y-%m-%d")
    send(f"🛰️ Старт скана ({today}).")
    fixtures = (api_get("https://v3.football.api-sports.io/fixtures",
                        {"date": today}).get("response") or [])
    store = load_store()
    day = store["days"].setdefault(today, [])
    sent = 0

    for m in fixtures:
        try:
            if m["fixture"]["status"]["short"] != "NS": continue
            fid = m["fixture"]["id"]
            home_id = m["teams"]["home"]["id"]; away_id = m["teams"]["away"]["id"]
            home = m["teams"]["home"]["name"];   away = m["teams"]["away"]["name"]
            league = f"{m['league']['country']} — {m['league']['name']}"

            if any(x.get("fixture_id")==fid for x in day): continue

            if not last_h2h_is_tb25(home_id, away_id): continue
            if not team_last2_all_tb25(home_id): continue
            if not team_last2_all_tb25(away_id): continue

            odd = odds_over25(fid)
            if ODDS_MIN is not None and (odd is None or odd < ODDS_MIN): continue
            if ODDS_MAX is not None and (odd is None or odd > ODDS_MAX): continue

            day.append({
                "fixture_id": fid,
                "home": home, "away": away,
                "league": league,
                "odds": odd,
                "date": today,
                "final_total": None,
                "result": None
            })
            sent += 1

        except Exception as e:
            log.error(f"scan item error: {e}")

    save_store(store)

    if sent==0:
        send("ℹ️ Подходящих матчей по стратегии сегодня не найдено.")
    else:
        lines = ["🔥 <b>Сигналы на сегодня</b>",
                 "<i>Стратегия: H2H=ТБ2.5 и у обеих 2/2 последние — ТБ2.5</i>"]
        for s in day:
            if s["date"]!=today: continue
            o = f"{s['odds']:.2f}" if isinstance(s["odds"],float) else "н/д"
            lines.append(f"• {s['league']}\n  {s['home']} — {s['away']}\n  Коэф ТБ2.5: <b>{o}</b>")
        send("\n".join(lines))

# ========= REPORTS =========
def resolve_fixture(fid):
    data = api_get("https://v3.football.api-sports.io/fixtures", {"id": fid})
    resp = data.get("response") or []
    if not resp: return None
    m = resp[0]
    st = m["fixture"]["status"]["short"]
    gh,ga,tot = total_from_fixture(m)
    return {"status":st,"gh":gh,"ga":ga,"total":tot}

def finalize(signals):
    changed=False
    for s in signals:
        if s.get("result") in ("WIN","LOSE"): continue
        res = resolve_fixture(s["fixture_id"])
        if not res: continue
        if finished(res["status"]):
            s["final_total"]=res["total"]
            s["result"]="WIN" if is_tb25(res["total"]) else "LOSE"
            changed=True
    return changed

def report_day():
    today = now_local().strftime("%Y-%m-%d")
    store = load_store()
    day = [x for x in store["days"].get(today,[])]

    if finalize(day):
        store["days"][today]=day
        save_store(store)

    if not day:
        send("📊 Отчёт за день\nСегодня сигналов не было.")
        return

    wins=sum(1 for s in day if s.get("result")=="WIN")
    loses=sum(1 for s in day if s.get("result")=="LOSE")
    pend =sum(1 for s in day if s.get("result") is None)
    lines=["📊 <b>Отчёт за день</b>",
           f"Сигналов: {len(day)} | ✅ {wins}  ❌ {loses}  ⏳ {pend}"]
    for i,s in enumerate(day,1):
        tail=""
        if s.get("result"):
            tail=f" | итог: {s.get('final_total','?')} ({'✅' if s['result']=='WIN' else '❌'})"
        lines.append(f"{i}. {s['home']}–{s['away']} | ТБ2.5 | кэф: {s.get('odds','н/д')}{tail}")
    send("\n".join(lines))

def report_week():
    store=load_store()
    today=now_local().date()
    start=today - timedelta(days=6)
    items=[]
    for d, arr in store["days"].items():
        try: dd=date.fromisoformat(d)
        except: continue
        if start<=dd<=today: items+=arr
    wins=sum(1 for s in items if s.get("result")=="WIN")
    loses=sum(1 for s in items if s.get("result")=="LOSE")
    lines=["🗓️ <b>Недельная сводка</b>",
           f"Период: {start} — {today}",
           f"Ставок: {wins+loses}, Вин: {wins}, Луз: {loses}"]
    send("\n".join(lines))

def report_month():
    store=load_store()
    today=now_local().date()
    first=today.replace(day=1)
    items=[]
    for d, arr in store["days"].items():
        try: dd=date.fromisoformat(d)
        except: continue
        if first<=dd<=today: items+=arr
    wins=sum(1 for s in items if s.get("result")=="WIN")
    loses=sum(1 for s in items if s.get("result")=="LOSE")
    lines=["📅 <b>Месячная сводка</b>",
           f"Период: {first} — {today}",
           f"Ставок: {wins+loses}, Вин: {wins}, Луз: {loses}"]
    send("\n".join(lines))

# ========= DUE CHECKERS (sleep-resilient) =========
def due_scan(now, meta):
    today=now.strftime("%Y-%m-%d")
    if meta.get("last_scan_date")==today: return False
    return now.hour>=8

def due_daily(now, meta):
    today=now.strftime("%Y-%m-%d")
    if meta.get("last_daily_report_date")==today: return False
    return (now.hour,now.minute)>=(23,30)

def due_weekly(now, meta):
    yrwk=f"{now.isocalendar().year}-{now.isocalendar().week:02d}"
    if meta.get("last_weekly_yrwk")==yrwk: return False
    return now.weekday()==6 and (now.hour,now.minute)>=(23,50)

def due_monthly(now, meta):
    yrmo=now.strftime("%Y-%m")
    if meta.get("last_monthly_yrmo")==yrmo: return False
    tomorrow=now.date()+timedelta(days=1)
    return (tomorrow.day==1) and (now.hour,now.minute)>=(23,50)

LOCK=Lock()

def scheduler_loop():
    send("🚀 Бот запущен. Команды: /scan /report /weekly /monthly /status /help")
    last_tick=None
    while True:
        try:
            now=now_local()
            tick=now.strftime("%Y-%m-%d %H:%M")
            if tick!=last_tick:
                last_tick=tick
                log.info("tick %s", tick)

            with LOCK:
                store=load_store()
                meta=store.get("meta",{})
                changed=False

                if due_scan(now, meta):
                    scan_today()
                    meta["last_scan_date"]=now.strftime("%Y-%m-%d"); changed=True

                if due_daily(now, meta):
                    report_day()
                    meta["last_daily_report_date"]=now.strftime("%Y-%m-%d"); changed=True

                if due_weekly(now, meta):
                    report_week()
                    meta["last_weekly_yrwk"]=f"{now.isocalendar().year}-{now.isocalendar().week:02d}"; changed=True

                if due_monthly(now, meta):
                    report_month()
                    meta["last_monthly_yrmo"]=now.strftime("%Y-%m"); changed=True

                if changed:
                    store["meta"]=meta
                    save_store(store)

            time.sleep(5)
        except Exception as e:
            log.error(f"scheduler error: {e}")
            time.sleep(5)

# ========= TELEGRAM COMMANDS =========
def owner_only(m): return str(m.chat.id)==str(CHAT_ID)

@bot.message_handler(commands=['help','start'])
def cmd_help(m):
    if not owner_only(m): return
    bot.reply_to(m,
        "Команды:\n"
        "/scan — запустить скан на сегодня\n"
        "/report — дневной отчёт\n"
        "/weekly — недельная сводка\n"
        "/monthly — месячная сводка\n"
        "/status — статус последних запусков\n"
    )

@bot.message_handler(commands=['status'])
def cmd_status(m):
    if not owner_only(m): return
    st=load_store().get("meta",{})
    bot.reply_to(m,
        "📌 Статус:\n"
        f"last_scan_date: {st.get('last_scan_date')}\n"
        f"last_daily_report_date: {st.get('last_daily_report_date')}\n"
        f"last_weekly_yrwk: {st.get('last_weekly_yrwk')}\n"
        f"last_monthly_yrmo: {st.get('last_monthly_yrmo')}\n"
    )

@bot.message_handler(commands=['scan'])
def cmd_scan(m):
    if not owner_only(m): return
    with LOCK:
        scan_today()
        st=load_store(); st["meta"]["last_scan_date"]=now_local().strftime("%Y-%m-%d"); save_store(st)
    bot.reply_to(m,"👌 Скан завершён.")

@bot.message_handler(commands=['report'])
def cmd_report(m):
    if not owner_only(m): return
    with LOCK:
        report_day()
        st=load_store(); st["meta"]["last_daily_report_date"]=now_local().strftime("%Y-%m-%d"); save_store(st)
    bot.reply_to(m,"👌 Дневной отчёт отправлен.")

@bot.message_handler(commands=['weekly'])
def cmd_weekly(m):
    if not owner_only(m): return
    with LOCK:
        report_week()
        st=load_store(); st["meta"]["last_weekly_yrwk"]=f"{now_local().isocalendar().year}-{now_local().isocalendar().week:02d}"
        save_store(st)
    bot.reply_to(m,"👌 Недельная сводка отправлена.")

@bot.message_handler(commands=['monthly'])
def cmd_monthly(m):
    if not owner_only(m): return
    with LOCK:
        report_month()
        st=load_store(); st["meta"]["last_monthly_yrmo"]=now_local().strftime("%Y-%m"); save_store(st)
    bot.reply_to(m,"👌 Месячная сводка отправлена.")

def tg_polling():
    while True:
        try: bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            log.error(f"polling error: {e}")
            time.sleep(3)

# ========= RUN =========
if __name__=="__main__":
    Thread(target=run_http, daemon=True).start()    # Render Web Service
    Thread(target=tg_polling, daemon=True).start()  # команды
    scheduler_loop()
