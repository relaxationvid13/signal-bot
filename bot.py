# -*- coding: utf-8 -*-
"""
Непадающий каркас бота для Render:
- HTTP health на / (чтобы Render не выключал web-сервис)
- Безопасные проверки окружения (не падает при отсутствии)
- Команды /start /ping /status /scan
- Периодические задачи (08:00 scan, 23:30 daily, вс 23:50 weekly, последний день 23:50 monthly)
- Весь код окружён try/except с логами
"""
import os
import sys
import time
import json
import pytz
import traceback
import threading
from datetime import datetime, timedelta, date

from flask import Flask

# Телеграм — опционально (если нет токена — просто не шлём)
try:
    import telebot
except Exception:
    telebot = None


# ====================== Настройки окружения ======================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TZ_NAME            = os.getenv("TZ", "Europe/Warsaw").strip()

# Частота «тика» основного цикла
TICK_SECONDS = 30  # каждые 30 секунд

# Времена срабатывания (локальное время TZ_NAME)
SCAN_TIME     = (8, 0)    # 08:00
DAILY_TIME    = (23, 30)  # 23:30
WEEKLY_TIME   = (23, 50)  # 23:50 каждое воскресенье
MONTHLY_TIME  = (23, 50)  # 23:50 в последний день месяца

# Папка для лёгких состояний/файлов (дни запуска и пр.)
STATE_FILE = "runtime_state.json"


# ====================== Утилиты логирования ======================

def log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


# ====================== Часы/таймзона ======================

def now_local():
    tz = pytz.timezone(TZ_NAME)
    return datetime.now(tz)

def is_last_day_of_month(dt: datetime) -> bool:
    tomorrow = dt + timedelta(days=1)
    return tomorrow.month != dt.month


# ====================== HTTP health для Render ======================

app = Flask(__name__)

@app.get("/")
def health():
    return "ok"

def run_http():
    port = int(os.getenv("PORT", "10000"))
    log(f"[boot] HTTP health server on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)


# ====================== Телеграм обёртки ======================

def can_telegram():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and telebot)

def send_telegram(text: str):
    """Безопасная отправка. Если не настроено — только лог."""
    if not can_telegram():
        log(f"[TG disabled] {text}")
        return
    try:
        bot.send_message(int(TELEGRAM_CHAT_ID), text)
    except Exception:
        log("[TG error] " + text)
        traceback.print_exc()


# ====================== Лёгкое состояние ======================

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        traceback.print_exc()
    return {
        "last_scan_date": "",     # "YYYY-MM-DD"
        "last_daily_date": "",
        "last_weekly_date": "",
        "last_monthly_stamp": "",  # "YYYY-MM" (месяц отчёта)
    }

def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()


STATE = load_state()


# ====================== Заглушки бизнес-логики ======================

def do_scan():
    """Тут будет реальная логика сканирования матчей.
       Пока шлём простое уведомление, чтобы видеть срабатывание."""
    dt = now_local().strftime("%Y-%m-%d %H:%M %Z")
    send_telegram(f"🔎 Сканирование (заглушка): {dt}\n"
                  f"Позже сюда вставим реальную логику.")

def send_daily_report():
    dt = now_local().strftime("%Y-%m-%d %H:%M %Z")
    send_telegram(f"📊 Дневной отчёт (заглушка): {dt}\n"
                  f"Здесь будет реальная статистика за день.")

def send_weekly_report():
    dt = now_local().strftime("%Y-%m-%d %H:%M %Z")
    send_telegram(f"🗓 Недельная сводка (заглушка): {dt}\n"
                  f"Здесь будет реальная статистика за неделю.")

def send_monthly_report():
    dt = now_local().strftime("%Y-%m-%d %H:%M %Z")
    send_telegram(f"📅 Месячная сводка (заглушка): {dt}\n"
                  f"Здесь будет реальная статистика за месяц.")


# ====================== Планировщик ======================

def should_fire(hour_min_tuple) -> bool:
    """Проверяем, что текущее локальное время попало в нужную минуту."""
    hh, mm = hour_min_tuple
    now = now_local()
    return (now.hour == hh) and (now.minute == mm)

def cron_tick():
    """Один «тик» планировщика — решает, что запускать."""
    global STATE

    now = now_local()
    today = now.strftime("%Y-%m-%d")

    # 1) Ежедневный скан в 08:00
    if should_fire(SCAN_TIME):
        if STATE.get("last_scan_date") != today:
            log("Run: do_scan()")
            try:
                do_scan()
                STATE["last_scan_date"] = today
                save_state(STATE)
            except Exception:
                traceback.print_exc()

    # 2) Ежедневный отчёт в 23:30
    if should_fire(DAILY_TIME):
        if STATE.get("last_daily_date") != today:
            log("Run: send_daily_report()")
            try:
                send_daily_report()
                STATE["last_daily_date"] = today
                save_state(STATE)
            except Exception:
                traceback.print_exc()

    # 3) Недельный (вс) 23:50
    if should_fire(WEEKLY_TIME) and now.weekday() == 6:  # Monday=0 ... Sunday=6
        if STATE.get("last_weekly_date") != today:
            log("Run: send_weekly_report()")
            try:
                send_weekly_report()
                STATE["last_weekly_date"] = today
                save_state(STATE)
            except Exception:
                traceback.print_exc()

    # 4) Месячный в последний день месяца 23:50
    if should_fire(MONTHLY_TIME) and is_last_day_of_month(now):
        ym = now.strftime("%Y-%m")  # месяц отчёта
        if STATE.get("last_monthly_stamp") != ym:
            log("Run: send_monthly_report()")
            try:
                send_monthly_report()
                STATE["last_monthly_stamp"] = ym
                save_state(STATE)
            except Exception:
                traceback.print_exc()


# ====================== Телеграм-бот (команды) ======================

bot = None
if can_telegram():
    try:
        bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")

        @bot.message_handler(commands=["start"])
        def cmd_start(m):
            msg = (
                "Привет! Я живу на Render 👋\n\n"
                "Команды:\n"
                "• /ping — проверить отклик\n"
                "• /status — расписание и TZ\n"
                "• /scan — вручную запустить скан\n"
            )
            bot.send_message(m.chat.id, msg)

        @bot.message_handler(commands=["ping"])
        def cmd_ping(m):
            bot.send_message(m.chat.id, "pong ✅")

        @bot.message_handler(commands=["status"])
        def cmd_status(m):
            info = (
                f"🕒 TZ={TZ_NAME}\n"
                f"🔎 Скан: {SCAN_TIME[0]:02d}:{SCAN_TIME[1]:02d}\n"
                f"📊 Дневной отчёт: {DAILY_TIME[0]:02d}:{DAILY_TIME[1]:02d}\n"
                f"🗓 Недельный (вс): {WEEKLY_TIME[0]:02d}:{WEEKLY_TIME[1]:02d}\n"
                f"📅 Месячный (посл. день): {MONTHLY_TIME[0]:02d}:{MONTHLY_TIME[1]:02d}\n"
            )
            bot.send_message(m.chat.id, info)

        @bot.message_handler(commands=["scan"])
        def cmd_scan(m):
            try:
                do_scan()
                bot.send_message(m.chat.id, "Запустил сканирование ✅")
            except Exception:
                traceback.print_exc()
                bot.send_message(m.chat.id, "Ошибка при сканировании ❌")

        def run_tg():
            log("[boot] Telegram polling started")
            while True:
                try:
                    bot.infinity_polling(timeout=30, long_polling_timeout=30)
                except Exception:
                    traceback.print_exc()
                    time.sleep(5)

        threading.Thread(target=run_tg, daemon=True).start()

    except Exception:
        log("WARN: Не удалось инициализировать телеграм-бота.")
        traceback.print_exc()
else:
    log("INFO: Telegram отключён (нет токена/чат_id или pyTelegramBotAPI).")


# ====================== Главный цикл ======================

def main_loop():
    log("Main loop started.")
    while True:
        try:
            cron_tick()           # проверяем расписание
            log("Tick: alive.")   # видно в логах Render, что живём
            time.sleep(TICK_SECONDS)
        except Exception:
            log("ERROR in main loop:")
            traceback.print_exc()
            time.sleep(5)         # чтобы не крутить ошибки слишком часто


# ====================== Старт ======================

if __name__ == "__main__":
    # 1) HTTP health для Render
    threading.Thread(target=run_http, daemon=True).start()

    # 2) Приветственное сообщение при старте
    try:
        send_telegram(
            "🚀 Бот запущен (стабильная версия, Render-ready).\n"
            f"ℹ️ TZ={TZ_NAME}. "
            f"Скан 08:00, дневной 23:30, недельный вс 23:50, месячный посл. день 23:50.\n"
            "Для ручного запуска: /scan"
        )
    except Exception:
        traceback.print_exc()

    # 3) Основной цикл
    main_loop()
