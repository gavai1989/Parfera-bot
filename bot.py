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

VERSACE_EROS_IMAGE = os.path.join("images", "versace_eros_product.jpg")


def norm(text: str) -> str:
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def rub(value):
    if value in (None, "", 0):
        return None
    return f"{int(value):,}".replace(",", " ") + " ₽"


def clean_product_name(name: str) -> str:
    """Human-friendly fragrance name for the Telegram shop."""
    name = str(name or "").strip()
    # Keep the supplier naming visible for now, as requested.
    return re.sub(r"\s+", " ", name).strip(" -")


def display_name(p):
    return clean_product_name(p.get("name", ""))


def group_key(p):
    # Group one fragrance into one card. Volume, TESTER and supplier condition
    # markers are variants, not separate fragrances. Concentration (EDT/EDP/EDP
    # etc.) remains part of the key so genuinely different formulations do not merge.
    name = str(p.get("base_name") or p.get("name", ""))
    name = re.sub(r"\s+TESTER\b", "", name, flags=re.I)
    name = re.sub(r"\s+(?:без крышки|с крышкой)\b", "", name, flags=re.I)
    name = re.sub(r"\s+пробник\b", "", name, flags=re.I)
    name = re.sub(r"\s+\d+(?:[.,]\d+)?\s*ml\b", "", name, flags=re.I)
    name = re.sub(r"\s*\((?:m|w|u)\)\b", "", name, flags=re.I)
    return norm(name)


SEARCH_TEXT = {p["id"]: norm(f'{p.get("name", "")} {p.get("article", "")}') for p in PRODUCTS}
BY_ID = {p["id"]: p for p in PRODUCTS}
PAGE_SIZE = 10
USER_SEARCH: Dict[int, str] = {}
CARTS: Dict[int, List[dict]] = {}

# One fragrance = one group containing all its volumes/testers.
GROUPS: Dict[str, List[dict]] = {}
for p in PRODUCTS:
    GROUPS.setdefault(group_key(p), []).append(p)


def sort_variants(items):
    def vol_num(p):
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*ml", p.get("name", ""), re.I)
        return float(m.group(1).replace(",", ".")) if m else 9999
    def variant_order(p):
        return 1 if p.get("tester") or p.get("tester_price_rub") else 0
    return sorted(items, key=lambda p: (vol_num(p), variant_order(p), p.get("name", "")))


def unique_variants(items):
    """Remove duplicate supplier rows that have identical visible purchase options."""
    out = []
    seen = set()
    for p in sort_variants(items):
        vol = p.get("volume") or ""
        b = p.get("bottle_price_rub")
        t = p.get("tester_price_rub")
        # Some supplier rows can repeat the same bottle/tester option.
        key = (norm(vol), b, t, bool(p.get("tester")))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def lowest_group_price(items):
    prices = []
    for p in items:
        if p.get("bottle_price_rub"):
            prices.append(int(p["bottle_price_rub"]))
        if p.get("tester_price_rub"):
            prices.append(int(p["tester_price_rub"]))
    return min(prices) if prices else None


# ---------- Automatic brand detection ----------
# The supplier catalog does not contain a separate brand column. We derive it from
# the shared beginning of product names. This handles one-word and multi-word brands
# while avoiding product-line prefixes such as "CHRISTIAN DIOR THE COLLECTION".
def word_tokens(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9À-ÿА-Яа-яЁё'&-]+", str(text or "").upper())


TOKENIZED_NAMES = [word_tokens(p.get("name", "")) for p in PRODUCTS]
PREFIX_COUNTS: Dict[int, Dict[str, int]] = {1: {}, 2: {}, 3: {}, 4: {}}
PREFIX_NEXT: Dict[int, Dict[str, set]] = {1: {}, 2: {}, 3: {}, 4: {}}
for tokens in TOKENIZED_NAMES:
    for k in range(1, min(4, len(tokens)) + 1):
        key = " ".join(tokens[:k])
        PREFIX_COUNTS[k][key] = PREFIX_COUNTS[k].get(key, 0) + 1
        if len(tokens) > k:
            PREFIX_NEXT[k].setdefault(key, set()).add(tokens[k])


def detect_brand(name: str) -> str:
    tokens = word_tokens(name)
    if not tokens:
        return "UNKNOWN"
    chosen = tokens[0]
    parent_count = PREFIX_COUNTS[1].get(chosen, 1)
    for k in range(2, min(4, len(tokens)) + 1):
        pref = " ".join(tokens[:k])
        count = PREFIX_COUNTS[k].get(pref, 0)
        # A real brand prefix usually covers a substantial share of its parent
        # and has multiple different product names following it.
        if count >= max(2, int(parent_count * 0.35)) and len(PREFIX_NEXT[k].get(pref, set())) > 1:
            chosen = pref
            parent_count = count
    return chosen


BRAND_FOR_ID: Dict[str, str] = {}
BRAND_DISPLAY: Dict[str, str] = {}
for p in PRODUCTS:
    key = detect_brand(p.get("name", ""))
    BRAND_FOR_ID[p["id"]] = key
    BRAND_DISPLAY.setdefault(key, key.title())

BRANDS: Dict[str, List[dict]] = {}
for p in PRODUCTS:
    BRANDS.setdefault(BRAND_FOR_ID[p["id"]], []).append(p)

BRAND_KEYS = sorted(BRANDS.keys(), key=lambda x: BRAND_DISPLAY[x].lower())
BRAND_ID_TO_KEY = {str(i): key for i, key in enumerate(BRAND_KEYS)}
BRAND_KEY_TO_ID = {key: str(i) for i, key in enumerate(BRAND_KEYS)}

# Pre-build one-fragrance groups per brand for fast browsing.
BRAND_GROUPS: Dict[str, List[List[dict]]] = {}
for bkey, products in BRANDS.items():
    seen = set()
    groups = []
    for p in products:
        gk = group_key(p)
        if gk not in seen:
            seen.add(gk)
            groups.append(GROUPS[gk])
    groups.sort(key=lambda items: display_name(next((x for x in items if not x.get("tester") and x.get("bottle_price_rub")), items[0])).lower())
    BRAND_GROUPS[bkey] = groups

# Demo photo for the tested Eros group.
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


def product_group(item):
    p = BY_ID[item] if isinstance(item, str) else item
    return GROUPS.get(group_key(p), [p])


def product_text(p):
    title = display_name(p)
    raw = str(p.get("name", ""))
    lines = [f"<b>{title}</b>", "✨ Оригинальная парфюмерия"]
    # Keep supplier technical markers available, but present the main metadata
    # in a compact premium-card style.
    meta = []
    m = re.search(r"\b(edp|edt|parfum|extrait)\b", raw, re.I)
    if m:
        meta.append(m.group(1).upper())
    if re.search(r"\(m\)", raw, re.I):
        meta.append("Мужской")
    elif re.search(r"\(w\)", raw, re.I):
        meta.append("Женский")
    if meta:
        lines.append(" · ".join(meta))
    lines.append("")
    if "VERSACE EROS EDT" in raw.upper():
        lines += ["Свежий, яркий и чувственный аромат с мятой, зелёным яблоком, лимоном, ванилью и древесными нотами.", ""]
    else:
        lines += ["Нишевая и оригинальная парфюмерия PARFERA.", ""]
    lines.append("<b>ВЫБЕРИТЕ ОБЪЁМ И ВАРИАНТ</b>")
    return "\n".join(lines)


def product_kb(pid, brand_id=None, brand_page=0):
    group = unique_variants(product_group(pid))
    rows = []
    option_rows = []
    for p in group:
        vol = p.get("volume") or ""
        options = []
        if p.get("bottle_price_rub"):
            options.append(InlineKeyboardButton(text=f"🧴 {vol} — {rub(p['bottle_price_rub'])}", callback_data=f"addv:{p['id']}:bottle"))
        if p.get("tester_price_rub"):
            options.append(InlineKeyboardButton(text=f"🧪 {vol} · Тестер — {rub(p['tester_price_rub'])}", callback_data=f"addv:{p['id']}:tester"))
        if options:
            option_rows.append(options)
    # Two-column selector on phones when there are many variants; long tester
    # labels remain full-width for readability.
    for opts in option_rows:
        if len(opts) == 2 and all(len(x.text) <= 25 for x in opts):
            rows.append(opts)
        else:
            rows.extend([[x] for x in opts])
    rows.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")])
    if brand_id is None:
        bkey = BRAND_FOR_ID.get(pid)
        brand_id = BRAND_KEY_TO_ID.get(bkey) if bkey else None
    if brand_id is not None:
        rows.append([InlineKeyboardButton(text="← К товарам бренда", callback_data=f"brand:{brand_id}:{brand_page}")])
    else:
        rows.append([InlineKeyboardButton(text="← К брендам", callback_data="brands:0")])
    rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results(query: str, page: int = 0):
    words = [w for w in norm(query).split() if w]
    raw = [p for p in PRODUCTS if all(w in SEARCH_TEXT[p["id"]] for w in words)]
    # Search results should also be fragrance cards, not supplier rows.
    matches = []
    seen = set()
    for p in raw:
        gk = group_key(p)
        if gk in seen:
            continue
        seen.add(gk)
        matches.append(p)
    start = page * PAGE_SIZE
    return matches, matches[start:start + PAGE_SIZE]


def results_kb(items: List[dict], page: int, total: int):
    rows = []
    for p in items:
        title = display_name(p)
        if len(title) > 48:
            title = title[:45] + "…"
        price = lowest_group_price(product_group(p))
        if price:
            title += f" · от {rub(price)}"
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


def brands_kb(page: int = 0):
    per_page = 20
    start = page * per_page
    keys = BRAND_KEYS[start:start + per_page]
    rows = []
    for key in keys:
        bid = BRAND_KEY_TO_ID[key]
        count = len(BRAND_GROUPS[key])
        rows.append([InlineKeyboardButton(text=f"{BRAND_DISPLAY[key]} · {count}", callback_data=f"brand:{bid}:0")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"brands:{page-1}"))
    if (page + 1) * per_page < len(BRAND_KEYS):
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"brands:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [
        [InlineKeyboardButton(text="🔎 Найти аромат", callback_data="search")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def brand_products_kb(brand_id: str, page: int = 0):
    key = BRAND_ID_TO_KEY[brand_id]
    groups = BRAND_GROUPS[key]
    total = len(groups)
    start = page * PAGE_SIZE
    items = groups[start:start + PAGE_SIZE]
    rows = []
    for group in items:
        p = group[0]
        title = display_name(p)
        if len(title) > 44:
            title = title[:41] + "…"
        price = lowest_group_price(group)
        if price:
            title += f" · от {rub(price)}"
        rows.append([InlineKeyboardButton(text=title, callback_data=f"product:{p['id']}:{brand_id}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"brand:{brand_id}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"brand:{brand_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [
        [InlineKeyboardButton(text="← К брендам", callback_data="brands:0")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")],
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


async def send_product(message, p, brand_id=None, brand_page=0):
    text = product_text(p)
    kb = product_kb(p["id"], brand_id, brand_page)
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
    text = f"🛍 <b>Каталог PARFERA</b>\n\nВ каталоге <b>{len(PRODUCTS):,}</b> позиций.\nБрендов: <b>{len(BRAND_KEYS)}</b>\n\nВыберите бренд:"
    await edit_or_replace(callback.message, text, brands_kb(0))
    await callback.answer()


@dp.callback_query(F.data.startswith("brands:"))
async def brands_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    text = f"🛍 <b>Бренды PARFERA</b>\n\nВсего брендов: <b>{len(BRAND_KEYS)}</b>\nВыберите бренд:"
    await edit_or_replace(callback.message, text, brands_kb(page))
    await callback.answer()


@dp.callback_query(F.data.startswith("brand:"))
async def brand_page(callback: CallbackQuery):
    _, brand_id, page = callback.data.split(":")
    key = BRAND_ID_TO_KEY.get(brand_id)
    if not key:
        await callback.answer("Бренд не найден", show_alert=True)
        return
    total = len(BRAND_GROUPS.get(key, []))
    title = BRAND_DISPLAY[key]
    text = f"<b>{title}</b>\n\nАроматов: <b>{total}</b>\nВыберите аромат:"
    await edit_or_replace(callback.message, text, brand_products_kb(brand_id, int(page)))
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
    parts = callback.data.split(":")
    pid = parts[1]
    brand_id = parts[2] if len(parts) > 2 else None
    brand_page = int(parts[3]) if len(parts) > 3 else 0
    p = BY_ID.get(pid)
    if not p:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await callback.message.delete()
    await send_product(callback.message, p, brand_id, brand_page)
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
        cart.append({"pid": pid, "type_key": typ, "name": display_name(p), "volume": p.get("volume", ""), "type": label, "price": int(price), "qty": 1})
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
        print(f"Catalog loaded: {len(PRODUCTS)} products, {len(BRAND_KEYS)} brands, {len(GROUPS)} fragrance groups")
        print("Starting Telegram long polling...")
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
