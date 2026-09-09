import os
import json
import asyncio
from aiohttp import web
import re
from typing import Dict, List

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

TOKEN = os.environ.get("PARFERA_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан PARFERA_BOT_TOKEN")

with open("catalog.json", encoding="utf-8") as f:
    PRODUCTS = json.load(f)["products"]

# Быстрый индекс для поиска по названию и артикулу.
def norm(text: str) -> str:
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()

SEARCH_TEXT = {
    p["id"]: norm(f'{p.get("name", "")} {p.get("article", "")}')
    for p in PRODUCTS
}

PAGE_SIZE = 12
USER_SEARCH: Dict[int, str] = {}

class SearchState(StatesGroup):
    waiting = State()

dp = Dispatcher()


def home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="catalog"),
         InlineKeyboardButton(text="🔎 Поиск", callback_data="search")],
        [InlineKeyboardButton(text="⭐ Популярное", callback_data="popular"),
         InlineKeyboardButton(text="🆕 Новинки", callback_data="new")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart"),
         InlineKeyboardButton(text="👤 Консультант", callback_data="consultant")]
    ])


def back_home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")]
    ])


def rub(value):
    if value in (None, "", 0):
        return None
    return f"{int(value):,}".replace(",", " ") + " ₽"


def product_text(p):
    lines = [f'<b>{p["name"]}</b>']
    if p.get("volume"):
        lines.append(p["volume"])
    if p.get("article"):
        lines.append(f'Артикул: {p["article"]}')
    if p.get("bottle_price_rub"):
        lines.append(f'Флакон — <b>{rub(p["bottle_price_rub"])}</b>')
    if p.get("tester_price_rub"):
        lines.append(f'Тестер — <b>{rub(p["tester_price_rub"])}</b>')
    return "\n".join(lines)


def product_kb(pid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Добавить", callback_data=f"add:{pid}")],
        [InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")]
    ])


def search_results(query: str, page: int = 0):
    q = norm(query)
    # Все слова запроса должны встречаться в названии/артикуле.
    words = [w for w in q.split() if w]
    matches = [p for p in PRODUCTS if all(w in SEARCH_TEXT[p["id"]] for w in words)]
    start = page * PAGE_SIZE
    return matches, matches[start:start + PAGE_SIZE]


def results_kb(items: List[dict], page: int, total: int):
    rows = []
    for p in items:
        title = p["name"]
        if len(title) > 55:
            title = title[:52] + "…"
        rows.append([InlineKeyboardButton(text=title, callback_data=f'product:{p["id"]}')])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"page:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_search_results(target_message, user_id: int, page: int = 0):
    query = USER_SEARCH.get(user_id, "")
    matches, items = search_results(query, page)
    if not matches:
        text = f'🔎 По запросу «{query}» ничего не найдено.\n\nПопробуйте название бренда или аромата, например: <b>Erba Pura</b>.'
        await target_message.edit_text(text, reply_markup=back_home_kb())
        return
    pages = (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE
    text = f'🔎 Результаты поиска: <b>{len(matches)}</b>\nЗапрос: «{query}»\n\nВыберите товар:'
    await target_message.edit_text(text, reply_markup=results_kb(items, page, len(matches)))


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>PARFERA</b>\n\nНишевая парфюмерия и персональный подбор.\n\nВ каталоге загружен полный прайс поставщика.",
        reply_markup=home_kb()
    )


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("<b>PARFERA</b>\n\nНишевая парфюмерия и персональный подбор.", reply_markup=home_kb())
    await callback.answer()


@dp.callback_query(F.data == "catalog")
async def catalog(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting)
    await callback.message.edit_text(
        f"🛍 <b>Каталог PARFERA</b>\n\nВ каталоге <b>{len(PRODUCTS):,}</b> позиций из прайса.\n\nВведите название бренда, аромата или артикул:",
        reply_markup=back_home_kb()
    )
    await callback.answer()


@dp.callback_query(F.data == "search")
async def search(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting)
    await callback.message.edit_text(
        "🔎 <b>Поиск по каталогу</b>\n\nВведите название бренда, аромата или артикул.\n\nНапример: <b>Erba Pura</b>, <b>Amouage</b> или <b>000-002</b>.",
        reply_markup=back_home_kb()
    )
    await callback.answer()


@dp.message(SearchState.waiting)
async def do_search(message: Message, state: FSMContext):
    query = message.text.strip() if message.text else ""
    if not query:
        await message.answer("Введите текст для поиска.")
        return
    USER_SEARCH[message.from_user.id] = query
    await state.clear()
    matches, items = search_results(query, 0)
    if not matches:
        await message.answer(
            f'🔎 По запросу «{query}» ничего не найдено.\n\nПопробуйте более короткий запрос.',
            reply_markup=back_home_kb()
        )
        return
    await message.answer(
        f'🔎 Найдено: <b>{len(matches)}</b>\nЗапрос: «{query}»\n\nВыберите товар:',
        reply_markup=results_kb(items, 0, len(matches))
    )


@dp.callback_query(F.data.startswith("page:"))
async def page(callback: CallbackQuery):
    page_num = int(callback.data.split(":")[1])
    await show_search_results(callback.message, callback.from_user.id, page_num)
    await callback.answer()


@dp.callback_query(F.data.startswith("product:"))
async def product(callback: CallbackQuery):
    pid = callback.data.split(":", 1)[1]
    p = next((x for x in PRODUCTS if x["id"] == pid), None)
    if not p:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await callback.message.edit_text(product_text(p), reply_markup=product_kb(pid))
    await callback.answer()


@dp.callback_query(F.data.startswith("add:"))
async def add(callback: CallbackQuery):
    await callback.answer("Товар добавлен в тестовую корзину", show_alert=True)


@dp.callback_query(F.data == "popular")
async def popular(callback: CallbackQuery):
    await callback.message.edit_text("⭐ <b>Популярное</b>\n\nРаздел подключим следующим этапом.", reply_markup=back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "new")
async def new(callback: CallbackQuery):
    await callback.message.edit_text("🆕 <b>Новинки</b>\n\nРаздел подключим следующим этапом.", reply_markup=back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "cart")
async def cart(callback: CallbackQuery):
    await callback.message.edit_text("🛒 <b>Корзина</b>\n\nПолноценную корзину подключим следующим этапом.", reply_markup=back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "consultant")
async def consultant(callback: CallbackQuery):
    await callback.message.edit_text("👤 <b>Консультант</b>\n\nНапишите, какой аромат ищете, и мы подберём варианты.", reply_markup=back_home_kb())
    await callback.answer()


async def health(request: web.Request):
    return web.Response(text="OK")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"HTTP server started on 0.0.0.0:{port}")
    return runner


async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    runner = None
    try:
        # Render may leave an old Telegram webhook from a previous deployment.
        # Remove it before switching this bot to long polling.
        await bot.delete_webhook(drop_pending_updates=True)
        runner = await run_web_server()
        print("Starting Telegram long polling...")
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
