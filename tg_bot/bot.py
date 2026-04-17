import asyncio
import csv
import logging
import os
import re
from datetime import date

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

logging.basicConfig(level=logging.INFO)

_MONTHS_RU = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]

_MONTH_MAP: dict[str, int] = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

# Popular Russian departure cities: display name → IATA
POPULAR_ORIGINS: dict[str, str] = {
    "Москва":           "MOW",
    "Санкт-Петербург":  "LED",
    "Новосибирск":      "OVB",
    "Екатеринбург":     "SVX",
    "Казань":           "KZN",
    "Нижний Новгород":  "GOJ",
    "Сочи":             "AER",
    "Уфа":              "UFA",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_date(s: str) -> str | None:
    """Convert various date formats to YYYY-MM-DD, auto-bump past dates."""
    s = s.strip()
    today = date.today()

    def bump(y: int, m: int) -> int:
        if y < today.year or (y == today.year and m < today.month):
            return today.year if m >= today.month else today.year + 1
        return y

    # YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        y, m, d_ = int(s[:4]), int(s[5:7]), int(s[8:10])
        if 1 <= m <= 12 and 1 <= d_ <= 31:
            return f"{bump(y, m)}-{m:02d}-{d_:02d}"

    # DD-MM-YYYY / DD.MM.YYYY / DD/MM/YYYY
    mt = re.fullmatch(r"(\d{1,2})[-./](\d{1,2})[-./](\d{4})", s)
    if mt:
        d_, mo, y = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
        if 1 <= mo <= 12 and 1 <= d_ <= 31:
            return f"{bump(y, mo)}-{mo:02d}-{d_:02d}"

    # "DD месяц [YYYY]"  e.g. "30 апреля 2026", "10 мая"
    mt = re.match(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", s.lower())
    if mt:
        d_ = int(mt.group(1))
        word = mt.group(2)
        y = int(mt.group(3)) if mt.group(3) else today.year
        for key, num in _MONTH_MAP.items():
            if word.startswith(key):
                return f"{bump(y, num)}-{num:02d}-{d_:02d}"

    return None


def clean_city(city: str) -> str:
    """Strip '(Country)' suffix: 'Барселона (Испания)' → 'Барселона'."""
    return re.sub(r"\s*\([^)]*\)", "", city).strip()


def _ddmm(date_str: str) -> str:
    """Extract DDMM from 'YYYY-MM-DD' or ISO datetime for Aviasales link."""
    mt = re.match(r"\d{4}-(\d{2})-(\d{2})", date_str)
    if mt:
        return mt.group(2) + mt.group(1)   # day + month
    mt = re.match(r"\d{4}-(\d{2})", date_str)
    if mt:
        return "01" + mt.group(1)
    return "0101"


def build_aviasales_link(origin: str, dest: str, dep: str, ret: str, adults: int = 1) -> str:
    return f"https://www.aviasales.ru/search/{origin}{_ddmm(dep)}{dest}{_ddmm(ret)}{adults}"


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class TravelForm(StatesGroup):
    origin = State()        # departure city
    vacation_type = State()
    companions = State()
    budget = State()
    budget_custom = State()  # manual budget input
    dates = State()
    result = State()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def kb_origin() -> InlineKeyboardMarkup:
    items = list(POPULAR_ORIGINS.items())
    rows = [
        [
            InlineKeyboardButton(text=f"🛫 {items[i][0]}", callback_data=f"org:{items[i][1]}"),
            InlineKeyboardButton(text=f"🛫 {items[i+1][0]}", callback_data=f"org:{items[i+1][1]}"),
        ]
        for i in range(0, len(items) - 1, 2)
    ]
    rows.append([InlineKeyboardButton(text="✏️ Другой город", callback_data="org:custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_vacation_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏖 Пляжный", callback_data="vt:пляжный"),
            InlineKeyboardButton(text="🏛 Экскурсионный", callback_data="vt:экскурсионный"),
        ],
        [
            InlineKeyboardButton(text="🏔 Активный", callback_data="vt:активный"),
            InlineKeyboardButton(text="⛷ Горнолыжный", callback_data="vt:горнолыжный"),
        ],
        [
            InlineKeyboardButton(text="💆 Оздоровительный", callback_data="vt:оздоровительный"),
            InlineKeyboardButton(text="✏️ Другое", callback_data="vt:custom"),
        ],
    ])


def kb_companions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Один", callback_data="cp:1"),
            InlineKeyboardButton(text="👫 2 человека", callback_data="cp:2"),
        ],
        [
            InlineKeyboardButton(text="👨‍👩‍👦 3 человека", callback_data="cp:3"),
            InlineKeyboardButton(text="👨‍👩‍👧‍👦 4 и более", callback_data="cp:4"),
        ],
    ])


def kb_budget() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 До 50 000 ₽", callback_data="bgt:50000")],
        [InlineKeyboardButton(text="💰 До 100 000 ₽", callback_data="bgt:100000")],
        [InlineKeyboardButton(text="✏️ Своя сумма", callback_data="bgt:custom")],
    ])


def kb_dates() -> InlineKeyboardMarkup:
    today = date.today()
    rows = []
    row = []
    for i in range(6):
        month_0 = (today.month - 1 + i) % 12
        year = today.year + (today.month - 1 + i) // 12
        label = f"{_MONTHS_RU[month_0].capitalize()} {year}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"dt:{year}-{month_0 + 1:02d}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Ввести даты вручную", callback_data="dt:custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_result(flight_link: str | None, flight_price: int | None) -> InlineKeyboardMarkup:
    rows = []
    if flight_link and flight_price is not None:
        price_str = f"{flight_price:,}".replace(",", "\u202f")
        rows.append([InlineKeyboardButton(text=f"✈️ Купить билет — {price_str} ₽", url=flight_link)])
    rows.append([
        InlineKeyboardButton(text="🔄 Другое направление", callback_data="action:another"),
        InlineKeyboardButton(text="🔁 Заново", callback_data="action:restart"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

_BUDGET_LABELS = {
    "50000":  "до 50 000 ₽",
    "100000": "до 100 000 ₽",
}

_VT_LABELS = {
    "пляжный": "🏖 Пляжный",
    "экскурсионный": "🏛 Экскурсионный",
    "активный": "🏔 Активный",
    "горнолыжный": "⛷ Горнолыжный",
    "оздоровительный": "💆 Оздоровительный",
}

_CP_LABELS = {
    "1": "👤 Один",
    "2": "👫 2 человека",
    "3": "👨‍👩‍👦 3 человека",
    "4": "👨‍👩‍👧‍👦 4 и более",
}


def get_iata_code_csv(city_name: str, csv_path: str = "codes.csv") -> str | None:
    city_name = city_name.strip().lower()
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("City", "").strip().lower() == city_name:
                    return row.get("IATA")
    except Exception as e:
        logging.error(f"CSV IATA lookup error: {e}")
    return None


async def get_iata_code_online(city_name: str) -> str | None:
    url = f"https://www.travelpayouts.com/widgets_suggest_params?q=Из%20Москвы%20в%20{city_name}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                iata = (data.get("destination") or {}).get("iata")
                if iata:
                    logging.info(f"IATA for '{city_name}': {iata}")
                    return iata
    except Exception as e:
        logging.error(f"Online IATA lookup error for '{city_name}': {e}")
    return None


def get_travel_idea(
    vacation_type: str,
    companions: str,
    budget: str,
    dates: str,
    exclude: list[str] | None = None,
) -> tuple:
    """Ask Groq LLM for a travel destination."""
    client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    today = date.today()

    exclude_note = f"Не предлагай эти города: {', '.join(exclude)}.\n" if exclude else ""

    system_msg = (
        f"Ты — помощник по путешествиям. Сегодня {today.strftime('%d.%m.%Y')}. "
        "Отвечай строго одной строкой в формате: Город, Страна, YYYY-MM-DD, YYYY-MM-DD. "
        "Никаких пояснений, только строка."
    )
    user_msg = (
        f"Подбери направление для {vacation_type} отдыха.\n"
        f"Туристов: {companions}. Бюджет на человека: {budget} руб.\n"
        f"Период поездки: {dates}.\n"
        f"{exclude_note}"
        "Учитывай бюджет:\n"
        "  до 50 000 руб → Стамбул, Тбилиси, Ереван, Баку, Алматы, Минск\n"
        "  до 100 000 руб → Бангкок, Дубай, Барселона, Прага, Рим, Пхукет, Берлин\n"
        "  свыше 100 000 руб → Токио, Сингапур, Нью-Йорк, Бали, Сидней\n"
        f"Пример ответа: Стамбул, Турция, {today.year}-07-10, {today.year}-07-20"
    )
    content = ""
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.7,
            max_tokens=64,
        )
        content = response.choices[0].message.content.strip()
        logging.info(f"Groq response: {content}")
        for line in content.split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                city = clean_city(parts[0])
                country = clean_city(parts[1])
                dep_date = normalize_date(parts[2]) or parts[2].strip()
                ret_date = normalize_date(parts[3]) or parts[3].strip()
                return f"{city}, {country}", city, country, dep_date, ret_date, content
    except Exception as e:
        logging.error(f"Groq LLM error: {e}")
        content = str(e)
    return None, None, None, None, None, content


async def search_aviasales(origin: str, destination: str, dep_date: str, ret_date: str) -> dict | None:
    """Search cheapest tickets via Travelpayouts Data API v1."""
    if not (origin and destination):
        return None
    url = "https://api.travelpayouts.com/v1/prices/cheap"
    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": dep_date[:7],   # YYYY-MM
        "return_date": ret_date[:7],   # YYYY-MM
        "currency": "rub",
        "token": AVIASALES_TOKEN,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                logging.info(f"Aviasales v1 response: {data}")
                if not data.get("success") or not data.get("data"):
                    return None
                dest_data: dict = data["data"].get(destination, {})
                # Try 0, 1, 2 transfers — pick cheapest available
                best = None
                for key in dest_data:
                    flight = dest_data[key]
                    if best is None or flight["price"] < best["price"]:
                        best = flight
                if best:
                    link = build_aviasales_link(
                        origin, destination,
                        best.get("departure_at", dep_date),
                        best.get("return_at", ret_date),
                    )
                    return {"price": best["price"], "link": link}
    except Exception as e:
        logging.error(f"Aviasales error: {e}")
    return None


# --- Hotellook (temporarily disabled) ---
# async def get_hotellook_location_id(...) ...
# async def get_first_hotellook_hotel(...) ...


async def _run_search(message: Message, state: FSMContext) -> None:
    """LLM → IATA → Aviasales → send result with action buttons."""
    user_data = await state.get_data()
    tried_cities: list[str] = user_data.get("tried_cities", [])
    origin_iata: str = user_data.get("origin_iata", "MOW")

    direction = city = country = dep_date = ret_date = destination = None

    for attempt in range(3):
        direction, city, country, dep_date, ret_date, raw = get_travel_idea(
            user_data["vacation_type"],
            user_data["companions"],
            user_data["budget"],
            user_data["dates"],
            exclude=tried_cities,
        )
        if not all([city, dep_date, ret_date]):
            logging.error(f"Parse failed (attempt {attempt + 1}): '{raw}'")
            continue

        destination = get_iata_code_csv(city) or await get_iata_code_online(city)
        if destination:
            break

        logging.warning(f"No IATA for '{city}', retrying")
        tried_cities.append(city)
        city = None

    await state.update_data(tried_cities=tried_cities, last_city=city)

    if not city or not destination:
        await message.answer(
            "😔 Не удалось подобрать направление.\n"
            "Попробуй изменить параметры: /start"
        )
        await state.clear()
        return

    flight_info = await search_aviasales(origin_iata, destination, dep_date, ret_date)

    price_str = ""
    if flight_info:
        price_str = f"{flight_info['price']:,}".replace(",", "\u202f")

    text = f"✈️ *{direction}*\n📅 {dep_date} — {ret_date}\n"
    if flight_info:
        text += f"\n🎫 Билет найден: *{price_str} ₽*"
    else:
        text += "\n🎫 Авиабилет на этот период не найден"

    await message.answer(
        text,
        parse_mode="Markdown",
        reply_markup=kb_result(
            flight_info["link"] if flight_info else None,
            flight_info["price"] if flight_info else None,
        ),
    )
    await state.set_state(TravelForm.result)


# ---------------------------------------------------------------------------
# Handlers — origin (departure city)
# ---------------------------------------------------------------------------

async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✈️ *Привет! Давай спланируем путешествие.*\n\nИз какого города летишь?",
        parse_mode="Markdown",
        reply_markup=kb_origin(),
    )
    await state.set_state(TravelForm.origin)


async def cb_origin(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":")[1]
    if value == "custom":
        await callback.message.edit_text("Напиши название своего города:")
        await callback.answer()
        return  # stay in origin state, wait for text

    # Find display name by IATA
    city_name = next((k for k, v in POPULAR_ORIGINS.items() if v == value), value)
    await callback.message.edit_text(f"Вылет из: 🛫 {city_name}")
    await state.update_data(origin_iata=value, origin_name=city_name)
    await callback.answer()
    await callback.message.answer("Какой отдых предпочитаешь?", reply_markup=kb_vacation_type())
    await state.set_state(TravelForm.vacation_type)


async def process_origin(message: Message, state: FSMContext):
    city = message.text.strip()
    iata = get_iata_code_csv(city) or await get_iata_code_online(city)
    if not iata:
        await message.answer(
            f"Не смог найти аэропорт для «{city}».\n"
            "Попробуй написать иначе или выбери из списка — /start"
        )
        return
    await state.update_data(origin_iata=iata, origin_name=city)
    await message.answer(f"Вылет из: 🛫 {city} ({iata})")
    await message.answer("Какой отдых предпочитаешь?", reply_markup=kb_vacation_type())
    await state.set_state(TravelForm.vacation_type)


# ---------------------------------------------------------------------------
# Handlers — vacation type
# ---------------------------------------------------------------------------

async def cb_vacation_type(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":")[1]
    if value == "custom":
        await callback.message.edit_text("Напиши, какой отдых тебя интересует:")
        await callback.answer()
        return
    await callback.message.edit_text(f"Отдых: {_VT_LABELS.get(value, value)}")
    await state.update_data(vacation_type=value)
    await callback.answer()
    await callback.message.answer("С кем едешь?", reply_markup=kb_companions())
    await state.set_state(TravelForm.companions)


async def process_vacation_type(message: Message, state: FSMContext):
    await state.update_data(vacation_type=message.text)
    await message.answer("С кем едешь?", reply_markup=kb_companions())
    await state.set_state(TravelForm.companions)


# ---------------------------------------------------------------------------
# Handlers — companions
# ---------------------------------------------------------------------------

async def cb_companions(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":")[1]
    await callback.message.edit_text(f"Путешественники: {_CP_LABELS.get(value, value)}")
    await state.update_data(companions=value)
    await callback.answer()
    await callback.message.answer("Какой бюджет на человека?", reply_markup=kb_budget())
    await state.set_state(TravelForm.budget)


async def process_companions(message: Message, state: FSMContext):
    await state.update_data(companions=message.text)
    await message.answer("Какой бюджет на человека?", reply_markup=kb_budget())
    await state.set_state(TravelForm.budget)


# ---------------------------------------------------------------------------
# Handlers — budget
# ---------------------------------------------------------------------------

async def cb_budget(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":")[1]
    if value == "custom":
        await callback.message.edit_text("Введи свой бюджет в рублях на человека (например: 75000):")
        await callback.answer()
        await state.set_state(TravelForm.budget_custom)
        return
    await callback.message.edit_text(f"Бюджет: {_BUDGET_LABELS.get(value, value + ' ₽')}")
    await state.update_data(budget=value)
    await callback.answer()
    await callback.message.answer("В какой месяц хочешь поехать?", reply_markup=kb_dates())
    await state.set_state(TravelForm.dates)


async def process_budget_custom(message: Message, state: FSMContext):
    raw = message.text.strip()
    # Extract digits only
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        await message.answer("Пожалуйста, введи число, например: 75000")
        return
    await state.update_data(budget=digits)
    amount = f"{int(digits):,}".replace(",", "\u202f")
    await message.answer(f"Бюджет: {amount} ₽")
    await message.answer("В какой месяц хочешь поехать?", reply_markup=kb_dates())
    await state.set_state(TravelForm.dates)


async def process_budget(message: Message, state: FSMContext):
    await state.update_data(budget=message.text)
    await message.answer("В какой месяц хочешь поехать?", reply_markup=kb_dates())
    await state.set_state(TravelForm.dates)


# ---------------------------------------------------------------------------
# Handlers — dates
# ---------------------------------------------------------------------------

async def cb_dates(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":")[1]
    if value == "custom":
        await callback.message.edit_text(
            "Напиши когда хочешь поехать — в любом формате:\n\n"
            "• с 30 апреля по 10 мая\n"
            "• с 5 по 10 июня\n"
            "• в июле\n"
            "• 2026-08-01 — 2026-08-14"
        )
        await callback.answer()
        return

    year, month = value.split("-")
    month_name = _MONTHS_RU[int(month) - 1].capitalize()
    dates_str = f"{month_name} {year}"

    await callback.message.edit_text(f"Период: {dates_str}")
    await state.update_data(dates=dates_str)
    await callback.answer()
    await callback.message.answer("⏳ Подбираю направление...")
    await _run_search(callback.message, state)


async def process_dates(message: Message, state: FSMContext):
    await state.update_data(dates=message.text)
    await message.answer("⏳ Подбираю направление...")
    await _run_search(message, state)


# ---------------------------------------------------------------------------
# Handlers — result actions
# ---------------------------------------------------------------------------

async def cb_result_action(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]

    if action == "restart":
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()
        await state.clear()
        await callback.message.answer(
            "✈️ *Начнём заново! Из какого города летишь?*",
            parse_mode="Markdown",
            reply_markup=kb_origin(),
        )
        await state.set_state(TravelForm.origin)
        return

    if action == "another":
        user_data = await state.get_data()
        tried = list(user_data.get("tried_cities", []))
        last = user_data.get("last_city")
        if last and last not in tried:
            tried.append(last)
        await state.update_data(tried_cities=tried)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ищу другое направление…")
        await callback.message.answer("⏳ Ищу другое направление...")
        await _run_search(callback.message, state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    bot = Bot(token=TG_BOT_TOKEN)
    dp = Dispatcher()

    # Messages
    dp.message.register(start, Command("start"))
    dp.message.register(process_origin, TravelForm.origin)
    dp.message.register(process_vacation_type, TravelForm.vacation_type)
    dp.message.register(process_companions, TravelForm.companions)
    dp.message.register(process_budget, TravelForm.budget)
    dp.message.register(process_budget_custom, TravelForm.budget_custom)
    dp.message.register(process_dates, TravelForm.dates)

    # Callbacks
    dp.callback_query.register(cb_origin, TravelForm.origin, F.data.startswith("org:"))
    dp.callback_query.register(cb_vacation_type, TravelForm.vacation_type, F.data.startswith("vt:"))
    dp.callback_query.register(cb_companions, TravelForm.companions, F.data.startswith("cp:"))
    dp.callback_query.register(cb_budget, TravelForm.budget, F.data.startswith("bgt:"))
    dp.callback_query.register(cb_dates, TravelForm.dates, F.data.startswith("dt:"))
    dp.callback_query.register(cb_result_action, TravelForm.result, F.data.startswith("action:"))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
