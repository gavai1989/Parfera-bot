import os, json, asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

TOKEN = os.environ.get("PARFERA_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан PARFERA_BOT_TOKEN")

with open("catalog.json", encoding="utf-8") as f:
    PRODUCTS = json.load(f)["products"]

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

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "PARFERA\n\nНишевая парфюмерия и персональный подбор.",
        reply_markup=home_kb()
    )

@dp.callback_query(F.data == "catalog")
async def catalog(callback: CallbackQuery):
    brands=list(dict.fromkeys(p["brand"] for p in PRODUCTS))
    kb=[[InlineKeyboardButton(text=b,callback_data=f"brand:{i}")] for i,b in enumerate(brands)]
    kb.append([InlineKeyboardButton(text="← Главное меню",callback_data="home")])
    await callback.message.edit_text("Выберите бренд:",reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("brand:"))
async def brand(callback: CallbackQuery):
    brands=list(dict.fromkeys(p["brand"] for p in PRODUCTS))
    b=brands[int(callback.data.split(":")[1])]
    ps=[p for p in PRODUCTS if p["brand"]==b]
    kb=[]
    for p in ps:
        prices=[]
        if p["bottle_price_rub"]: prices.append(f'флакон {p["bottle_price_rub"]:,} ₽'.replace(","," "))
        if p["tester_price_rub"]: prices.append(f'тестер {p["tester_price_rub"]:,} ₽'.replace(","," "))
        kb.append([InlineKeyboardButton(text=f'{p["name"]} · {p["volume"]}',callback_data=f'product:{p["id"]}')])
    kb.append([InlineKeyboardButton(text="← Бренды",callback_data="catalog")])
    await callback.message.edit_text(f"<b>{b}</b>\n\nВыберите аромат:",reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("product:"))
async def product(callback: CallbackQuery):
    p=next(x for x in PRODUCTS if x["id"]==callback.data.split(":")[1])
    lines=[f'<b>{p["brand"]}</b>',f'<b>{p["name"]}</b>',p["volume"],""]
    if p["bottle_price_rub"]: lines.append(f'Флакон — <b>{p["bottle_price_rub"]:,} ₽</b>'.replace(","," "))
    if p["tester_price_rub"]: lines.append(f'Тестер — <b>{p["tester_price_rub"]:,} ₽</b>'.replace(","," "))
    kb=[[InlineKeyboardButton(text="🛒 Добавить",callback_data=f'add:{p["id"]}')],
        [InlineKeyboardButton(text="← К бренду",callback_data=f'brand:{list(dict.fromkeys(x["brand"] for x in PRODUCTS)).index(p["brand"])}')]]
    await callback.message.edit_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await callback.message.edit_text("PARFERA\n\nНишевая парфюмерия и персональный подбор.",reply_markup=home_kb())
    await callback.answer()

@dp.callback_query(F.data == "search")
async def search(callback: CallbackQuery):
    await callback.message.edit_text("🔎 Поиск\n\nВ следующей версии добавим поиск по всему каталогу.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню",callback_data="home")]]))
    await callback.answer()

@dp.callback_query(F.data == "popular")
async def popular(callback: CallbackQuery):
    await callback.message.edit_text("⭐ Популярное\n\nПодключим после первичного теста.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню",callback_data="home")]]))
    await callback.answer()

@dp.callback_query(F.data == "new")
async def new(callback: CallbackQuery):
    await callback.message.edit_text("🆕 Новинки\n\nПодключим после первичного теста.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню",callback_data="home")]]))
    await callback.answer()

@dp.callback_query(F.data == "cart")
async def cart(callback: CallbackQuery):
    await callback.message.edit_text("🛒 Корзина\n\nТестовый раздел. Полноценную корзину подключим следующим этапом.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню",callback_data="home")]]))
    await callback.answer()

@dp.callback_query(F.data == "consultant")
async def consultant(callback: CallbackQuery):
    await callback.message.edit_text("👤 Консультант\n\nНапишите, какой аромат ищете, и мы подберём варианты.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню",callback_data="home")]]))
    await callback.answer()

@dp.callback_query(F.data.startswith("add:"))
async def add(callback: CallbackQuery):
    await callback.answer("Товар добавлен в тестовую корзину",show_alert=True)

async def main():
    bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
