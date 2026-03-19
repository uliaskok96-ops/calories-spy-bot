import asyncio
import logging
import os
from datetime import datetime
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from apscheduler.schedulers.asyncio import AsyncIOScheduler


# -----------------------------
# Дані (в памʼяті)
# -----------------------------

user_data = defaultdict(list)

FOOD_DB = {
    "гречка": {"kcal": 110, "p": 4, "f": 1, "c": 21},
    "курка": {"kcal": 165, "p": 31, "f": 3.6, "c": 0},
    "яйце": {"kcal": 155, "p": 13, "f": 11, "c": 1},
    "мівіна": {"kcal": 250, "p": 6, "f": 10, "c": 30},
}


# -----------------------------
# Клавіатура
# -----------------------------

def get_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Підсумок", callback_data="summary")],
        [InlineKeyboardButton(text="🗑 Очистити день", callback_data="reset")]
    ])


# -----------------------------
# /start
# -----------------------------

async def start(message: Message):
    await message.answer(
        "Йоу 👋\n\n"
        "Пиши так:\n"
        "<b>гречка 150</b>\n\n"
        "Я порахую калорії 🔥",
        reply_markup=get_keyboard()
    )


# -----------------------------
# Обробка тексту
# -----------------------------

async def handle_text(message: Message):
    try:
        parts = message.text.lower().split()
        food = parts[0]
        weight = float(parts[1])
    except:
        await message.answer("❌ Напиши нормально: <b>гречка 150</b>")
        return

    if food not in FOOD_DB:
        await message.answer("❌ Я не знаю цю їжу")
        return

    data = FOOD_DB[food]

    kcal = data["kcal"] * weight / 100
    p = data["p"] * weight / 100
    f = data["f"] * weight / 100
    c = data["c"] * weight / 100

    user_data[message.from_user.id].append({
        "food": food,
        "kcal": kcal
    })

    await message.answer(
        f"🍽 {food} {weight}г\n"
        f"🔥 {kcal:.0f} ккал\n"
        f"Б {p:.1f} / Ж {f:.1f} / В {c:.1f}",
        reply_markup=get_keyboard()
    )


# -----------------------------
# Кнопки
# -----------------------------

async def callbacks(callback: CallbackQuery):
    user_id = callback.from_user.id

    if callback.data == "summary":
        total = sum(item["kcal"] for item in user_data[user_id])
        await callback.message.answer(f"📊 Сьогодні: {total:.0f} ккал")

    elif callback.data == "reset":
        user_data[user_id] = []
        await callback.message.answer("🗑 День очищено")

    await callback.answer()


# -----------------------------
# Нотифікації
# -----------------------------

async def send_notification(bot: Bot, text: str):
    for user_id in user_data.keys():
        try:
            await bot.send_message(user_id, text)
        except:
            pass


# -----------------------------
# main
# -----------------------------

async def main():
    logging.basicConfig(level=logging.INFO)

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN not set")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.register(start, Command("start"))
    dp.message.register(handle_text, F.text)
    dp.callback_query.register(callbacks)

    scheduler = AsyncIOScheduler()

    scheduler.add_job(send_notification, "cron", hour=8, args=[bot, "🌅 Йоу! Час сніданку! Що там?"])
    scheduler.add_job(send_notification, "cron", hour=13, args=[bot, "🌞 Йоу! Час обіду! Що там??"])
    scheduler.add_job(send_notification, "cron", hour=19, args=[bot, "🌙 Йоу! Час вечері! Що там?"])

    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
