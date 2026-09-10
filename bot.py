import os
import json
import asyncio
import re
from typing import Dict, List
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
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

# Demo product photo for the first test card.
VERSACE_EROS_IMAGE = os.path.join("images", "versace_eros_product.jpg")
for p in PRODUCTS:
    n = p.get("name", "").lower()
    if "versace eros edt (m) 100ml" in n and "tester" not in n:
        p["image_url"] = VERSACE_EROS_IMAGE


def norm(text: str) -> str:
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def rub(value):
    if value in (None, "", 0):
        return None
    return f"{int(value):,}".replace(",", " ") + " ₽"


def display_name(p):
    name = p.get("name", "")
    name = re.sub(r"\s+TESTER$", "", name, flags=re.I)
    return name


def group_key(p):
    """Group the same fragrance/concentration/gender across volumes and tester.
    Gift sets remain separate variants because their extra contents stay in the key.
    """
    base = p.get("base_name") or p.get("name", "")
    base = re.sub(r"\s+TESTER$", "", base, flags=re.I)
    base = re.sub(r"\s+\d+(?:[.,]\d+)?\s*ml\b", "", base, count=1, flags=re.I)
    return norm(base)


SEARCH_TEXT = {p["id"]: norm(f'{p.get("name", "")} {p.get("article", "")}') for p in PRODUCTS}
BY_ID = {p["id"]: p for p in PRODUCTS}
PAGE_SIZE = 10
USER_SEARCH: Dict[int, str] = {}
CARTS: Dict[int, List[dict]] = {}

# Product groups are used in brand browsing: one fragrance = one card in the list.
GROUPS: Dict[str, List[dict]] = {}
for p in PRODUCTS:
    GROUPS.setdefault(group_key(p), []).append(p)

# Reuse the demo Eros image for every standard Eros EDT (men) variant.
eros_group = norm("VERSACE EROS edt (m)")
for p in GROUPS.get(eros_group, []):
    p["image_url"] = VERSACE_EROS_IMAGE


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


def product_group(pid):
    p = BY_ID[pid]
    return GROUPS.get(group_key(p), [p])


def sort_variants(items):
    def vol_num(p):
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*ml", p.get("name", ""), re.I)
        return float(m.group(1).replace(",", ".")) if m else 9999
    return sorted(items, key=lambda p: (vol_num(p), bool(p.get("tester")), p.get("name", "")))


def lowest_group_price(items):
    prices = []
    for p in items:
        if p.get("bottle_price_rub"):
            prices.append(int(p["bottle_price_rub"]))
        if p.get("tester_price_rub"):
            prices.append(int(p["tester_price_rub"]))
    return min(prices) if prices else None


def product_text(p):
    group = product_group(p)
    # Use a clean fragrance name without the selected volume in the heading.
    title = display_name(p)
    title = re.sub(r"\s+\d+(?:[.,]\d+)?\s*ml.*$", "", title, flags=re.I)
    lines = [f"<b>{title}</b>", "✨ Оригинальная парфюмерия", ""]
    if "VERSACE EROS EDT" in display_name(p).upper():
        lines += ["Свежий, яркий и чувственный аромат с мятой, зелёным яблоком, лимоном, ванилью и древесными нотами.", ""]
    lines.append("<b>Выберите объём и вариант:</b>")
    available = [x for x in group if x.get("bottle_price_rub") or x.get("tester_price_rub")]
    lines.append(f"Вариантов: <b>{len(available)}</b>")
    return "\n".join(lines)


def product_kb(pid):
    group = sort_variants(product_group(pid))
    rows = []
    for p in group:
        vol = p.get("volume") or ""
        if p.get("bottle_price_rub"):
            rows.append([InlineKeyboardButton(
                text=f"🧴 {vol} — {rub(p['bottle_price_rub'])}",
                callback_data=f"addv:{p['id']}:bottle"
            )])
        if p.get("tester_price_rub"):
            rows.append([InlineKeyboardButton(
                text=f"🧪 {vol} · Тестер — {rub(p['tester_price_rub'])}",
                callback_data=f"addv:{p['id']}:tester"
            )])
    rows.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")])
    rows.append([InlineKeyboardButton(text="← К товарам VERSACE", callback_data="brand:versace")])
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


def versace_groups():
    groups = []
    seen = set()
    for p in PRODUCTS:
        if not norm(p.get("name", "")).startswith("versace "):
            continue
        gk = group_key(p)
        if gk in seen:
            continue
        seen.add(gk)
        groups.append((gk, GROUPS[gk]))
        if len(groups) >= 40:
            break
    return groups


def versace_kb(groups):
    rows = []
    for gk, items in groups:
        # Prefer the cleanest non-tester product as the list entry.
        p = next((x for x in items if not x.get("tester")), items[0])
        title = display_name(p)
        title = re.sub(r"\s+\d+(?:[.,]\d+)?\s*ml.*$", "", title, flags=re.I)
        price = lowest_group_price(items)
        label = title[:42] + ("…" if len(title) > 42 else "")
        if price:
            label += f" · от {rub(price)}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"product:{p['id']}")])
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


async def edit_or_replace(message, text, reply_markup=None):
    """Edit text messages. If target is a photo card, replace it with a new text message."""
    if message.photo:
        await message.delete()
        return await message.answer(text, reply_markup=reply_markup)
    return await message.edit_text(text, reply_markup=reply_markup)


async def show_search_results(target_message, user_id: int, page: int = 0):
    query = USER_SEARCH.get(user_id, "")
    matches, items = search_results(query, page)
    if not matches:
        await edit_or_replace(target_message, f'🔎 По запросу «{query}» ничего не найдено.\n\nПопробуйте название бренда или аромата.', back_home_kb())
        return
    await edit_or_replace(target_message, f'🔎 Найдено: <b>{len(matches)}</b>\nЗапрос: «{query}»\n\nВыберите товар:', results_kb(items, page, len(matches)))


async def send_product(message, p):
    text = product_text(p)
    kb = product_kb(p["id"])
    image = p.get("image_url")
    if image:
        try:
            await message.answer_photo(photo=FSInputFile(image), caption=text, reply_markup=kb)
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
    await edit_or_replace(callback.message, "<b>PARFERA</b>\n\nНишевая парфюмерия и персональный подбор.\n\nВыберите раздел:", home_kb())
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
    await edit_or_replace(callback.message, f"🛍 <b>Каталог PARFERA</b>\n\nВ каталоге <b>{len(PRODUCTS):,}</b> позиций.\n\nВыберите бренд:", kb)
    await callback.answer()


@dp.callback_query(F.data == "brand:versace")
async def brand_versace(callback: CallbackQuery):
    groups = versace_groups()
    await edit_or_replace(callback.message, "<b>VERSACE</b>\n\nВыберите аромат:", versace_kb(groups))
    await callback.answer()


@dp.callback_query(F.data.in_({"search", "catalog_search"}))
async def search(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting)
    await edit_or_replace(callback.message, "🔎 <b>Поиск по каталогу</b>\n\nВведите название бренда, аромата или артикул.\n\nНапример: <b>Versace Eros</b>, <b>Erba Pura</b> или <b>000-002</b>.", back_home_kb())
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


@dp.callback_query(F.data.startswith("addv:"))
async def add_variant(callback: CallbackQuery):
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
        cart.append({
            "pid": pid,
            "type_key": typ,
            "name": display_name(p),
            "volume": p.get("volume", ""),
            "type": label,
            "price": int(price),
            "qty": 1
        })
    await callback.answer("Добавлено в корзину")
    await callback.message.answer(cart_text(uid), reply_markup=cart_kb(uid))


@dp.callback_query(F.data == "cart")
async def cart(callback: CallbackQuery):
    uid = callback.from_user.id
    await edit_or_replace(callback.message, cart_text(uid), cart_kb(uid))
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
    await edit_or_replace(callback.message, cart_text(uid), cart_kb(uid))
    await callback.answer()


@dp.callback_query(F.data.startswith("del:"))
async def delete_item(callback: CallbackQuery):
    uid = callback.from_user.id
    i = int(callback.data.split(":")[1])
    items = CARTS.get(uid, [])
    if 0 <= i < len(items):
        items.pop(i)
    await edit_or_replace(callback.message, cart_text(uid), cart_kb(uid))
    await callback.answer("Удалено")


@dp.callback_query(F.data == "checkout")
async def checkout(callback: CallbackQuery):
    await edit_or_replace(callback.message, "📦 <b>Оформление заказа</b>\n\nНа тестовом этапе заказ пока не оплачивается. Следующим этапом подключим данные покупателя, доставку и отправку заказа менеджеру PARFERA.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Корзина", callback_data="cart")], [InlineKeyboardButton(text="← Главное меню", callback_data="home")]]))
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "popular")
async def popular(callback: CallbackQuery):
    await edit_or_replace(callback.message, "⭐ <b>Популярное</b>\n\nПодключим после теста каталога.", back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "new")
async def new(callback: CallbackQuery):
    await edit_or_replace(callback.message, "🆕 <b>Новинки</b>\n\nПодключим после теста каталога.", back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "consultant")
async def consultant(callback: CallbackQuery):
    await edit_or_replace(callback.message, "👤 <b>Консультант PARFERA</b>\n\nПомогу подобрать аромат, ответить на вопросы и рассказать о новинках.\n\nНапишите, какой аромат вы ищете.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔎 Подобрать аромат", callback_data="search")], [InlineKeyboardButton(text="← Главное меню", callback_data="home")]]))
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
