# -*- coding: utf-8 -*-
"""
Bot: First-Half Over 0.5 (FlashScore)
- Render-ready (Flask healthcheck)
- /scan (manual)
- Daily scan 08:00 Europe/Warsaw for pro leagues only
- Daily / Weekly / Monthly reports
- Favorite detection by 1x2 odds (favorite price <= 1.50)
- Filters: favorite avg scored >= 1.6 (last 5), underdog avg conceded >= 1.2 (last 5)

NOTE: FlashScore HTML can change. We use cloudscraper + BeautifulSoup and robust logging.
"""

import os, sys, json, time, logging, re
from datetime import datetime, timedelta, date
from threading import Thread

import pytz
import telebot
import cloudscraper
from bs4 import BeautifulSoup
from flask import Flask

# ========= ENV =========
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TIMEZONE  = os.getenv("TZ", "Europe/Warsaw")

if not API_TOKEN or not CHAT_ID:
    sys.exit("❌ Need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment.")
CHAT_ID = int(CHAT_ID)

# ========= CONFIG =========
LOG_FILE   = "bot.log"
STATE_FILE = "state.json"

# Strategy thresholds
FAV_MAX_PRICE        = 1.50   # favorite threshold by 1x2 odds
FAV_LAST_N           = 5
FAV_MIN_SCORED       = 1.6    # favorite avg scored >= 1.6
DOG_MIN_CONCEDED     = 1.2    # underdog avg conceded >= 1.2

# Schedule
SCAN_HH, SCAN_MM          = 8, 0
DAILY_RPT_HH, DAILY_RPT_MM = 23, 30
WEEKLY_RPT_HH, WEEKLY_RPT_MM = 23, 50
MONTHLY_RPT_HH, MONTHLY_RPT_MM = 23, 50

# Pro leagues whitelist (by league/country names visible on FlashScore)
# You can extend later; we match by substring lower().
PRO_LEAGUE_KEYWORDS = [
    # Big-5
    "england", "premier league", "championship",
    "germany", "bundesliga", "2. bundesliga",
    "spain", "la liga", "laliga", "primera division", "segunda division",
    "italy", "serie a", "serie b",
    "france", "ligue 1", "ligue 2",
    # Extended europe
    "netherlands", "eredivisie",
    "portugal", "primeira liga", "liga portugal",
    "turkey", "super lig",
    "belgium", "pro league",
    "czech", "czech republic", "1. liga",
    "switzerland", "super league",
    "austria", "bundesliga",
    "scotland", "premiership",
    "denmark", "superliga",
    "sweden", "allsvenskan",
    "norway", "eliteserien",
    "poland", "ekstraklasa",
    # add more if needed
]

# ========= LOG =========
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("fh-over05-bot")

# ========= TELEGRAM =========
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

def send(msg: str):
    try:
        bot.send_message(CHAT_ID, msg)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

@bot.message_handler(commands=["scan"])
def cmd_scan(_m):
    try:
        send("⏳ Ручной скан запущен…")
        picks = scan_today()
        store_picks(date_today(), picks)
        notify_picks(picks, title="Сигналы (ручной)")
    except Exception as e:
        log.exception("scan command error")
        send(f"❌ Ошибка: {e}")

def telebot_loop():
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            log.error(f"telebot error: {e}")
            time.sleep(5)

# ========= Render Flask =========
app = Flask(__name__)

@app.get("/")
def health():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ========= TIME/STATE =========
def tz_now():
    return datetime.now(pytz.timezone(TIMEZONE))

def date_today() -> date:
    return tz_now().date()

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"picks": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"picks": {}}

def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_state error: {e}")

def store_picks(d: date, picks: list[dict]):
    st = load_state()
    key = d.isoformat()
    st["picks"].setdefault(key, [])
    # avoid duplicates by fixture_url
    known = {p.get("fixture_url") for p in st["picks"][key]}
    for p in picks:
        if p.get("fixture_url") not in known:
            st["picks"][key].append(p)
    save_state(st)

def list_picks_between(d1: date, d2: date):
    st = load_state()
    out = []
    cur = d1
    while cur <= d2:
        out.extend(st["picks"].get(cur.isoformat(), []))
        cur += timedelta(days=1)
    return out

# ========= FlashScore client =========
class Flashscore:
    BASE = "https://www.flashscore.com"
    FOOTBALL_TODAY = "https://www.flashscore.com/football/"

    def __init__(self):
        self.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                          " AppleWebKit/537.36 (KHTML, like Gecko)"
                          " Chrome/125.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def get(self, url):
        r = self.scraper.get(url, headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.text

    def today_fixtures(self):
        """
        Parse today's football fixtures list from /football/.
        We look for match blocks with league names; FlashScore uses dynamic content,
        so we rely on what's rendered server-side (fallback).
        """
        html = self.get(self.FOOTBALL_TODAY)
        soup = BeautifulSoup(html, "html.parser")

        # Collect blocks: league headers and matches beneath.
        fixtures = []
        # League headers (could be <div class="event__title--type"> or similar)
        # We'll search sections where league/country text appears, then match rows inside.
        # Generic approach: rows with data-id or 'event__match'
        for league_block in soup.select("div.league-header, div.event__title, div.event__header, div.leagueHeader, div.Scores-h2"):
            league_text = league_block.get_text(separator=" ", strip=True).lower()
            if not self._is_pro_league(league_text):
                continue
            # find following siblings that contain matches until next league block
            # This is heuristic due to HTML variability
            nxt = league_block.find_next_sibling()
            while nxt and nxt.name == "div" and ("event__match" in (nxt.get("class") or []) or "match-row" in (nxt.get("class") or [])):
                try:
                    # team names
                    home = nxt.select_one(".event__participant--home, .home, .participant--home")
                    away = nxt.select_one(".event__participant--away, .away, .participant--away")
                    if not home or not away:
                        break
                    home_name = home.get_text(strip=True)
                    away_name = away.get_text(strip=True)
                    # link to match (fixture URL)
                    link = nxt.select_one("a")
                    href = link.get("href") if link else None
                    if href and href.startswith("/match/"):
                        fixture_url = self.BASE + href
                    else:
                        # Try alternative anchor structure
                        fixture_url = None
                        for a in nxt.find_all("a", href=True):
                            if a["href"].startswith("/match/"):
                                fixture_url = self.BASE + a["href"]
                                break
                    # team page links if present
                    home_url = None
                    away_url = None
                    for a in nxt.find_all("a", href=True):
                        h = a["href"]
                        if "/team/" in h and home_name.lower() in a.get_text(" ", strip=True).lower():
                            home_url = self.BASE + h
                        if "/team/" in h and away_name.lower() in a.get_text(" ", strip=True).lower():
                            away_url = self.BASE + h

                    fixtures.append({
                        "league_text": league_text,
                        "home": home_name, "away": away_name,
                        "fixture_url": fixture_url,
                        "home_url": home_url, "away_url": away_url
                    })
                except Exception as e:
                    log.info(f"parse match row issue: {e}")
                nxt = nxt.find_next_sibling()
        # Fallback #2: generic rows
        if not fixtures:
            for row in soup.select("div.event__match"):
                try:
                    league_el = row.find_previous("div", class_="event__title")
                    league_text = (league_el.get_text(" ", strip=True).lower() if league_el else "")
                    if not self._is_pro_league(league_text):
                        continue
                    home = row.select_one(".event__participant--home")
                    away = row.select_one(".event__participant--away")
                    if not home or not away:
                        continue
                    home_name = home.get_text(strip=True)
                    away_name = away.get_text(strip=True)
                    link = row.select_one("a")
                    href = link.get("href") if link else None
                    fixture_url = self.BASE + href if (href and href.startswith("/match/")) else None
                    fixtures.append({
                        "league_text": league_text,
                        "home": home_name, "away": away_name,
                        "fixture_url": fixture_url,
                        "home_url": None, "away_url": None
                    })
                except Exception:
                    pass

        return fixtures

    def team_last_matches(self, team_url, n=5):
        """
        Parse a team page to get last n finished results (goals for/against).
        We try '/results/' subpage if exists.
        """
        if not team_url:
            return []

        # Prefer results subpage if obvious
        results_url = team_url
        if not results_url.endswith("/results/"):
            if results_url.endswith("/"):
                results_url = results_url + "results/"
            else:
                results_url = results_url + "/results/"

        html = self.get(results_url)
        soup = BeautifulSoup(html, "html.parser")

        matches = []
        # rows might be 'event__match event__match--static event__match--last'
        # We look for finished matches with score
        for row in soup.select("div.event__match"):
            try:
                st = row.get("class") or []
                # If status shows finished or has score
                score_el = row.select_one(".event__scores, .event__score")
                if not score_el:
                    continue
                # parse numbers like "2:1"
                score_txt = score_el.get_text(" ", strip=True)
                m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", score_txt)
                if not m:
                    continue
                gh, ga = int(m.group(1)), int(m.group(2))
                matches.append((gh, ga))
                if len(matches) >= n:
                    break
            except Exception:
                continue
        return matches

    def match_odds_1x2(self, fixture_url):
        """
        Try to extract 1x2 odds from match page (if present in static HTML).
        Returns tuple (fav_side: 'home'/'away', fav_price: float) or (None, None)
        """
        if not fixture_url:
            return (None, None)
        try:
            html = self.get(fixture_url)
            soup = BeautifulSoup(html, "html.parser")

            # Look for simple odds table with 1, X, 2 values
            # This is heuristic: try values like data-odd or text cells
            # We'll gather numeric odds in form [(label, price)]
            odds = []
            for el in soup.find_all(["a", "div", "span"]):
                txt = el.get_text(" ", strip=True).lower()
                if txt in ("1", "home") or "home win" in txt:
                    price = _extract_float(el)
                    if price:
                        odds.append(("home", price))
                elif txt in ("2", "away") or "away win" in txt:
                    price = _extract_float(el)
                    if price:
                        odds.append(("away", price))
            # pick min price as favorite
            fav_side, fav_price = None, None
            for side, price in odds:
                if price and (fav_price is None or price < fav_price):
                    fav_side, fav_price = side, price
            # basic sanity
            if fav_price and (1.01 <= fav_price <= 10.0):
                return fav_side, float(fav_price)
            return (None, None)
        except Exception as e:
            log.info(f"odds parse fail: {e}")
            return (None, None)

    def _is_pro_league(self, league_text_lower: str) -> bool:
        if not league_text_lower:
            return False
        for kw in PRO_LEAGUE_KEYWORDS:
            if kw in league_text_lower:
                return True
        return False

def _extract_float(el):
    # helper to find first float-looking number inside element
    m = re.search(r"\d+\.\d+|\d+", el.get_text(" ", strip=True))
    if m:
        try:
            return float(m.group(0))
        except:
            return None
    return None

FS = Flashscore()

# ========= STRATEGY LOGIC =========
def average_scored(matches: list[tuple[int,int]]) -> float:
    if not matches:
        return 0.0
    # assume provided list are matches of the team FROM its perspective:
    # but we don't know which side is which; we just average gh
    # On team page results, order usually "team score : opponent score"
    return sum(gh for gh, _ in matches) / len(matches)

def average_conceded(matches: list[tuple[int,int]]) -> float:
    if not matches:
        return 0.0
    return sum(ga for _, ga in matches) / len(matches)

def pass_filters_fh_over05(fixture: dict) -> tuple[bool, dict]:
    """
    fixture: {league_text, home, away, fixture_url, home_url, away_url}
    Returns (ok, info_dict)
    """
    # Determine favorite by 1x2 if possible
    fav_side, fav_price = FS.match_odds_1x2(fixture["fixture_url"])
    if not fav_side or not fav_price:
        log.info(f"[skip] odds not found: {fixture['home']} vs {fixture['away']}")
        return False, {"reason": "no_odds"}

    if fav_price > FAV_MAX_PRICE:
        return False, {"reason": f"fav_price {fav_price:.2f} > {FAV_MAX_PRICE:.2f}"}

    # Collect last results (last 5)
    home_last = FS.team_last_matches(fixture.get("home_url"), n=FAV_LAST_N)
    away_last = FS.team_last_matches(fixture.get("away_url"), n=FAV_LAST_N)

    if len(home_last) < 2 or len(away_last) < 2:
        return False, {"reason": "not_enough_history"}

    # Map favorites to team stats
    if fav_side == "home":
        fav_last = home_last
        dog_last = away_last
        fav_name = fixture["home"]
        dog_name = fixture["away"]
    else:
        fav_last = away_last
        dog_last = home_last
        fav_name = fixture["away"]
        dog_name = fixture["home"]

    fav_scored = average_scored(fav_last)
    dog_conceded = average_conceded(dog_last)

    if fav_scored < FAV_MIN_SCORED:
        return False, {"reason": f"fav_scored {fav_scored:.2f} < {FAV_MIN_SCORED:.2f}"}
    if dog_conceded < DOG_MIN_CONCEDED:
        return False, {"reason": f"dog_conceded {dog_conceded:.2f} < {DOG_MIN_CONCEDED:.2f}"}

    info = {
        "fav_side": fav_side, "fav_price": fav_price,
        "fav_name": fav_name, "dog_name": dog_name,
        "fav_scored": fav_scored, "dog_conceded": dog_conceded
    }
    return True, info

# ========= SCAN / REPORTS =========
def scan_today():
    picks = []
    fixtures = FS.today_fixtures()
    log.info(f"fixtures found: {len(fixtures)}")

    for fx in fixtures:
        try:
            ok, info = pass_filters_fh_over05(fx)
            if ok:
                picks.append({**fx, **info})
            else:
                log.info(f"skip {fx['home']} vs {fx['away']}: {info.get('reason')}")
            time.sleep(0.2)  # be gentle
        except Exception as e:
            log.exception("scan item error")
            continue
    return picks

def notify_picks(picks: list[dict], title="Сигналы (ежедневный скан)"):
    if not picks:
        send("ℹ️ Подходящих матчей не найдено.")
        return
    for p in picks:
        when_txt = "сегодня"
        msg = (
            f"🟢 *{title}*\n"
            f"🏆 {p.get('league_text','').title()}\n"
            f"{p['home']} — {p['away']}\n"
            f"💹 Фаворит: *{p['fav_name']}* (кэф ~ {p['fav_price']:.2f})\n"
            f"📊 Ср. забитых фав.: {p['fav_scored']:.2f} | ср. пропущенных сопер.: {p['dog_conceded']:.2f}\n"
            f"🎯 Рынок: ТБ 0.5 (1-й тайм)\n"
            f"🔗 Матч: {p.get('fixture_url','')}\n"
            "──────────────"
        )
        send(msg)

def daily_report():
    # Simple: count how many signals were sent today
    st = load_state()
    key = date_today().isoformat()
    cnt = len(st["picks"].get(key, []))
    send(f"📊 *Отчёт за день*\nДата: {key}\nСигналов отправлено: *{cnt}*")

def weekly_report():
    today = date_today()
    start = today - timedelta(days=6)
    picks = list_picks_between(start, today)
    send(f"🗓 *Недельная сводка*\nПериод: {start} — {today}\nСигналов: *{len(picks)}*")

def monthly_report():
    today = date_today()
    start = today.replace(day=1)
    picks = list_picks_between(start, today)
    send(f"📅 *Месячная сводка*\nПериод: {start} — {today}\nСигналов: *{len(picks)}*")

# ========= SCHEDULER =========
def scheduler_loop():
    send("🚀 Бот активен. Скан 08:00; отчёт 23:30; неделя вс 23:50; месяц в посл. день 23:50.")
    last_scan_key = last_daily_key = last_weekly_key = last_monthly_key = ""

    while True:
        try:
            now = tz_now()
            dkey = now.date().isoformat()
            wd   = now.weekday()  # 6 = Sunday
            last_day_month = (now + timedelta(days=1)).day == 1

            # Daily scan 08:00
            if now.hour == SCAN_HH and now.minute == SCAN_MM and last_scan_key != dkey:
                picks = scan_today()
                store_picks(now.date(), picks)
                notify_picks(picks)
                last_scan_key = dkey

            # Daily report 23:30
            if now.hour == DAILY_RPT_HH and now.minute == DAILY_RPT_MM and last_daily_key != dkey:
                daily_report()
                last_daily_key = dkey

            # Weekly report Sun 23:50
            if wd == 6 and now.hour == WEEKLY_RPT_HH and now.minute == WEEKLY_RPT_MM and last_weekly_key != dkey:
                weekly_report()
                last_weekly_key = dkey

            # Monthly report last day 23:50
            if last_day_month and now.hour == MONTHLY_RPT_HH and now.minute == MONTHLY_RPT_MM and last_monthly_key != dkey:
                monthly_report()
                last_monthly_key = dkey

        except Exception as e:
            log.error(f"scheduler error: {e}")

        time.sleep(20)

# ========= MAIN =========
if __name__ == "__main__":
    # start Flask (Render health)
    Thread(target=run_http, daemon=True).start()
    # start Telegram polling in background (for /scan)
    Thread(target=telebot_loop, daemon=True).start()

    # startup scan (one-shot)
    send("🤖 Бот запущен. Делаю стартовый скан…")
    try:
        picks_now = scan_today()
        store_picks(date_today(), picks_now)
        notify_picks(picks_now, title="Сигналы (стартовый прогон)")
    except Exception as e:
        log.exception("startup scan failed")
        send(f"⚠️ Стартовый скан не удался: {e}")

    # schedule forever
    scheduler_loop()
