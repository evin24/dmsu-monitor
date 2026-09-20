"""
DMSU.gov.ua — Моніторинг вільних місць в електронній черзі
Перевіряє API без браузера — просто HTTP запит кожні N хвилин.

Залежності (набагато простіше ніж IPC!):
    pip install requests python-telegram-bot
"""

import requests
import asyncio
import logging
from datetime import datetime, timedelta

import telegram

# ─────────────────────────────────────────────
#  НАЛАШТУВАННЯ — заповни перед запуском
# ─────────────────────────────────────────────
TELEGRAM_TOKEN    = ""
TELEGRAM_CHAT_IDS = [
    "",       # дізнатись через @userinfobot у Telegram
    # "chat_id_дружини",  # додай ще якщо потрібно
]

CHECK_INTERVAL_SEC = 300  # перевірка кожні 5 хвилин

# Параметри API (з DevTools)
SUBDIVISION_ID = 17   # ID підрозділу: Деснянський відділ ЦМУ ДМС м. Київ
SERVICE_ID     = 5    # ID послуги: паспорт для виїзду за кордон

# Перевіряти на скільки днів вперед (API повертає дані порціями)
DAYS_AHEAD = 60  # 2 місяці вперед
# ─────────────────────────────────────────────

API_URL = "https://cherga.dmsu.gov.ua/api/v1/days/{subdivision}/{service}"
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("dmsu_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def fetch_free_days(start_date: datetime, end_date: datetime) -> list[str]:
    """
    Запитує API і повертає список вільних дат.
    {"data": []} = немає місць
    {"data": ["2026-10-15", "2026-10-16"]} = є вільні дати
    """
    url = API_URL.format(
        subdivision=SUBDIVISION_ID,
        service=SERVICE_ID,
    )
    params = {
        "startDate": start_date.strftime("%Y-%m-%d"),
        "endDate":   end_date.strftime("%Y-%m-%d"),
    }

    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=15)
        response.raise_for_status()
        data = response.json()
        days = data.get("data", [])
        log.info(f"API відповів: {len(days)} вільних дат ({params['startDate']} → {params['endDate']})")
        return days
    except requests.RequestException as e:
        log.error(f"Помилка API запиту: {e}")
        return []
    except Exception as e:
        log.error(f"Несподівана помилка: {e}")
        return []


def get_all_free_days() -> list[str]:
    """Перевіряє всі дати на DAYS_AHEAD днів вперед."""
    today = datetime.now()
    end   = today + timedelta(days=DAYS_AHEAD)
    return fetch_free_days(today, end)


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


async def monitor() -> None:
    bot = telegram.Bot(token=TELEGRAM_TOKEN)

    # Перевіряємо підключення
    try:
        me = await bot.get_me()
        log.info(f"Telegram бот: @{me.username}")
    except Exception as e:
        log.error(f"Не вдалось підключитись до Telegram: {e}")
        return

    await send_telegram(
        bot,
        "🤖 <b>Моніторинг ДМСУ запущено</b>\n"
        f"📍 Підрозділ: Деснянський відділ ЦМУ ДМС м. Київ\n"
        f"📄 Послуга: Паспорт для виїзду за кордон\n"
        f"⏱ Перевірка кожні {CHECK_INTERVAL_SEC // 60} хв.\n\n"
        "Сповіщу як тільки з'являться вільні місця!"
    )

    last_notified: set[str] = set()
    check_count = 0

    while True:
        check_count += 1
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        log.info(f"─── Перевірка #{check_count} | {now} ───")

        free_days = get_all_free_days()
        new_days  = set(free_days) - last_notified

        if new_days:
            # Форматуємо дати красиво: 2026-10-15 → 15 жовтня 2026
            month_ua = {
                "01": "січня", "02": "лютого", "03": "березня",
                "04": "квітня", "05": "травня", "06": "червня",
                "07": "липня", "08": "серпня", "09": "вересня",
                "10": "жовтня", "11": "листопада", "12": "грудня",
            }
            def fmt(d: str) -> str:
                y, m, day = d.split("-")
                return f"{int(day)} {month_ua.get(m, m)} {y}"

            sorted_days = sorted(new_days)
            day_list = "\n".join(f"  📅 {fmt(d)}" for d in sorted_days)

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

        # Щогодинний статус
        checks_per_hour = max(1, 3600 // CHECK_INTERVAL_SEC)
        if check_count % checks_per_hour == 0:
            status = f"🟢 є {len(free_days)} вільних дат" if free_days else "⛔ вільних місць немає"
            await send_telegram(
                bot,
                f"🔄 <b>Моніторинг активний</b>\n"
                f"Перевірок: {check_count} | Статус: {status}\n"
                f"🕐 {now}"
            )

        await asyncio.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(monitor())
