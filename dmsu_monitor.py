

"""
DMSU.gov.ua — Моніторинг вільних місць в електронній черзі

Особливості:
  - Час синхронізовано з Києвом (Europe/Kyiv)
  - З 23:00 до 00:30 за Києвом — перевірка кожну хвилину (час оновлення черги)
  - З 00:30 до 23:00 — перевірка кожні 5 хвилин
  - Помилка 500 (техобслуговування 21:00-00:01 UTC) — мовчки пропускається
  - Перший запит після відновлення (00:01 Київ) завжди з актуальною датою

Залежності:
    pip install requests python-telegram-bot
"""

import asyncio
import logging
import os
import json
from datetime import datetime, timedelta, timezone

import requests
import telegram

# ── Часовий пояс Києва ───────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except ImportError:
    KYIV_TZ = timezone(timedelta(hours=3))

def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

# ── Налаштування ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "123456789:AAxxxx...")

_chat_ids_env = os.environ.get("TELEGRAM_CHAT_IDS", "")
if _chat_ids_env:
    TELEGRAM_CHAT_IDS = json.loads(_chat_ids_env)
else:
    TELEGRAM_CHAT_IDS = [
        "твій_chat_id",
    ]

# Параметри API
SUBDIVISION_ID = 17   # Деснянський відділ ЦМУ ДМС м. Київ
SERVICE_ID     = 5    # Паспорт для виїзду за кордон
DAYS_AHEAD     = 60   # перевіряти на 2 місяці вперед

# Інтервали перевірки
INTERVAL_NORMAL  = 300   # 5 хвилин — звичайний час
INTERVAL_NIGHT   = 60    # 1 хвилина — нічний час (оновлення черги)
NIGHT_START_HOUR = 23    # з 23:00 за Києвом
NIGHT_END_HOUR   = 1     # до 01:00 за Києвом
# ─────────────────────────────────────────────────────────────────────────────

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


def get_interval() -> int:
    """
    Повертає інтервал перевірки залежно від часу за Києвом:
      23:00 – 01:00 → кожну хвилину (черга оновлюється опівночі)
      решта часу    → кожні 5 хвилин
    """
    hour = now_kyiv().hour
    if hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR:
        return INTERVAL_NIGHT
    return INTERVAL_NORMAL


def fmt_date(d: str) -> str:
    """2026-10-15 → 15 жовтня 2026"""
    y, m, day = d.split("-")
    return f"{int(day)} {MONTH_UA.get(m, m)} {y}"


def fetch_free_days() -> tuple[list[str], bool]:
    """
    Запит до API.
    Повертає (список_вільних_дат, успішно).
    При помилці 500 повертає ([], False) — скрипт мовчить і чекає.
    """
    today = now_kyiv()
    end   = today + timedelta(days=DAYS_AHEAD)
    params = {
        "startDate": today.strftime("%Y-%m-%d"),
        "endDate":   end.strftime("%Y-%m-%d"),
    }
    try:
        r = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)

        # 500 = техобслуговування (21:00-00:01 UTC) — мовчки пропускаємо
        if r.status_code == 500:
            log.warning("⚙  Сервер на обслуговуванні (500) — чекаємо відновлення…")
            return [], False

        r.raise_for_status()
        days = r.json().get("data", [])
        log.info(f"API: {len(days)} вільних дат ({params['startDate']} → {params['endDate']})")
        return days, True

    except requests.RequestException as e:
        log.error(f"Помилка API: {e}")
        return [], False


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


async def run_loop() -> None:
    """Нескінченний цикл для Railway."""
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    now = now_kyiv().strftime("%d.%m.%Y %H:%M")

    await send_telegram(
        bot,
        "🤖 <b>Моніторинг ДМСУ запущено</b>\n"
        f"📍 Деснянський відділ ЦМУ ДМС м. Київ\n"
        f"📄 Паспорт для виїзду за кордон\n"
        f"🕐 {now} (час Київ)\n\n"
        "Вночі (23:00–01:00) перевірка щохвилини.\n"
        "Решту часу — кожні 5 хвилин."
    )

    last_notified: set[str] = set()
    check_count = 0
    was_maintenance = False  # флаг: чи були на обслуговуванні

    while True:
        check_count += 1
        now  = now_kyiv().strftime("%d.%m.%Y %H:%M")
        hour = now_kyiv().hour
        interval = get_interval()

        log.info(
            f"─── Перевірка #{check_count} | {now} Київ | "
            f"інтервал: {interval//60} хв ───"
        )

        free_days, success = fetch_free_days()

        if not success:
            # Сервер на обслуговуванні — чекаємо мовчки
            was_maintenance = True
            await asyncio.sleep(interval)
            continue

        # Щойно відновились після обслуговування — повідомимо
        if was_maintenance:
            log.info("✅ Сервер відновлено після обслуговування.")
            was_maintenance = False

        new_days = set(free_days) - last_notified

        if new_days:
            day_list = "\n".join(f"  📅 {fmt_date(d)}" for d in sorted(new_days))
            await send_telegram(
                bot,
                f"🟢 <b>З'явились вільні місця на запис до ДМСУ!</b>\n\n"
                f"{day_list}\n\n"
                f"👉 <a href='{BOOKING_URL}'>Записатись зараз!</a>\n"
                f"🕐 {now} (Київ)"
            )
            last_notified = set(free_days)
        else:
            log.info("Вільних місць немає.")
            if not free_days:
                last_notified.clear()

        # Щогодинний статус
        if check_count % max(1, 3600 // interval) == 0:
            status = f"🟢 є {len(free_days)} вільних дат" if free_days else "⛔ вільних місць немає"
            await send_telegram(
                bot,
                f"🔄 <b>Моніторинг активний</b>\n"
                f"Перевірок: {check_count} | {status}\n"
                f"🕐 {now} (Київ)"
            )

        await asyncio.sleep(interval)


if __name__ == "__main__":
    log.info("Запуск моніторингу ДМСУ (Railway)…")
    asyncio.run(run_loop())
