import asyncio
import logging
import os
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

from apscheduler.schedulers.asyncio import AsyncIOScheduler


# -----------------------------
# Налаштування
# -----------------------------

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_TZ = os.getenv("APP_TZ", "Europe/Kyiv")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")


# -----------------------------
# Дані в пам'яті
# -----------------------------

# Для кожного користувача зберігаємо список страв за день
# Кожен запис:
# {
#   "food": "гречка",
#   "weight": 150,
#   "kcal": 165,
#   "p": 6.0,
#   "f": 1.5,
#   "c": 31.5
# }
user_data = defaultdict(list)

# База продуктів на 100 г
FOOD_DB = {
    "гречка": {"kcal": 110, "p": 4.0, "f": 1.0, "c": 21.0},
    "греча": {"kcal": 110, "p": 4.0, "f": 1.0, "c": 21.0},
    "рис": {"kcal": 130, "p": 2.7, "f": 0.3, "c": 28.0},
    "вівсянка": {"kcal": 68, "p": 2.4, "f": 1.4, "c": 12.0},
    "макарони": {"kcal": 131, "p": 5.0, "f": 1.1, "c": 25.0},
    "картопля": {"kcal": 77, "p": 2.0, "f": 0.1, "c": 17.0},
    "пюре": {"kcal": 90, "p": 2.0, "f": 3.0, "c": 15.0},
    "курка": {"kcal": 165, "p": 31.0, "f": 3.6, "c": 0.0},
    "грудка": {"kcal": 165, "p": 31.0, "f": 3.6, "c": 0.0},
    "крильця": {"kcal": 230, "p": 23.0, "f": 16.0, "c": 0.0},
    "яйце": {"kcal": 155, "p": 13.0, "f": 11.0, "c": 1.1},
    "яйця": {"kcal": 155, "p": 13.0, "f": 11.0, "c": 1.1},
    "огірок": {"kcal": 15, "p": 0.8, "f": 0.1, "c": 2.8},
    "огірки": {"kcal": 15, "p": 0.8, "f": 0.1, "c": 2.8},
    "кукурудза": {"kcal": 96, "p": 3.4, "f": 1.5, "c": 17.0},
    "крабові": {"kcal": 95, "p": 7.0, "f": 1.0, "c": 15.0},
    "крабпалички": {"kcal": 95, "p": 7.0, "f": 1.0, "c": 15.0},
    "хамон": {"kcal": 241, "p": 31.0, "f": 13.0, "c": 0.0},
    "прошуто": {"kcal": 250, "p": 26.0, "f": 17.0, "c": 0.0},
    "сир": {"kcal": 350, "p": 24.0, "f": 27.0, "c": 0.0},
    "хліб": {"kcal": 265, "p": 8.0, "f": 3.2, "c": 49.0},
    "банан": {"kcal": 89, "p": 1.1, "f": 0.3, "c": 23.0},
    "яблуко": {"kcal": 52, "p": 0.3, "f": 0.2, "c": 14.0},
    "мівіна": {"kcal": 250, "p": 6.0, "f": 10.0, "c": 30.0},
    "роліні": {"kcal": 198, "p": 6.0, "f": 8.0, "c": 26.0},
    "кабаноси": {"kcal": 400, "p": 25.0, "f": 35.0, "c": 2.0},
}


# -----------------------------
# Клавіатура
# -----------------------------

def get_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Підсумок", callback_data="summary")],
            [InlineKeyboardButton(text="↩️ Видалити останнє", callback_data="delete_last")],
            [InlineKeyboardButton(text="🗑 Очистити день", callback_data="reset")],
        ]
    )


# -----------------------------
# Допоміжні функції
# -----------------------------

def calculate_macros(food_name: str, weight: float) -> dict:
    data = FOOD_DB[food_name]
    factor = weight / 100.0

    kcal = data["kcal"] * factor
    p = data["p"] * factor
    f = data["f"] * factor
    c = data["c"] * factor

    return {
        "food": food_name,
        "weight": weight,
        "kcal": kcal,
        "p": p,
        "f": f,
        "c": c,
    }


def format_entry(entry: dict, index: int) -> str:
    return (
        f"{index}. {entry['food']} {entry['weight']:.0f}г — "
        f"{entry['kcal']:.0f} ккал "
        f"(Б {entry['p']:.1f} / Ж {entry['f']:.1f} / В {entry['c']:.1f})"
    )


def daily_totals(user_id: int) -> dict:
    items = user_data[user_id]
    return {
        "kcal": sum(item["kcal"] for item in items),
        "p": sum(item["p"] for item in items),
        "f": sum(item["f"] for item in items),
        "c": sum(item["c"] for item in items),
    }


def summary_text(user_id: int) -> str:
    items = user_data[user_id]

    if not items:
        return "📊 За сьогодні ще нічого не додано."

    lines = ["📋 <b>Що зʼїдено сьогодні:</b>\n"]

    for i, item in enumerate(items, start=1):
        lines.append(format_entry(item, i))

    total = daily_totals(user_id)

    lines.append("")
    lines.append(
        f"🔥 <b>Разом:</b> {total['kcal']:.0f} ккал\n"
        f"Б {total['p']:.1f} / Ж {total['f']:.1f} / В {total['c']:.1f}"
    )

    return "\n".join(lines)


# -----------------------------
# Хендлери
# -----------------------------

async def start_handler(message: Message) -> None:
    await message.answer(
        "Йоу 👋\n\n"
        "Пиши так:\n"
        "<b>гречка 150</b>\n\n"
        "Я порахую калорії, білки, жири і вуглеводи.\n\n"
        "Доступні кнопки нижче 👇",
        reply_markup=get_keyboard(),
    )


async def text_handler(message: Message) -> None:
    text = (message.text or "").strip().lower()

    if not text:
        return

    try:
        parts = text.split()
        if len(parts) < 2:
            raise ValueError("not enough parts")

        weight_str = parts[-1].replace(",", ".")
        food_name = " ".join(parts[:-1])
        weight = float(weight_str)

        if weight <= 0:
            await message.answer("❌ Вага має бути більшою за 0.")
            return

    except Exception:
        await message.answer(
            "❌ Напиши у форматі: <b>гречка 150</b>",
            reply_markup=get_keyboard(),
        )
        return

    if food_name not in FOOD_DB:
        available = ", ".join(sorted(list(FOOD_DB.keys())[:20]))
        await message.answer(
            "❌ Я поки не знаю цю їжу.\n\n"
            f"Спробуй щось із бази, наприклад: {available}",
            reply_markup=get_keyboard(),
        )
        return

    entry = calculate_macros(food_name, weight)
    user_data[message.from_user.id].append(entry)

    total = daily_totals(message.from_user.id)

    await message.answer(
        f"✅ Додано:\n"
        f"🍽 {entry['food']} {entry['weight']:.0f}г\n"
        f"🔥 {entry['kcal']:.0f} ккал\n"
        f"Б {entry['p']:.1f} / Ж {entry['f']:.1f} / В {entry['c']:.1f}\n\n"
        f"📊 Зараз за день: {total['kcal']:.0f} ккал",
        reply_markup=get_keyboard(),
    )


async def callback_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id

    if callback.data == "summary":
        await callback.message.answer(
            summary_text(user_id),
            reply_markup=get_keyboard(),
        )

    elif callback.data == "delete_last":
        if not user_data[user_id]:
            await callback.message.answer(
                "❌ Немає чого видаляти.",
                reply_markup=get_keyboard(),
            )
        else:
            deleted = user_data[user_id].pop()
            total = daily_totals(user_id)

            await callback.message.answer(
                f"↩️ Видалено останнє:\n"
                f"{deleted['food']} {deleted['weight']:.0f}г — {deleted['kcal']:.0f} ккал\n\n"
                f"📊 Тепер за день: {total['kcal']:.0f} ккал",
                reply_markup=get_keyboard(),
            )

    elif callback.data == "reset":
        user_data[user_id] = []
        await callback.message.answer(
            "🗑 День очищено.",
            reply_markup=get_keyboard(),
        )

    await callback.answer()


# -----------------------------
# Нотифікації
# -----------------------------

async def send_notification(bot: Bot, text: str) -> None:
    for user_id in list(user_data.keys()):
        try:
            await bot.send_message(user_id, text)
        except Exception:
            pass


# -----------------------------
# Головна функція
# -----------------------------

async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.message.register(start_handler, Command("start"))
    dp.callback_query.register(callback_handler)
    dp.message.register(text_handler, F.text)

    scheduler = AsyncIOScheduler(timezone=APP_TZ)

    scheduler.add_job(
        send_notification,
        "cron",
        hour=8,
        minute=0,
        args=[bot, "🌅 Йоу! Час сніданку! Що там?"],
    )
    scheduler.add_job(
        send_notification,
        "cron",
        hour=13,
        minute=0,
        args=[bot, "🌞 Йоу! Час обіду! Що там??"],
    )
    scheduler.add_job(
        send_notification,
        "cron",
        hour=19,
        minute=0,
        args=[bot, "🌙 Йоу! Час вечері! Що там?"],
    )

    scheduler.start()
    logging.info("Scheduler started")
    logging.info("Start polling")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
