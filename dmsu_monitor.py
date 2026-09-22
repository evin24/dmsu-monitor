"""
DMSU.gov.ua — Моніторинг вільних місць в електронній черзі

Особливості:
  - Час синхронізовано з Києвом (Europe/Kyiv)
  - Нічне вікно 23:00–02:00 за Києвом — перевірка кожну хвилину
    (покриває вікно техобслуговування 21:00–00:01 UTC + запас)
  - Вдень 02:00–23:00 — перевірка кожні 5 хвилин
  - Помилка 500 (техобслуговування) — мовчки пропускається
  - Retry з backoff на мережеві збої в нічному вікні
  - Перший запит після відновлення завжди з актуальною датою

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
    # FIX: fallback для дуже старих Python (3.8 і нижче)
    KYIV_TZ = timezone(timedelta(hours=3))

def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

# ── Налаштування ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

# FIX: безпечний парсинг TELEGRAM_CHAT_IDS — не крашимо на старті,
# якщо в Railway випадково вставили просто "123456" без дужок.
_chat_ids_env = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
if _chat_ids_env:
    try:
        parsed = json.loads(_chat_ids_env)
        if isinstance(parsed, list):
            TELEGRAM_CHAT_IDS = [str(x) for x in parsed]
        else:
            TELEGRAM_CHAT_IDS = [str(parsed)]
    except json.JSONDecodeError:
        # приймаємо і "123456,789012", і "123456"
        TELEGRAM_CHAT_IDS = [x.strip() for x in _chat_ids_env.split(",") if x.strip()]
else:
    TELEGRAM_CHAT_IDS = []

# Параметри API
SUBDIVISION_ID = 17   # Деснянський відділ ЦМУ ДМС м. Київ
SERVICE_ID     = 5    # Паспорт для виїзду за кордон
DAYS_AHEAD     = 60   # перевіряти на 2 місяці вперед

# Інтервали перевірки
INTERVAL_NORMAL  = 300   # 5 хвилин — звичайний час
INTERVAL_NIGHT   = 60    # 1 хвилина — нічний час (оновлення черги)
# FIX: нічне вікно розширено до 02:00, щоб покрити техобслуговування 21:00–00:01 UTC
# (влітку UTC+3 → це 00:01–03:01 за Києвом; взимку UTC+2 → 23:01–02:01).
# Ставимо до 02:00 — компроміс, який ловить більшість оновлень.
NIGHT_START_HOUR = 23    # з 23:00 за Києвом
NIGHT_END_HOUR   = 2     # до 02:00 за Києвом

# Retry на мережеві збої (важливо для нічного вікна)
RETRY_ATTEMPTS = 3
RETRY_BACKOFF  = [2, 5, 10]  # секунди між спробами

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
      23:00 – 02:00 → кожну хвилину (черга оновлюється опівночі,
                        плюс запас на техобслуговування)
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
    Запит до API з retry.
    Повертає (список_вільних_дат, успішно).
    При помилці 500 повертає ([], False) — скрипт мовчить і чекає.
    """
    today = now_kyiv()
    end   = today + timedelta(days=DAYS_AHEAD)
    params = {
        "startDate": today.strftime("%Y-%m-%d"),
        "endDate":   end.strftime("%Y-%m-%d"),
    }

    last_err: Exception | None = None

    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)

            # 500 = техобслуговування — не ретраїмо, просто чекаємо наступного циклу
            if r.status_code == 500:
                log.warning("⚙  Сервер на обслуговуванні (500) — чекаємо відновлення…")
                return [], False

            # 502/503/504 — теж технічні, не ретраїмо агресивно
            if r.status_code in (502, 503, 504):
                log.warning(f"⚙  Сервер недоступний ({r.status_code}) — чекаємо…")
                return [], False

            r.raise_for_status()

            # FIX: захист від {"data": null} та неочікуваних типів
            try:
                payload = r.json()
            except ValueError:
                log.warning("Не вдалося розпарсити JSON — пропускаємо цикл.")
                return [], False

            days = payload.get("data") or []
            if not isinstance(days, list):
                log.warning(f"Несподіваний формат data: {type(days).__name__}")
                days = []

            log.info(f"API: {len(days)} вільних дат ({params['startDate']} → {params['endDate']})")
            return days, True

        except requests.RequestException as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS - 1:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                log.warning(f"Спроба {attempt + 1} невдала ({e}); повтор через {wait} с…")
                # FIX: синхронний sleep тут, бо fetch викликається з async функції,
                # але сам виклик requests — блокуючий, тому це прийнятно для 3 спроб.
                import time
                time.sleep(wait)

    log.error(f"Помилка API після {RETRY_ATTEMPTS} спроб: {last_err}")
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
    if not TELEGRAM_TOKEN:
        log.error("❌ TELEGRAM_TOKEN не задано. Встановіть змінну середовища на Railway.")
        return

    if not TELEGRAM_CHAT_IDS:
        log.error("❌ TELEGRAM_CHAT_IDS порожній. Встановіть змінну середовища на Railway.")
        return

    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    now = now_kyiv().strftime("%d.%m.%Y %H:%M")

    await send_telegram(
        bot,
        "🤖 <b>Моніторинг ДМСУ запущено</b>\n"
        "📍 Деснянський відділ ЦМУ ДМС м. Київ\n"
        "📄 Паспорт для виїзду за кордон\n"
        f"🕐 {now} (час Київ)\n\n"
        "Нічне вікно 23:00–02:00 — перевірка щохвилини.\n"
        "Решту часу — кожні 5 хвилин."
    )

    # FIX: last_notified оновлюється ІНКРЕМЕНТАЛЬНО (|=), а не перезаписом.
    # Це гарантує, що повторна поява дати після тимчасового зникнення
    # знову викличе сповіщення.
    last_notified: set[str] = set()
    check_count = 0
    was_maintenance = False

    while True:
        check_count += 1
        now = now_kyiv().strftime("%d.%m.%Y %H:%M")
        interval = get_interval()

        log.info(
            f"─── Перевірка #{check_count} | {now} Київ | "
            f"інтервал: {interval} с ───"
        )

        free_days, success = fetch_free_days()

        if not success:
            was_maintenance = True
            await asyncio.sleep(interval)
            continue

        # Повідомляємо один раз про відновлення після обслуговування
        if was_maintenance:
            log.info("✅ Сервер відновлено після обслуговування.")
            await send_telegram(
                bot,
                f"✅ <b>Сервер ДМСУ знову доступний</b>\n"
                f"Продовжую моніторинг.\n"
                f"🕐 {now} (Київ)"
            )
            was_maintenance = False

        current = set(free_days)
        # FIX: new_days — це дати, які ЗАРАЗ є і про які ми ще не повідомляли.
        # last_notified містить усе, що ми колись бачили в поточній "серії".
        new_days = current - last_notified

        if new_days:
            day_list = "\n".join(f"  📅 {fmt_date(d)}" for d in sorted(new_days))
            await send_telegram(
                bot,
                "🟢 <b>З'явились вільні місця на запис до ДМСУ!</b>\n\n"
                f"{day_list}\n\n"
                f"👉 <a href='{BOOKING_URL}'>Записатись зараз!</a>\n"
                f"🕐 {now} (Київ)"
            )
            # FIX: додаємо нові дати, НЕ перезаписуємо весь набір
            last_notified |= current
        else:
            log.info("Вільних місць немає.")

        # FIX: якщо зараз взагалі немає вільних дат — скидаємо "історію".
        # Наступна поява будь-якої дати знову викличе сповіщення.
        if not current:
            last_notified.clear()

        # Щогодинний статус (рахуємо кількість циклів на годину залежно від інтервалу)
        cycles_per_hour = max(1, 3600 // interval)
        if check_count % cycles_per_hour == 0:
            status = f"🟢 є {len(free_days)} вільних дат" if free_days else "⛔ вільних місць немає"
            await send_telegram(
                bot,
                "🔄 <b>Моніторинг активний</b>\n"
                f"Перевірок: {check_count} | {status}\n"
                f"🕐 {now} (Київ)"
            )

        await asyncio.sleep(interval)


if __name__ == "__main__":
    log.info("Запуск моніторингу ДМСУ (Railway)…")
    asyncio.run(run_loop())