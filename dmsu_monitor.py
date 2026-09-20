"""
DMSU.gov.ua — Моніторинг вільних місць в електронній черзі

Режими роботи:
  - GitHub Actions: одна перевірка і завершення (запускається кожні 5 хв за розкладом)
  - Локально:       нескінченний цикл з інтервалом CHECK_INTERVAL_SEC

Залежності:
    pip install requests python-telegram-bot
"""

import asyncio
import logging
import os
import json
from datetime import datetime, timedelta

import requests
import telegram

# ─────────────────────────────────────────────
#  НАЛАШТУВАННЯ
#  Локально: заповни тут
#  GitHub Actions: заповни в Secrets (Settings → Secrets → Actions)
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "123456789:AAxxxx...")

# Для GitHub Actions: в Secrets вкажи як JSON рядок: ["111","222"]
# Локально: просто заповни список нижче
_chat_ids_env = os.environ.get("TELEGRAM_CHAT_IDS", "")
if _chat_ids_env:
    TELEGRAM_CHAT_IDS = json.loads(_chat_ids_env)
else:
    TELEGRAM_CHAT_IDS = [
        "твій_chat_id",
        # "chat_id_дружини",
    ]

CHECK_INTERVAL_SEC = 300   # лише для локального запуску (5 хвилин)

# Параметри API
SUBDIVISION_ID = 17   # Деснянський відділ ЦМУ ДМС м. Київ
SERVICE_ID     = 5    # Паспорт для виїзду за кордон
DAYS_AHEAD     = 60   # перевіряти на 2 місяці вперед
# ─────────────────────────────────────────────

API_URL     = f"https://cherga.dmsu.gov.ua/api/v1/days/{SUBDIVISION_ID}/{SERVICE_ID}"
BOOKING_URL = "https://dmsu.gov.ua/services/online.html"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "uk-UA,uk;q=0.9",
    "referer": "https://cherga.dmsu.gov.ua/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

MONTH_UA = {
    "01": "січня", "02": "лютого", "03": "березня",
    "04": "квітня", "05": "травня", "06": "червня",
    "07": "липня", "08": "серпня", "09": "вересня",
    "10": "жовтня", "11": "листопада", "12": "грудня",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def fmt_date(d: str) -> str:
    """2026-10-15 → 15 жовтня 2026"""
    y, m, day = d.split("-")
    return f"{int(day)} {MONTH_UA.get(m, m)} {y}"


def fetch_free_days() -> list[str]:
    """Один запит до API, повертає список вільних дат."""
    today = datetime.now()
    end   = today + timedelta(days=DAYS_AHEAD)
    params = {
        "startDate": today.strftime("%Y-%m-%d"),
        "endDate":   end.strftime("%Y-%m-%d"),
    }
    try:
        r = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        days = r.json().get("data", [])
        log.info(f"API: {len(days)} вільних дат")
        return days
    except Exception as e:
        log.error(f"Помилка API: {e}")
        return []


async def send_telegram(bot: telegram.Bot, message: str) -> None:
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            log.info(f"Telegram надіслано → {chat_id}")
        except Exception as e:
            log.error(f"Помилка Telegram для {chat_id}: {e}")


async def run_once() -> None:
    """Одна перевірка — для GitHub Actions."""
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    free_days = fetch_free_days()

    if free_days:
        day_list = "\n".join(f"  📅 {fmt_date(d)}" for d in sorted(free_days))
        await send_telegram(
            bot,
            f"🟢 <b>З'явились вільні місця на запис до ДМСУ!</b>\n\n"
            f"{day_list}\n\n"
            f"👉 <a href='{BOOKING_URL}'>Записатись зараз!</a>\n"
            f"🕐 {now}"
        )
    else:
        log.info("Вільних місць немає — тихо завершуємо.")


async def run_loop() -> None:
    """Нескінченний цикл — для локального запуску."""
    bot = telegram.Bot(token=TELEGRAM_TOKEN)

    await send_telegram(
        bot,
        "🤖 <b>Моніторинг ДМСУ запущено (локально)</b>\n"
        f"⏱ Перевірка кожні {CHECK_INTERVAL_SEC // 60} хв."
    )

    last_notified: set[str] = set()
    check_count = 0

    while True:
        check_count += 1
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        log.info(f"─── Перевірка #{check_count} | {now} ───")

        free_days = fetch_free_days()
        new_days  = set(free_days) - last_notified

        if new_days:
            day_list = "\n".join(f"  📅 {fmt_date(d)}" for d in sorted(new_days))
            await send_telegram(
                bot,
                f"🟢 <b>З'явились вільні місця на запис до ДМСУ!</b>\n\n"
                f"{day_list}\n\n"
                f"👉 <a href='{BOOKING_URL}'>Записатись зараз!</a>\n"
                f"🕐 {now}"
            )
            last_notified = set(free_days)
        else:
            log.info("Вільних місць немає.")
            if not free_days:
                last_notified.clear()

        await asyncio.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    # Визначаємо режим: GitHub Actions встановлює змінну GITHUB_ACTIONS=true
    if os.environ.get("GITHUB_ACTIONS") == "true":
        log.info("Режим: GitHub Actions (одна перевірка)")
        asyncio.run(run_once())
    else:
        log.info("Режим: локальний (нескінченний цикл)")
        asyncio.run(run_loop())
