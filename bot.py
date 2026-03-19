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

user_data = defaultdict(list)

# КБЖУ на 100 г
FOOD_DB = {
    # Крупи / гарніри
    "гречка": {"kcal": 92, "p": 3.4, "f": 0.6, "c": 19.9},
    "греча": {"kcal": 92, "p": 3.4, "f": 0.6, "c": 19.9},
    "рис": {"kcal": 130, "p": 2.7, "f": 0.3, "c": 28.2},
    "макарони": {"kcal": 131, "p": 5.0, "f": 1.1, "c": 25.0},
    "вівсянка": {"kcal": 68, "p": 2.4, "f": 1.4, "c": 12.0},
    "картопля": {"kcal": 77, "p": 2.0, "f": 0.1, "c": 17.5},
    "пюре": {"kcal": 88, "p": 1.7, "f": 3.4, "c": 13.8},

    # М'ясо / білок
    "курка": {"kcal": 165, "p": 31.0, "f": 3.6, "c": 0.0},
    "грудка": {"kcal": 165, "p": 31.0, "f": 3.6, "c": 0.0},
    "куряча грудка": {"kcal": 165, "p": 31.0, "f": 3.6, "c": 0.0},
    "крильця": {"kcal": 203, "p": 30.5, "f": 8.1, "c": 0.0},

    # Яйця
    "яйце": {"kcal": 143, "p": 12.6, "f": 9.5, "c": 0.7},
    "яйця": {"kcal": 143, "p": 12.6, "f": 9.5, "c": 0.7},

    # Овочі
    "огірок": {"kcal": 15, "p": 0.7, "f": 0.1, "c": 3.6},
    "огірки": {"kcal": 15, "p": 0.7, "f": 0.1, "c": 3.6},

    # Фрукти
    "банан": {"kcal": 89, "p": 1.1, "f": 0.3, "c": 22.8},
    "банани": {"kcal": 89, "p": 1.1, "f": 0.3, "c": 22.8},
    "яблуко": {"kcal": 52, "p": 0.3, "f": 0.2, "c": 13.8},
    "яблука": {"kcal": 52, "p": 0.3, "f": 0.2, "c": 13.8},

    # Хліб / базове
    "хліб": {"kcal": 265, "p": 9.0, "f": 3.2, "c": 49.0},

    # Готові / приблизні
    "кукурудза": {"kcal": 86, "p": 3.3, "f": 1.4, "c": 18.7},
    "крабові": {"kcal": 95, "p": 7.0, "f": 1.0, "c": 15.0},
    "крабпалички": {"kcal": 95, "p": 7.0, "f": 1.0, "c": 15.0},
    "крабові палички": {"kcal": 95, "p": 7.0, "f": 1.0, "c": 15.0},

    # Делікатеси / ковбаси — приблизно
    "хамон": {"kcal": 241, "p": 31.0, "f": 13.0, "c": 0.0},
    "прошуто": {"kcal": 250, "p": 26.0, "f": 17.0, "c": 0.0},
    "кабаноси": {"kcal": 400, "p": 25.0, "f": 35.0, "c": 2.0},
    "кабанос": {"kcal": 400, "p": 25.0, "f": 35.0, "c": 2.0},

    # Локшина швидкого приготування — приблизно
    "мівіна": {"kcal": 450, "p": 9.0, "f": 17.0, "c": 63.0},
}

# Синоніми назв
ALIASES = {
    "кабанос": "кабаноси",
    "kabanosy": "кабаноси",
    "крабові палички": "крабові палички",
    "крабові": "крабові",
    "яєць": "яйця",
    "банани": "банан",
    "яблука": "яблуко",
    "огірки": "огірок",
}

# Середня вага 1 штуки у грамах
UNIT_WEIGHTS = {
    "яйце": 50,
    "яйця": 50,
    "банан": 120,
    "банани": 120,
    "яблуко": 180,
    "яблука": 180,
    "огірок": 120,
    "огірки": 120,
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

def normalize_food_name(food_name: str) -> str:
    food_name = food_name.strip().lower()
    food_name = " ".join(food_name.split())
    return ALIASES.get(food_name, food_name)


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


def parse_input(text: str):
    parts = text.strip().lower().split()

    if len(parts) < 2:
        raise ValueError("bad format")

    # ВАРІАНТ 1: "гречка 150"
    try:
        weight = float(parts[-1].replace(",", "."))
        food_name = " ".join(parts[:-1])
        food_name = normalize_food_name(food_name)
        return food_name, weight
    except ValueError:
        pass

    # ВАРІАНТ 2: "2 яйця"
    try:
        count = float(parts[0].replace(",", "."))
        food_name = " ".join(parts[1:])
        food_name = normalize_food_name(food_name)

        if food_name not in UNIT_WEIGHTS:
            raise ValueError("no unit weight")

        weight = count * UNIT_WEIGHTS[food_name]
        return food_name, weight
    except ValueError:
        raise ValueError("bad format")


# -----------------------------
# Хендлери
# -----------------------------

async def start_handler(message: Message) -> None:
    await message.answer(
        "Йоу 👋\n\n"
        "Пиши так:\n"
        "<b>гречка 150</b>\n"
        "або\n"
        "<b>2 яйця</b>\n\n"
        "Я порахую калорії, білки, жири і вуглеводи.",
        reply_markup=get_keyboard(),
    )


async def text_handler(message: Message) -> None:
    text = (message.text or "").strip().lower()

    if not text:
        return

    try:
        food_name, weight = parse_input(text)

        if weight <= 0:
            await message.answer("❌ Кількість або вага має бути більшою за 0.", reply_markup=get_keyboard())
            return

    except Exception:
        await message.answer(
            "❌ Напиши у форматі:\n"
            "<b>гречка 150</b>\n"
            "або\n"
            "<b>2 яйця</b>",
            reply_markup=get_keyboard(),
        )
        return

    if food_name not in FOOD_DB:
        await message.answer(
            "❌ Я поки не знаю цю їжу.",
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
