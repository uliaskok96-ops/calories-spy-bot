"""
Telegram calorie counter bot (aiogram 3.x) with in-memory per-user storage + APScheduler notifications.

Key properties:
- No DB, no files: all user data lives in RAM of this process.
- Multi-user: state is keyed by Telegram user_id.
- Concurrency-safe: per-user asyncio.Lock protects from race conditions.
- Scheduled notifications: 08:00 / 13:00 / 19:00 in server timezone.

Tested against:
- aiogram 3.26.0
- APScheduler 3.11.2
Python: 3.10+
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, date, tzinfo
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

try:
    # Python 3.9+ standard library
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# -----------------------------
# Configuration (edit as needed)
# -----------------------------

MAX_GRAMS: float = 5_000.0          # Hard limit to prevent absurd inputs / typos
MAX_ENTRIES_PER_DAY: int = 80       # Prevent unbounded keyboards/messages
MAX_INLINE_BUTTONS: int = 60        # Telegram UI safety margin

BTN_TODAY = "📋 Сьогодні"
BTN_RESET = "🔄 Скинути день"
BTN_HELP = "ℹ️ Допомога"
BTN_CANCEL = "❌ Скасувати"

NOTIFY_TEXT_08 = "🌅 08:00. Доброго ранку! Запиши сніданок: <i>назва вага_г</i> (наприклад: <code>гречка 150</code>)."
NOTIFY_TEXT_13 = "🍽️ 13:00. Час обіду. Додай їжу, якщо ще не додав(ла), і перевір підсумок кнопкою «📋 Сьогодні»."
NOTIFY_TEXT_19 = "🌙 19:00. Вечірній чек: чи все записано? Підсумок дня — кнопка «📋 Сьогодні». Можна скинути день кнопкою «🔄»."

ENTRY_RE = re.compile(
    r"^\s*(?P<name>.+?)\s+(?P<grams>\d+(?:[.,]\d+)?)\s*(?:г|гр|g)?\s*$",
    flags=re.IGNORECASE,
)

MACROS_RE = re.compile(
    r"^\s*(?P<kcal>\d+(?:[.,]\d+)?)\s+(?P<p>\d+(?:[.,]\d+)?)\s+(?P<f>\d+(?:[.,]\d+)?)\s+(?P<c>\d+(?:[.,]\d+)?)\s*$"
)


# -----------------------------
# Nutrition model
# -----------------------------

@dataclass(frozen=True, slots=True)
class Per100g:
    kcal: float
    protein: float
    fat: float
    carbs: float


@dataclass(slots=True)
class Entry:
    id: int
    name: str
    grams: float
    kcal: float
    protein: float
    fat: float
    carbs: float


@dataclass(slots=True)
class PendingUnknown:
    name_norm: str
    name_display: str
    grams: float


@dataclass(slots=True)
class UserData:
    day: date
    next_id: int = 1
    entries: List[Entry] = field(default_factory=list)
    custom_foods: Dict[str, Per100g] = field(default_factory=dict)
    pending_unknown: Optional[PendingUnknown] = None


# Built-in foods (example baseline; adjust for your needs)
# Keys MUST be normalized (lowercase, trimmed, single spaces).
BUILTIN_FOODS: Dict[str, Per100g] = {
    "гречка (варена)": Per100g(kcal=92, protein=3.4, fat=0.6, carbs=20.0),
    "рис (варений)": Per100g(kcal=130, protein=2.4, fat=0.3, carbs=28.0),
    "вівсянка (суха)": Per100g(kcal=379, protein=13.2, fat=6.5, carbs=67.7),
    "куряча грудка (готова)": Per100g(kcal=165, protein=31.0, fat=3.6, carbs=0.0),
    "яйце (ціле)": Per100g(kcal=143, protein=12.6, fat=9.5, carbs=0.7),
    "банан": Per100g(kcal=89, protein=1.1, fat=0.3, carbs=22.8),
}


# -----------------------------
# In-memory store (concurrency-safe)
# -----------------------------

class InMemoryStore:
    """
    In-process RAM store:
    - users[user_id] -> UserData
    - per-user asyncio.Lock to guard mutations of that user's data
    - global lock to guard users/locks dict structure
    """

    def __init__(self) -> None:
        self._users: Dict[int, UserData] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _today(self, tz: tzinfo) -> date:
        return datetime.now(tz).date()

    async def list_user_ids(self) -> List[int]:
        async with self._global_lock:
            return list(self._users.keys())

    async def drop_user(self, user_id: int) -> None:
        async with self._global_lock:
            self._users.pop(user_id, None)
            self._locks.pop(user_id, None)

    @asynccontextmanager
    async def user_ctx(self, user_id: int, tz: tzinfo) -> UserData:
        async with self._global_lock:
            if user_id not in self._users:
                self._users[user_id] = UserData(day=self._today(tz))
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            lock = self._locks[user_id]
            user = self._users[user_id]

        async with lock:
            # Day rollover
            today = self._today(tz)
            if user.day != today:
                user.day = today
                user.next_id = 1
                user.entries.clear()
                user.pending_unknown = None
            yield user


# -----------------------------
# Helpers
# -----------------------------

def normalize_food_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_RESET)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши: назва вага_г (напр. гречка 150)",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        input_field_placeholder="Надішли: ккал білки жири вуглеводи (на 100г)",
    )


def compute_entry(per100: Per100g, grams: float) -> Tuple[float, float, float, float]:
    k = grams / 100.0
    return (
        per100.kcal * k,
        per100.protein * k,
        per100.fat * k,
        per100.carbs * k,
    )


def totals(entries: List[Entry]) -> Tuple[float, float, float, float]:
    kcal = sum(e.kcal for e in entries)
    p = sum(e.protein for e in entries)
    f = sum(e.fat for e in entries)
    c = sum(e.carbs for e in entries)
    return kcal, p, f, c


def fmt_num(x: float) -> str:
    # Pretty formatting: 1 decimal but strip trailing .0
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def render_today(user: UserData) -> Tuple[str, Optional[ReplyKeyboardMarkup], Optional[object]]:
    """
    Build text + inline keyboard for today's entries.
    Return: (text, reply_kb, inline_kb)
    """
    if not user.entries:
        text = (
            "📋 <b>Сьогодні поки що порожньо</b>\n\n"
            "Додай запис форматом: <code>назва вага_г</code>\n"
            "Приклад: <code>гречка (варена) 150</code>"
        )
        return text, menu_keyboard(), None

    kcal, p, f, c = totals(user.entries)

    lines = [f"📋 <b>Сьогодні ({user.day.isoformat()})</b>"]
    for e in user.entries[-MAX_ENTRIES_PER_DAY:]:
        lines.append(
            f"{e.id}) <b>{e.name}</b> — {fmt_num(e.grams)} г\n"
            f"   {fmt_num(e.kcal)} ккал | Б {fmt_num(e.protein)} г | Ж {fmt_num(e.fat)} г | В {fmt_num(e.carbs)} г"
        )

    if len(user.entries) > MAX_ENTRIES_PER_DAY:
        lines.append(f"\n… показано останні {MAX_ENTRIES_PER_DAY} записів з {len(user.entries)}")

    lines.append(
        "\n<b>Разом за день:</b> "
        f"{fmt_num(kcal)} ккал | Б {fmt_num(p)} г | Ж {fmt_num(f)} г | В {fmt_num(c)} г"
    )

    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    # Delete buttons (short callback_data: 'del:<id>')
    shown_entries = user.entries[-min(MAX_ENTRIES_PER_DAY, MAX_INLINE_BUTTONS):]
    for e in shown_entries:
        builder.button(text=f"❌ Видалити {e.id}", callback_data=f"del:{e.id}")

    builder.button(text="🔄 Скинути день", callback_data="reset_day")
    builder.adjust(3)  # 3 buttons per row, then wraps
    return text, menu_keyboard(), builder.as_markup()


def parse_entry_text(text: str) -> Tuple[str, float]:
    """
    Parse '<name> <grams>' where grams is last token, can have ',' or '.' and optional 'г/гр/g'.
    Supports multi-word name.
    """
    m = ENTRY_RE.match(text)
    if not m:
        raise ValueError("bad_format")

    name = m.group("name").strip()
    grams_raw = m.group("grams").replace(",", ".")
    grams = float(grams_raw)
    return name, grams


def parse_macros_text(text: str) -> Per100g:
    """
    Parse 'kcal protein fat carbs' for 100g.
    Example: '343 12.6 3.4 71.5'
    """
    m = MACROS_RE.match(text)
    if not m:
        raise ValueError("bad_macros_format")

    def fnum(key: str) -> float:
        return float(m.group(key).replace(",", "."))

    per = Per100g(
        kcal=fnum("kcal"),
        protein=fnum("p"),
        fat=fnum("f"),
        carbs=fnum("c"),
    )
    # Basic sanity checks
    if not (0 <= per.kcal <= 1000):
        raise ValueError("kcal_range")
    for v in (per.protein, per.fat, per.carbs):
        if not (0 <= v <= 200):
            raise ValueError("macro_range")
    return per


def get_app_timezone() -> tzinfo:
    """
    Server-local timezone by default; can be overridden with APP_TZ='Europe/Kyiv' (IANA name).
    """
    tz_name = os.getenv("APP_TZ", "").strip()
    if tz_name:
        if ZoneInfo is None:
            raise RuntimeError("ZoneInfo is not available (Python 3.9+ required).")
        return ZoneInfo(tz_name)
    # Use OS-configured local timezone
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


# -----------------------------
# Scheduler jobs
# -----------------------------

async def broadcast(bot: Bot, store: InMemoryStore, text: str) -> None:
    """
    Send a message to all users known to this in-memory process.
    If a user blocked the bot, drop them from memory.
    """
    user_ids = await store.list_user_ids()
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
        except TelegramRetryAfter as e:
            # Flood control: wait and continue
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            # User blocked bot / kicked it: remove from our memory store
            await store.drop_user(uid)
        except TelegramBadRequest:
            # Chat not found or similar: also drop to avoid endless errors
            await store.drop_user(uid)


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot, store: InMemoryStore, tz: tzinfo) -> None:
    """
    Register 3 daily jobs. We recreate them at every startup (in-memory).
    """
    scheduler.add_job(
        broadcast,
        trigger=CronTrigger(hour=8, minute=0, timezone=tz),
        args=(bot, store, NOTIFY_TEXT_08),
        id="notify_08",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 30,
    )
    scheduler.add_job(
        broadcast,
        trigger=CronTrigger(hour=13, minute=0, timezone=tz),
        args=(bot, store, NOTIFY_TEXT_13),
        id="notify_13",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 30,
    )
    scheduler.add_job(
        broadcast,
        trigger=CronTrigger(hour=19, minute=0, timezone=tz),
        args=(bot, store, NOTIFY_TEXT_19),
        id="notify_19",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 30,
    )


# -----------------------------
# Handlers
# -----------------------------

async def cmd_start(message: Message, store: InMemoryStore, tz: tzinfo) -> None:
    # Ensure user exists in memory
    async with store.user_ctx(message.from_user.id, tz):
        pass

    text = (
        "Привіт! 👋 Я бот для підрахунку калорій та БЖВ.\n\n"
        "✅ Формат введення:\n"
        "  <code>назва вага_г</code>\n"
        "Приклад:\n"
        "  <code>гречка (варена) 150</code>\n\n"
        "📌 Порада: натисни «📋 Сьогодні», щоб побачити список і видалити окремі позиції."
    )
    await message.answer(text, reply_markup=menu_keyboard())


async def show_help(message: Message) -> None:
    text = (
        "ℹ️ <b>Довідка</b>\n\n"
        "1) Додати страву/продукт:\n"
        "   <code>назва вага_г</code>\n"
        "   Напр.: <code>банан 120</code>\n\n"
        "2) Якщо продукт невідомий — бот попросить БЖВ на 100г:\n"
        "   <code>ккал білки жири вуглеводи</code>\n"
        "   Напр.: <code>343 12.6 3.4 71.5</code>\n\n"
        "3) Переглянути день і видалити позиції: кнопка «📋 Сьогодні».\n"
        "4) Скинути день: кнопка «🔄 Скинути день» або інлайн-кнопка в списку.\n\n"
        f"⚠️ Обмеження ваги: 1 … {int(MAX_GRAMS)} г."
    )
    await message.answer(text, reply_markup=menu_keyboard())


async def show_today(message: Message, store: InMemoryStore, tz: tzinfo) -> None:
    async with store.user_ctx(message.from_user.id, tz) as user:
        text, reply_kb, inline_kb = render_today(user)
    await message.answer(text, reply_markup=inline_kb or reply_kb)


async def reset_day(message: Message, store: InMemoryStore, tz: tzinfo) -> None:
    async with store.user_ctx(message.from_user.id, tz) as user:
        user.entries.clear()
        user.next_id = 1
        user.pending_unknown = None
    await message.answer("🔄 День скинуто. Можеш починати заново.", reply_markup=menu_keyboard())


async def on_callback(call: CallbackQuery, store: InMemoryStore, tz: tzinfo) -> None:
    """
    Handles:
    - del:<id>
    - reset_day
    """
    data = (call.data or "").strip()
    uid = call.from_user.id

    if data == "reset_day":
        async with store.user_ctx(uid, tz) as user:
            user.entries.clear()
            user.next_id = 1
            user.pending_unknown = None
            text, _, inline_kb = render_today(user)
        await call.answer("День скинуто ✅")
        if call.message:
            try:
                await call.message.edit_text(text, reply_markup=inline_kb)
            except TelegramBadRequest:
                # Message cannot be edited (too old / same content etc.)
                await call.message.answer(text, reply_markup=inline_kb)
        return

    if data.startswith("del:"):
        try:
            entry_id = int(data.split(":", 1)[1])
        except ValueError:
            await call.answer("Некоректний ID", show_alert=True)
            return

        async with store.user_ctx(uid, tz) as user:
            before = len(user.entries)
            user.entries = [e for e in user.entries if e.id != entry_id]
            removed = (len(user.entries) != before)
            text, _, inline_kb = render_today(user)

        await call.answer("Видалено ✅" if removed else "Не знайдено", show_alert=False)
        if call.message:
            try:
                await call.message.edit_text(text, reply_markup=inline_kb)
            except TelegramBadRequest:
                await call.message.answer(text, reply_markup=inline_kb)
        return

    await call.answer("Невідома дія", show_alert=True)


async def on_text_message(message: Message, store: InMemoryStore, tz: tzinfo) -> None:
    """
    Main text handler:
    - If user is in "pending unknown food" state: parse macros and continue.
    - Else: parse '<name> <grams>' and add entry.
    """
    text_raw = (message.text or "").strip()
    if not text_raw:
        return

    # Ignore commands
    if text_raw.startswith("/"):
        await show_help(message)
        return

    uid = message.from_user.id

    async with store.user_ctx(uid, tz) as user:
        # Cancel pending
        if user.pending_unknown and text_raw == BTN_CANCEL:
            user.pending_unknown = None
            await message.answer("Скасовано. Можеш додати інший продукт.", reply_markup=menu_keyboard())
            return

        # If waiting for macros for unknown food
        if user.pending_unknown:
            try:
                per = parse_macros_text(text_raw)
            except ValueError as e:
                await message.answer(
                    "❗ Формат невірний.\n"
                    "Надішли 4 числа на 100г: <code>ккал білки жири вуглеводи</code>\n"
                    "Напр.: <code>343 12.6 3.4 71.5</code>\n"
                    "Або натисни «❌ Скасувати».",
                    reply_markup=cancel_keyboard(),
                )
                return

            # Save as per-user custom food and create entry immediately
            pending = user.pending_unknown
            user.custom_foods[pending.name_norm] = per

            kcal, p, f, c = compute_entry(per, pending.grams)
            entry = Entry(
                id=user.next_id,
                name=pending.name_display,
                grams=pending.grams,
                kcal=kcal,
                protein=p,
                fat=f,
                carbs=c,
            )
            user.next_id += 1
            user.entries.append(entry)
            user.pending_unknown = None

            total_k, total_p, total_f, total_c = totals(user.entries)
            await message.answer(
                "✅ Додано запис:\n"
                f"<b>{entry.name}</b> — {fmt_num(entry.grams)} г\n"
                f"{fmt_num(entry.kcal)} ккал | Б {fmt_num(entry.protein)} г | Ж {fmt_num(entry.fat)} г | В {fmt_num(entry.carbs)} г\n\n"
                "<b>Разом за день:</b> "
                f"{fmt_num(total_k)} ккал | Б {fmt_num(total_p)} г | Ж {fmt_num(total_f)} г | В {fmt_num(total_c)} г",
                reply_markup=menu_keyboard(),
            )
            return

        # Normal mode: parse entry
        try:
            name, grams = parse_entry_text(text_raw)
        except ValueError:
            await message.answer(
                "❗ Не зрозумів формат.\n"
                "Надішли: <code>назва вага_г</code>\n"
                "Наприклад: <code>яйце (ціле) 100</code>",
                reply_markup=menu_keyboard(),
            )
            return

        if grams <= 0 or grams > MAX_GRAMS:
            await message.answer(
                f"❗ Вага має бути в межах 1…{int(MAX_GRAMS)} г. Ти надіслав(ла): {grams}",
                reply_markup=menu_keyboard(),
            )
            return

        if len(user.entries) >= MAX_ENTRIES_PER_DAY:
            await message.answer(
                f"❗ Забагато записів за день (ліміт {MAX_ENTRIES_PER_DAY}). "
                "Щоб продовжити — скинь день кнопкою «🔄».",
                reply_markup=menu_keyboard(),
            )
            return

        name_norm = normalize_food_name(name)

        per = user.custom_foods.get(name_norm) or BUILTIN_FOODS.get(name_norm)
        if per is None:
            # Unknown food flow
            user.pending_unknown = PendingUnknown(
                name_norm=name_norm,
                name_display=name.strip(),
                grams=grams,
            )
            await message.answer(
                "🤔 Я не знаю цей продукт.\n\n"
                f"Продукт: <b>{name.strip()}</b>\n"
                f"Вага: <b>{fmt_num(grams)} г</b>\n\n"
                "Надішли БЖВ на 100г у форматі:\n"
                "<code>ккал білки жири вуглеводи</code>\n"
                "Напр.: <code>343 12.6 3.4 71.5</code>\n\n"
                "Або натисни «❌ Скасувати».",
                reply_markup=cancel_keyboard(),
            )
            return

        kcal, p, f, c = compute_entry(per, grams)
        entry = Entry(
            id=user.next_id,
            name=name.strip(),
            grams=grams,
            kcal=kcal,
            protein=p,
            fat=f,
            carbs=c,
        )
        user.next_id += 1
        user.entries.append(entry)

        total_k, total_p, total_f, total_c = totals(user.entries)

    # Reply outside the lock (store mutation already done)
    await message.answer(
        "✅ Додано:\n"
        f"<b>{entry.name}</b> — {fmt_num(entry.grams)} г\n"
        f"{fmt_num(entry.kcal)} ккал | Б {fmt_num(entry.protein)} г | Ж {fmt_num(entry.fat)} г | В {fmt_num(entry.carbs)} г\n\n"
        "<b>Разом за день:</b> "
        f"{fmt_num(total_k)} ккал | Б {fmt_num(total_p)} г | Ж {fmt_num(total_f)} г | В {fmt_num(total_c)} г",
        reply_markup=menu_keyboard(),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(show_today, F.text == BTN_TODAY)
    dp.message.register(reset_day, F.text == BTN_RESET)
    dp.message.register(show_help, F.text == BTN_HELP)

    dp.message.register(show_today, Command("today"))
    dp.message.register(reset_day, Command("reset"))
    dp.message.register(show_help, Command("help"))

    dp.callback_query.register(on_callback)
    dp.message.register(on_text_message, F.text)


# -----------------------------
# Entry point
# -----------------------------

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("8798461449:AAErcSbY3Wk5ModnMuzW-UO1Sw8bl6JGQWA")

    tz = get_app_timezone()

    # aiogram bot with default HTML parse mode
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    store = InMemoryStore()
    scheduler = AsyncIOScheduler(timezone=tz)

    # inject dependencies into aiogram context
    dp = Dispatcher(store=store, scheduler=scheduler, tz=tz)
    register_handlers(dp)

    # Optional: drop pending updates on startup (production convenience)
    await bot.delete_webhook(drop_pending_updates=True)

    setup_scheduler(scheduler, bot, store, tz)
    scheduler.start()

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
