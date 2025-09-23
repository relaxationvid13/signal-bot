# -*- coding: utf-8 -*-
"""
Футбол-бот (эконом, Render-friendly):
- Ловим сигналы ТОЛЬКО до 20-й минуты:
    * если ровно 2 гола → ТБ(3), кэф ∈ [1.29; 2.00]
    * если ровно 3 гола → ТБ(4), кэф ∈ [1.29; 2.00]
- /fixtures?live=all — базово раз в 15 минут; если есть кандидаты (≤20' и 2–3 гола) —
  временно ускоряемся до 3 минут. После 20' матч НЕ проверяем.
- По каждой фикстуре /odds вызываем не чаще 1 раза в минуту (до 20').
- Сигнал отправляется ОДИН раз, только если кэф в диапазоне.
- Ежедневный отчёт (23:30–23:35 Europe/Warsaw) + недельная и месячная сводки.
- Ручные команды: /status, /report, /test_signal.
- Переживает перезапуски: сигналы дня в `signals_YYYY-MM-DD.json`, история исходов в `history.jsonl`.
"""

import os, sys, time, json, logging
from datetime import datetime, timedelta
from threading import Thread
from typing import Iterable, Tuple, Optional

import pytz
import requests
import telebot
from flask import Flask

# ================== СЕКРЕТЫ / НАСТРОЙКИ ==================
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API_KEY   = os.getenv("API_FOOTBALL_KEY")
if not API_TOKEN or not CHAT_ID or not API_KEY:
    sys.exit("❌ Нет переменных окружения: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / API_FOOTBALL_KEY")

CHAT_ID   = int(CHAT_ID)
TIMEZONE  = "Europe/Warsaw"        # польское время

# Опрос API (адаптивный)
BASE_POLL  = 15 * 60               # 15 минут, когда кандидатов нет
BOOST_POLL = 3  * 60               # 3 минуты, когда есть кандидаты до 20'
current_poll = BASE_POLL

WINDOW_MAX_MINUTE = 20             # работаем по матчам до/включая 20'

# Фильтр коэффициентов (включительно)
LOW_ODDS  = 1.29
HIGH_ODDS = 2.00

STAKE_UNITS = 1                    # условная ставка в отчётах (+1/-1)

LOG_FILE      = "bot.log"
DAY_FILE_TPL  = "signals_{day}.json"   # сигналы за день
HISTORY_FILE  = "history.jsonl"        # история рассчитанных исходов

# ================== ЛОГИ ==================
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("signals-bot")

# ================== FLASK (Render keep-alive) ==================
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ================== TELEGRAM ==================
bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

def run_telebot():
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)

# ================== API-FOOTBALL ==================
API = requests.Session()
API.headers.update({"x-apisports-key": API_KEY})
DEFAULT_TIMEOUT = 15

def get_live():
    try:
        r = API.get("https://v3.football.api-sports.io/fixtures?live=all", timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("fixtures HTTP %s %s", r.status_code, r.text[:200])
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
        st = m["fixture"]["status"]["short"]
        gh = m["goals"]["home"] or 0
        ga = m["goals"]["away"] or 0
        return st, gh, ga
    except Exception as e:
        log.error(f"get_fixture_result({fid}) error: {e}")
        return None

def get_over_odds_for_line(fixture_id: int, target_total: int) -> Tuple[Optional[float], Optional[str]]:
    """
    Возвращает (лучший_кэф, букмекер) для рынка Over/Under:
    ищем ставку "Over {target_total}" среди всех букмекеров.
    Если не нашли — (None, None).
    """
    try:
        r = API.get(f"https://v3.football.api-sports.io/odds?fixture={fixture_id}", timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            log.warning("odds HTTP %s %s", r.status_code, r.text[:200])
        r.raise_for_status()
        resp = r.json().get("response", []) or []

        best_odd = None
        best_book = None

        for item in resp:
            for bm in item.get("bookmakers", []) or []:
                bm_name = bm.get("name")
                for bet in bm.get("bets", []) or []:
                    name = (bet.get("name") or "").lower()
                    if "over" in name and "under" in name:  # "Over/Under"
                        for v in bet.get("values", []) or []:
                            # форматы могут быть разные:
                            # 1) value: "Over 3" | "Over 4"
                            # 2) value: "3" / "3.0" + label: "Over"
                            val = (v.get("value") or "").strip()
                            label = (v.get("label") or "").lower()
                            odd_raw = v.get("odd") or v.get("price")
                            try:
                                odd = float(str(odd_raw))
                            except Exception:
                                continue

                            ok = False
                            if val.lower() == f"over {target_total}":
                                ok = True
                            elif (val == str(target_total) or val == f"{target_total}.0") and "over" in label:
                                ok = True

                            if ok:
                                if best_odd is None or odd > best_odd:
                                    best_odd = odd
                                    best_book = bm_name
        return best_odd, best_book
    except Exception as e:
        log.error(f"get_over_odds_for_line({fixture_id},{target_total}) error: {e}")
        return None, None

# ================== ВРЕМЯ / УТИЛИТЫ ==================
def tz():
    return pytz.timezone(TIMEZONE)

def now_local() -> datetime:
    return datetime.now(tz())

def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")

def day_file(day: str | None = None) -> str:
    return DAY_FILE_TPL.format(day=(day or today_str()))

def send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ================== ДНЕВНЫЕ ФАЙЛЫ СИГНАЛОВ ==================
def load_day_signals(day: str | None = None) -> list[dict]:
    path = day_file(day)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error("load_day_signals error: %s", e)
        return []

def save_day_signals(arr: list[dict], day: str | None = None):
    path = day_file(day)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False)
    except Exception as e:
        log.error("save_day_signals error: %s", e)

def append_day_signal(rec: dict):
    day = today_str()
    arr = load_day_signals(day)
    if any(x.get("fixture_id") == rec.get("fixture_id") for x in arr):
        return
    arr.append(rec)
    save_day_signals(arr, day)

# ================== ИСТОРИЯ ИСХОДОВ ==================
def append_history(entry: dict):
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("append_history error: %s", e)

def read_history_iter() -> Iterable[dict]:
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

# ================== КЭШ ЛАЙВ-МАТЧЕЙ ==================
# fixture_id -> {"last_minute": int, "last_goals": int, "last_odds_check_minute": int}
live_cache: dict[int, dict] = {}
signaled_ids: set[int] = set()  # чтобы не слать дважды

def update_live_cache(fid: int, minute: int, total_goals: int):
    item = live_cache.get(fid, {"last_minute": -1, "last_goals": -1, "last_odds_check_minute": -1})
    changed = (item["last_minute"] != minute) or (item["last_goals"] != total_goals)
    item["last_minute"] = minute
    item["last_goals"] = total_goals
    live_cache[fid] = item
    return changed

def can_check_odds_now(fid: int, minute: int) -> bool:
    item = live_cache.get(fid, {})
    return int(item.get("last_odds_check_minute", -1)) != int(minute)

def mark_odds_checked(fid: int, minute: int):
    item = live_cache.get(fid, {"last_minute": -1, "last_goals": -1, "last_odds_check_minute": -1})
    item["last_odds_check_minute"] = int(minute)
    live_cache[fid] = item

# ================== ОСНОВНАЯ ЛОГИКА: СКАН И СИГНАЛ ==================
def scan_and_signal():
    global current_poll
    live = get_live()

    has_candidates = False

    for m in live:
        try:
            f = m["fixture"]; t = m["teams"]; g = m["goals"]; L = m["league"]
            fid = int(f["id"])
            elapsed = int(f["status"]["elapsed"] or 0)
            gh, ga = (g["home"] or 0), (g["away"] or 0)
            total = gh + ga

            # интересуют ТОЛЬКО ранние отрезки: до/включая 20'
            if elapsed > WINDOW_MAX_MINUTE:
                continue

            # интересуют ТОЛЬКО ровно 2 или 3 гола
            if total not in (2, 3):
                continue

            has_candidates = True

            # обновим кэш (фиксируем, менялось ли что-то)
            changed = update_live_cache(fid, elapsed, total)

            # если уже сигналили по этому матчу — пропустим
            if fid in signaled_ids:
                continue

            # odds вызываем максимум раз в минуту по фикстуре
            if not can_check_odds_now(fid, elapsed):
                continue

            # если за последний цикл ничего не поменялось — можно пропустить вызов odds
            if not changed:
                continue

            # определить линию по правилу
            target_total = 3 if total == 2 else 4

            # получить лучший кэф по рынку Over target_total
            odds, bookmaker = get_over_odds_for_line(fid, target_total)
            mark_odds_checked(fid, elapsed)

            # обязательный фильтр по кэфу
            if odds is None or not (LOW_ODDS <= odds <= HIGH_ODDS):
                log.info(f"[skip] fid={fid} min={elapsed} total={total} O{target_total} odds={odds}")
                continue

            rec = {
                "fixture_id": fid,
                "utc": f["date"],
                "minute": elapsed,
                "home": t["home"]["name"],
                "away": t["away"]["name"],
                "league": L["name"],
                "country": L.get("country") or "",
                "goals_home": gh,
                "goals_away": ga,
                "total_at_signal": total,
                "bet_line": f"ТБ {target_total}",
                "odds": round(float(odds), 2),
                "bookmaker": bookmaker or "",
                "ts": int(now_local().timestamp())
            }

            # сохраняем в файл дня (переживёт перезапуск)
            append_day_signal(rec)
            # помечаем, что по этому матчу сигнал уже слали
            signaled_ids.add(fid)

            # отправляем сообщение
            send(
                "⚽️ *Ставка!*\n"
                f"🏆 {rec['country']} — {rec['league']}\n"
                f"{rec['home']} {gh} — {ga} {rec['away']}\n"
                f"⏱ {elapsed}'  • {rec['bet_line']}  • кэф *{rec['odds']:.2f}*"
                + (f"  ({rec['bookmaker']})" if rec['bookmaker'] else "")
                + "\n─────────────"
            )
            log.info("Signal sent: fid=%s  %s %d-%d %s  min=%d  O%d @ %.2f",
                     fid, rec['home'], gh, ga, rec['away'], elapsed, target_total, rec['odds'])

        except Exception as e:
            log.error(f"scan_and_signal item error: {e}")

    # адаптивная частота: ускоряемся ТОЛЬКО пока есть кандидаты до 20'
    current_poll = BOOST_POLL if has_candidates else BASE_POLL

# ================== ОТЧЁТЫ ==================
def settle_and_build_lines(records: list[dict]):
    wins = losses = 0
    pnl  = 0.0
    lines = []

    for i, rec in enumerate(records, start=1):
        res = get_fixture_result(rec["fixture_id"])
        if not res:
            lines.append(f"#{i:02d} ❓ {rec['home']} — {rec['away']} | результат недоступен")
            continue

        st, gh, ga = res
        total = (gh or 0) + (ga or 0)
        need  = 4 if rec["bet_line"] == "ТБ 3" else 5

        if st == "FT":
            if total >= need:
                wins += 1
                pnl += STAKE_UNITS
                lines.append(f"#{i:02d} ✅ +{STAKE_UNITS}  ({rec.get('odds','n/a')})  {rec['home']} {gh}-{ga} {rec['away']} | {rec['bet_line']}")
            else:
                losses += 1
                pnl -= STAKE_UNITS
                lines.append(f"#{i:02d} ❌ -{STAKE_UNITS}  ({rec.get('odds','n/a')})  {rec['home']} {gh}-{ga} {rec['away']} | {rec['bet_line']}")
        else:
            lines.append(f"#{i:02d} ⏳ {rec['home']} — {rec['away']} | статус: {st}")

    return lines, wins, losses, pnl

def send_daily_report():
    day = today_str()
    records = load_day_signals(day)

    if not records:
        send("🗒 За сегодня ставок не было.")
        return

    lines, wins, losses, pnl = settle_and_build_lines(records)
    total_bets = wins + losses
    passrate = (wins / total_bets * 100.0) if total_bets else 0.0

    msg = [
        "📊 *Отчёт за день*",
        f"Дата: {day} (Europe/Warsaw)",
        "─────────────",
        f"Всего ставок: {total_bets}",
        f"Сыграло: {wins}  |  Не сыграло: {losses}",
        f"Проходимость: {passrate:.0f}%",
        f"Итог: {pnl:+.0f} (ставка {STAKE_UNITS})",
        "─────────────",
        *lines
    ]
    send("\n".join(msg))

    # История исходов
    for rec in records:
        res = get_fixture_result(rec["fixture_id"])
        if not res:  # нет финала
            continue
        st, gh, ga = res
        if st != "FT":
            continue
        need = 4 if rec["bet_line"] == "ТБ 3" else 5
        total = (gh or 0) + (ga or 0)
        outcome = "win" if total >= need else "loss"
        pnl1 = STAKE_UNITS if outcome == "win" else -STAKE_UNITS
        append_history({
            "ts": now_local().isoformat(),
            "date": day,
            "fixture_id": rec["fixture_id"],
            "home": rec["home"],
            "away": rec["away"],
            "league": rec["league"],
            "country": rec["country"],
            "bet_line": rec["bet_line"],
            "odds": rec.get("odds"),
            "bookmaker": rec.get("bookmaker"),
            "result_score": f"{gh}-{ga}",
            "status": st,
            "pnl": pnl1,
            "outcome": outcome
        })

    # Очистим файл дня
    save_day_signals([], day)

def aggregate_history(start_dt: datetime, end_dt: datetime) -> dict:
    s_utc = start_dt.astimezone(pytz.UTC)
    e_utc = end_dt.astimezone(pytz.UTC)

    bets = wins = losses = 0
    pnl  = 0

    for row in read_history_iter() or []:
        try:
            ts = datetime.fromisoformat(row.get("ts"))
        except Exception:
            continue
        ts_utc = ts.astimezone(pytz.UTC)
        if not (s_utc <= ts_utc <= e_utc):
            continue
        if row.get("outcome") not in ("win", "loss"):
            continue

        bets += 1
        pnl += int(row.get("pnl", 0))
        if row["outcome"] == "win":
            wins += 1
        else:
            losses += 1

    rate = (wins / bets * 100.0) if bets else 0.0
    return {"bets": bets, "wins": wins, "losses": losses, "pnl": pnl, "rate": rate}

def send_weekly_monthly_reports():
    now = now_local()

    # Неделя: последние 7 суток (включая сегодня)
    end_dt = now.replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=7)
    agg = aggregate_history(start_dt, end_dt)
    send(
        "📈 *Отчёт за неделю*\n"
        f"Период: {start_dt.strftime('%d.%m %H:%M')} — {end_dt.strftime('%d.%m %H:%M')}\n"
        "─────────────\n"
        f"Ставок: {agg['bets']}\n"
        f"Сыграло: {agg['wins']}  |  Не сыграло: {agg['losses']}\n"
        f"Проходимость: {agg['rate']:.0f}%\n"
        f"Итог: {agg['pnl']:+.0f} (ставка {STAKE_UNITS})"
    )

    # Месяц: с 1-го числа по сегодня
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    agg_m = aggregate_history(first_of_month, end_dt)
    send(
        "🗓 *Отчёт за месяц (текущий)*\n"
        f"Период: {first_of_month.strftime('%d.%m')} — {end_dt.strftime('%d.%m')}\n"
        "─────────────\n"
        f"Ставок: {agg_m['bets']}\n"
        f"Сыграло: {agg_m['wins']}  |  Не сыграло: {agg_m['losses']}\n"
        f"Проходимость: {agg_m['rate']:.0f}%\n"
        f"Итог: {agg_m['pnl']:+.0f} (ставка {STAKE_UNITS})"
    )

# ================== КОМАНДЫ TELEGRAM ==================
@bot.message_handler(commands=['status'])
def cmd_status(message):
    try:
        now = now_local()
        day = today_str()
        records = load_day_signals(day)
        text = [
            "🩺 *Статус бота*",
            f"⏱ Локальное время: {now.strftime('%Y-%m-%d %H:%M')}",
            f"🌍 TIMEZONE: {TIMEZONE}",
            f"⚙️ Опрос: {current_poll//60} мин (адаптивно)",
            f"🎯 Фильтр: до 20' и ровно 2/3 гола",
            f"💵 Кэф фильтр: {LOW_ODDS:.2f}–{HIGH_ODDS:.2f}",
            f"🧾 Сигналов сегодня: {len(records)}",
        ]
        bot.reply_to(message, "\n".join(text), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"Ошибка /status: {e}")

@bot.message_handler(commands=['report'])
def cmd_report(message):
    try:
        send_daily_report()
        bot.reply_to(message, "📨 Отчёт за сегодня отправлен.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка /report: {e}")

@bot.message_handler(commands=['test_signal'])
def cmd_test_signal(message):
    try:
        now = now_local()
        fake = {
            "fixture_id": 999999,
            "utc": now.astimezone(pytz.UTC).isoformat(),
            "minute": 19,
            "home": "Test FC",
            "away": "Debug United",
            "league": "DEBUG League",
            "country": "DEBUG",
            "goals_home": 1,
            "goals_away": 1,
            "total_at_signal": 2,
            "bet_line": "ТБ 3",
            "odds": 1.75,
            "bookmaker": "DEBUG",
            "ts": int(now.timestamp())
        }
        append_day_signal(fake)
        bot.reply_to(message, "✅ Тестовый сигнал добавлен. Запусти /report для проверки.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка /test_signal: {e}")

# ================== RUN ==================
if __name__ == "__main__":
    Thread(target=run_http,    daemon=True).start()
    Thread(target=run_telebot, daemon=True).start()

    send("🚀 Бот запущен — новая версия!")
    send(f"✅ Режим: до 20' (2/3 гола) + кэф {LOW_ODDS:.2f}–{HIGH_ODDS:.2f} (ТБ3/ТБ4). Отчёт 23:30 (Europe/Warsaw).")

    while True:
        try:
            log.info(f"Tick: {now_local().strftime('%Y-%m-%d %H:%M')}")
            scan_and_signal()

            now = now_local()
            if now.hour == 23 and 30 <= now.minute <= 35:
                send_daily_report()
                # Недельная + месячная — можно слать раз в сутки вместе с дневным
                send_weekly_monthly_reports()
                time.sleep(60)

            time.sleep(current_poll)
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(current_poll)
