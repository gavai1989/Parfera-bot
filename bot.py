import os
import json
import asyncio
import re
from typing import Dict, List
from aiohttp import web

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

# Demo product photo. Replace later with supplier/authorized commercial images.
VERSACE_EROS_IMAGE = "https://tiendacdn.farmashop.com.uy/media/catalog/product/cache/f8158826193ba5faa8b862a9bd1eb9e9/8/8/88188_77.jpg"

for p in PRODUCTS:
    n = p.get("name", "").lower()
    if "versace eros edt (m) 100ml" in n:
        p["image_url"] = VERSACE_EROS_IMAGE


def norm(text: str) -> str:
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()

SEARCH_TEXT = {p["id"]: norm(f'{p.get("name", "")} {p.get("article", "")}') for p in PRODUCTS}
BY_ID = {p["id"]: p for p in PRODUCTS}
PAGE_SIZE = 10
USER_SEARCH: Dict[int, str] = {}
CARTS: Dict[int, List[dict]] = {}

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
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="home")]])


def rub(value):
    if value in (None, "", 0):
        return None
    return f"{int(value):,}".replace(",", " ") + " ₽"


def display_name(p):
    name = p.get("name", "")
    name = re.sub(r"\s+TESTER$", "", name, flags=re.I)
    return name


def product_text(p):
    lines = [f'<b>{display_name(p)}</b>']
    lines.append("✨ Оригинальная парфюмерия")
    if p.get("volume"):
        lines.append(f'Объём: <b>{p["volume"]}</b>')
    if p.get("article"):
        lines.append(f'Артикул: {p["article"]}')
    if p.get("bottle_price_rub"):
        lines.append(f'Флакон — <b>{rub(p["bottle_price_rub"])}</b>')
    if p.get("tester_price_rub"):
        lines.append(f'Тестер — <b>{rub(p["tester_price_rub"])}</b>')
    if "VERSACE EROS EDT" in display_name(p).upper():
        lines += ["", "Свежий, яркий и чувственный аромат с мятой, зелёным яблоком, лимоном, ванилью и древесными нотами."]
    return "\n".join(lines)


def product_kb(pid):
    p = BY_ID[pid]
    rows = []
    if p.get("bottle_price_rub"):
        rows.append([InlineKeyboardButton(text=f"🧴 Флакон — {rub(p['bottle_price_rub'])}", callback_data=f"add:{pid}:bottle")])
    if p.get("tester_price_rub"):
        rows.append([InlineKeyboardButton(text=f"🧪 Тестер — {rub(p['tester_price_rub'])}", callback_data=f"add:{pid}:tester")])
    rows.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")])
    rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results(query: str, page: int = 0):
    words = [w for w in norm(query).split() if w]
    matches = [p for p in PRODUCTS if all(w in SEARCH_TEXT[p["id"]] for w in words)]
    start = page * PAGE_SIZE
    return matches, matches[start:start + PAGE_SIZE]


def results_kb(items: List[dict], page: int, total: int):
    rows = []
    for p in items:
        title = display_name(p)
        if len(title) > 48:
            title = title[:45] + "…"
        rows.append([InlineKeyboardButton(text=title, callback_data=f'product:{p["id"]}')])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"page:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")])
    rows.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def versace_items():
    return [p for p in PRODUCTS if norm(p.get("name", "")).startswith("versace ") and not p.get("tester")][:30]


def versace_kb(items):
    rows = []
    for p in items:
        title = display_name(p)
        rows.append([InlineKeyboardButton(text=title[:52], callback_data=f"product:{p['id']}")])
    rows += [
        [InlineKeyboardButton(text="🔎 Поиск по каталогу", callback_data="search")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cart_text(uid: int):
    items = CARTS.get(uid, [])
    if not items:
        return "🛒 <b>Ваша корзина пуста</b>\n\nДобавьте аромат из каталога."
    total = sum(x["price"] * x["qty"] for x in items)
    lines = [f"🛒 <b>Ваша корзина</b>\n{len(items)} позиция(й)\n"]
    for i, x in enumerate(items, 1):
        lines.append(f"{i}. {x['name']}\n   {x['type']} · {x['volume']} · {x['qty']} × {rub(x['price'])}")
    lines.append(f"\n<b>Итого: {rub(total)}</b>")
    return "\n".join(lines)


def cart_kb(uid: int):
    items = CARTS.get(uid, [])
    rows = []
    for i, x in enumerate(items):
        rows.append([
            InlineKeyboardButton(text="−", callback_data=f"qty:{i}:-1"),
            InlineKeyboardButton(text=f"{i+1}: {x['qty']}", callback_data="noop"),
            InlineKeyboardButton(text="+", callback_data=f"qty:{i}:1"),
            InlineKeyboardButton(text="🗑", callback_data=f"del:{i}")
        ])
    if items:
        rows.append([InlineKeyboardButton(text="📦 Оформить заказ", callback_data="checkout")])
    rows.append([InlineKeyboardButton(text="← Продолжить покупки", callback_data="catalog")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_search_results(target_message, user_id: int, page: int = 0):
    query = USER_SEARCH.get(user_id, "")
    matches, items = search_results(query, page)
    if not matches:
        await target_message.edit_text(f'🔎 По запросу «{query}» ничего не найдено.\n\nПопробуйте название бренда или аромата.', reply_markup=back_home_kb())
        return
    await target_message.edit_text(f'🔎 Найдено: <b>{len(matches)}</b>\nЗапрос: «{query}»\n\nВыберите товар:', reply_markup=results_kb(items, page, len(matches)))


async def send_product(message, p):
    text = product_text(p)
    kb = product_kb(p["id"])
    image = p.get("image_url")
    if image:
        try:
            await message.answer_photo(photo=image, caption=text, reply_markup=kb)
            return
        except Exception as e:
            print(f"Photo send failed for {p['id']}: {e}")
    await message.answer(text, reply_markup=kb)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("<b>PARFERA</b>\n\nНишевая парфюмерия и персональный подбор.\n\nВыберите раздел:", reply_markup=home_kb())


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("<b>PARFERA</b>\n\nНишевая парфюмерия и персональный подбор.\n\nВыберите раздел:", reply_markup=home_kb())
    await callback.answer()


@dp.callback_query(F.data == "catalog")
async def catalog(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="VERSACE", callback_data="brand:versace")],
        [InlineKeyboardButton(text="🔎 Найти аромат", callback_data="search")],
        [InlineKeyboardButton(text="📚 Весь каталог", callback_data="catalog_search")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")]
    ])
    await callback.message.edit_text(f"🛍 <b>Каталог PARFERA</b>\n\nВ каталоге <b>{len(PRODUCTS):,}</b> позиций.\n\nПока первым тестируем раздел VERSACE:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "brand:versace")
async def brand_versace(callback: CallbackQuery):
    items = versace_items()
    await callback.message.edit_text("<b>VERSACE</b>\n\nВыберите аромат:", reply_markup=versace_kb(items))
    await callback.answer()


@dp.callback_query(F.data.in_({"search", "catalog_search"}))
async def search(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting)
    await callback.message.edit_text("🔎 <b>Поиск по каталогу</b>\n\nВведите название бренда, аромата или артикул.\n\nНапример: <b>Versace Eros</b>, <b>Erba Pura</b> или <b>000-002</b>.", reply_markup=back_home_kb())
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
        await message.answer(f'🔎 По запросу «{query}» ничего не найдено.\n\nПопробуйте более короткий запрос.', reply_markup=back_home_kb())
        return
    await message.answer(f'🔎 Найдено: <b>{len(matches)}</b>\nЗапрос: «{query}»\n\nВыберите товар:', reply_markup=results_kb(items, 0, len(matches)))


@dp.callback_query(F.data.startswith("page:"))
async def page(callback: CallbackQuery):
    await show_search_results(callback.message, callback.from_user.id, int(callback.data.split(":")[1]))
    await callback.answer()


@dp.callback_query(F.data.startswith("product:"))
async def product(callback: CallbackQuery):
    pid = callback.data.split(":", 1)[1]
    p = BY_ID.get(pid)
    if not p:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await callback.message.delete()
    await send_product(callback.message, p)
    await callback.answer()


@dp.callback_query(F.data.startswith("add:"))
async def add(callback: CallbackQuery):
    _, pid, typ = callback.data.split(":")
    p = BY_ID.get(pid)
    if not p:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if typ == "bottle":
        price = p.get("bottle_price_rub")
        label = "Флакон"
    else:
        price = p.get("tester_price_rub")
        label = "Тестер"
    if not price:
        await callback.answer("Этот вариант недоступен", show_alert=True)
        return
    uid = callback.from_user.id
    cart = CARTS.setdefault(uid, [])
    key = (pid, typ)
    existing = next((x for x in cart if (x["pid"], x["type_key"]) == key), None)
    if existing:
        existing["qty"] += 1
    else:
        cart.append({"pid": pid, "type_key": typ, "name": display_name(p), "volume": p.get("volume", ""), "type": label, "price": int(price), "qty": 1})
    await callback.answer("Добавлено в корзину")
    await callback.message.answer(cart_text(uid), reply_markup=cart_kb(uid))


@dp.callback_query(F.data == "cart")
async def cart(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.edit_text(cart_text(uid), reply_markup=cart_kb(uid))
    await callback.answer()


@dp.callback_query(F.data.startswith("qty:"))
async def qty(callback: CallbackQuery):
    _, idx, delta = callback.data.split(":")
    uid = callback.from_user.id
    items = CARTS.get(uid, [])
    i = int(idx)
    if 0 <= i < len(items):
        items[i]["qty"] += int(delta)
        if items[i]["qty"] <= 0:
            items.pop(i)
    await callback.message.edit_text(cart_text(uid), reply_markup=cart_kb(uid))
    await callback.answer()


@dp.callback_query(F.data.startswith("del:"))
async def delete_item(callback: CallbackQuery):
    uid = callback.from_user.id
    i = int(callback.data.split(":")[1])
    items = CARTS.get(uid, [])
    if 0 <= i < len(items):
        items.pop(i)
    await callback.message.edit_text(cart_text(uid), reply_markup=cart_kb(uid))
    await callback.answer("Удалено")


@dp.callback_query(F.data == "checkout")
async def checkout(callback: CallbackQuery):
    await callback.message.edit_text("📦 <b>Оформление заказа</b>\n\nНа тестовом этапе заказ пока не оплачивается. Следующим этапом подключим данные покупателя, доставку и отправку заказа менеджеру PARFERA.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Корзина", callback_data="cart")], [InlineKeyboardButton(text="← Главное меню", callback_data="home")]]))
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "popular")
async def popular(callback: CallbackQuery):
    await callback.message.edit_text("⭐ <b>Популярное</b>\n\nПодключим после теста каталога.", reply_markup=back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "new")
async def new(callback: CallbackQuery):
    await callback.message.edit_text("🆕 <b>Новинки</b>\n\nПодключим после теста каталога.", reply_markup=back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "consultant")
async def consultant(callback: CallbackQuery):
    await callback.message.edit_text("👤 <b>Консультант PARFERA</b>\n\nПомогу подобрать аромат, ответить на вопросы и рассказать о новинках.\n\nНапишите, какой аромат вы ищете.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔎 Подобрать аромат", callback_data="search")], [InlineKeyboardButton(text="← Главное меню", callback_data="home")]]))
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
