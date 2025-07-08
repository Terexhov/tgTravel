import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from urllib.parse import quote_plus
import logging
import requests
import csv
import aiohttp
from openai import OpenAI
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import os
from dotenv import load_dotenv
load_dotenv()

# --- Токены напрямую в коде (НЕБЕЗОПАСНО для публичного репозитория) ---
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
AVIASALES_TOKEN = "a657bbd62fab8b942f8d904f22bd426e"

logging.basicConfig(level=logging.INFO)

print("TG_BOT_TOKEN:", TG_BOT_TOKEN)

class TravelForm(StatesGroup):
    vacation_type = State()
    companions = State()
    budget = State()
    dates = State()
    clarify = State()

def get_iata_code(city_name, csv_path="codes.csv"):
    city_name = city_name.strip().lower()
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Ожидаем колонку: City
                if row.get("City", "").strip().lower() == city_name:
                    return row.get("IATA")
    except Exception as e:
        logging.error(f"Ошибка поиска IATA-кода: {e}")
    return None

def get_iata_code_online(city_name):
    try:
        url = f"https://www.travelpayouts.com/widgets_suggest_params?q=Из%20Москвы%20в%20{city_name}"
        logging.info(f"Запрос IATA-кода для города '{city_name}': {url}")
        resp = requests.get(url, timeout=5)
        data = resp.json()
        logging.info(f"Ответ API travelpayouts: {data}")
        
        # Проверяем, что ответ не пустой и содержит destination
        if data and "destination" in data and data["destination"] and "iata" in data["destination"]:
            iata_code = data["destination"]["iata"]
            logging.info(f"Найден IATA-код для '{city_name}': {iata_code}")
            return iata_code
        else:
            logging.warning(f"Не найден IATA-код для города '{city_name}' в ответе API")
            return None
    except Exception as e:
        logging.error(f"Ошибка онлайн-поиска IATA-кода для '{city_name}': {e}")
    return None


USE_GIGACHAT = True  # Только GigaChat

def get_travel_idea_deepseek(vacation_type, companions, budget, dates):
    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.artemox.com/v1"
    )
    prompt = (
        f"Ты — помощник по путешествиям. Пользователь хочет {vacation_type} отдых, едет {companions} человек, бюджет на человека {budget} рублей, даты: {dates}. "
        "На основе этих данных подбери одно направление для путешествия — город и страну, подходящие по бюджету и типу отдыха. "
        "Также подбери примерные даты поездки внутри указанных пользователем дат. "
        "Ответь **только одной строкой** строго в следующем формате:\n"
        "Город, Страна, Departure date (в формате YYYY-MM-DD), Return date (в формате YYYY-MM-DD)\n"
    )
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "Ты — помощник по путешествиям."},
            {"role": "user", "content": prompt},
        ],
        stream=False
    )
    content = response.choices[0].message.content.strip()
    for line in content.split('\n'):
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 4:
            city, country, dep_date, ret_date = parts[:4]
            direction = f"{city}, {country}"
            return direction, city, country, dep_date, ret_date, content
    return None, None, None, None, None, content

def search_aviasales(origin, destination, dep_date, ret_date, budget):
    if not (origin and destination):
        logging.error(f"Нет IATA-кода для города: origin={origin}, destination={destination}")
        return None
    url = (
        f"https://api.travelpayouts.com/aviasales/v3/prices_for_dates?"
        f"origin={origin}&destination={destination}&departure_at={dep_date[:7]}&return_at={ret_date[:7]}"
        f"&unique=false&sorting=price&direct=false&currency=rub&limit=1&page=1&one_way=false&token={AVIASALES_TOKEN}"
    )
    logging.info(f"Aviasales API URL: {url}")
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        logging.info(f"Aviasales API response: {data}")
        if data.get("success") and data.get("data"):
            flight = data["data"][0]
            price = flight["price"]
            if price <= float(budget) * 1.05:
                aviasales_link = f"https://aviasales.ru/{flight['link']}"
                return {"price": price, "link": aviasales_link}
    except Exception as e:
        logging.error(f"Aviasales error: {e}")
    return None

# --- Hotellook integration с подробным логированием ---
async def get_hotellook_location_id(city_name: str) -> str | None:
    url = "https://engine.hotellook.com/api/v2/lookup.json"
    params = {
        "query": city_name,
        "lang": "ru",
        "lookFor": "city",
        "limit": 1,
        "token": AVIASALES_TOKEN
    }
    logging.info(f"Hotellook lookup params: {params}")
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            logging.info(f"Hotellook lookup response for '{city_name}': {data}")
            locations = data.get("results", {}).get("locations", [])
            if locations:
                return locations[0]["id"]
    return None

async def get_first_hotellook_hotel(location_id: str, check_in: str, check_out: str, adults: int = 2) -> dict | None:
    url = "https://yasen.hotellook.com/tp/public/widget_location_dump.json"
    params = {
        "currency": "rub",
        "language": "ru",
        "limit": 1,
        "id": location_id,
        "type": "popularity",
        "check_in": check_in,
        "check_out": check_out,
        "adults": adults,
        "token": AVIASALES_TOKEN
    }
    logging.info(f"Hotellook hotel params: {params}")
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200 or "application/json" not in resp.headers.get("Content-Type", ""):
                text = await resp.text()
                logging.error(f"Hotellook API error {resp.status}: {text}\nParams: {params}")
                return None
            data = await resp.json()
            hotels = data.get("popularity", [])
            if hotels:
                return hotels[0]
    return None

def build_hotellook_hotel_link(hotel_id: int) -> str:
    return f"https://hotellook.com/hotels/hotel-{hotel_id}"

async def start(message: Message, state: FSMContext):
    await message.answer("Привет! Давай спланируем путешествие. Какой отдых предпочитаешь? (пляжный, экскурсионный, активный и т.д.)")
    await state.set_state(TravelForm.vacation_type)

async def process_vacation_type(message: Message, state: FSMContext):
    await state.update_data(vacation_type=message.text)
    await message.answer("Ты едешь один или с кем-то? Если с кем-то — укажи сколько человек.")
    await state.set_state(TravelForm.companions)

async def process_companions(message: Message, state: FSMContext):
    await state.update_data(companions=message.text)
    await message.answer("Какой у тебя бюджет на человека? (в рублях)")
    await state.set_state(TravelForm.budget)

async def process_budget(message: Message, state: FSMContext):
    await state.update_data(budget=message.text)
    await message.answer("В какие даты ты можешь поехать? (например: 2024-08-01 — 2024-08-14)")
    await state.set_state(TravelForm.dates)

async def process_dates(message: Message, state: FSMContext):
    user_data = await state.get_data()
    vacation_type = user_data['vacation_type']
    companions = user_data['companions']
    budget = user_data['budget']
    dates = message.text
    await message.answer("Планирую путешествие...")
    direction, city, country, dep_date, ret_date, raw_response = get_travel_idea_deepseek(vacation_type, companions, budget, dates)
    if not all([city, dep_date, ret_date]):
        logging.error(f"Ошибка парсинга направления из ответа DeepSeek: '{raw_response}'")
        await message.answer("Не удалось корректно распознать направление. Вот что предложила нейросеть:\n" + raw_response)
        await state.clear()
        return
    origin = "MOW"  # Москва как точка отправления (можно сделать выбор)
    destination = get_iata_code_online(city)
    if not destination:
        await message.answer("Продолжаю планировать... Попробую другое направление!")
        data = await state.get_data()
        vacation_type = data.get("vacation_type")
        companions = data.get("companions")
        budget = data.get("budget")
        dep_date = data.get("dep_date")
        ret_date = data.get("ret_date")
        new_city = get_travel_idea_deepseek(vacation_type, companions, budget, f"{dep_date} — {ret_date}")
        await message.answer(f"Новое направление: {new_city}")
        await process_dates(message, state)
        return
    flight_info = search_aviasales(origin, destination, dep_date, ret_date, budget)
    # Получаем ссылку на первый отель через Hotellook
    try:
        guests = int(companions) if companions.isdigit() else 2
    except Exception:
        guests = 2
    hotel_link = None
    hotel_price = None
    location_id = await get_hotellook_location_id(city)
    if not location_id:
        logging.error(f"Не найден location_id для города: {city}")
        await message.answer(f"Не удалось найти отели для города: {city}. Попробуйте другой город.")
        return
    hotel = await get_first_hotellook_hotel(location_id, dep_date, ret_date, guests)
    if not hotel:
        await message.answer(f"Не удалось найти отели по направлению {city} на эти даты.")
        return
    price_info = hotel.get("last_price_info") or {}
    hotel_price = price_info.get("price")
    hotel_link = build_hotellook_hotel_link(hotel["hotel_id"])
    answer = f"✈️ Направление: {direction}\nДаты: {dep_date} — {ret_date}\n\n"
    if flight_info:
        answer += f"Билет: {flight_info['price']} руб.\n[Купить билет]({flight_info['link']})\n"
    else:
        answer += "Не удалось найти подходящий авиабилет в рамках бюджета или по данному направлению.\n"
    if hotel_link:
        answer += f"\n🏨 [Смотреть первый отель]({hotel_link})"
        if hotel_price:
            answer += f"\nЦена за проживание: {hotel_price} руб."
    else:
        answer += "\nНе удалось найти отель по данному направлению."
    await message.answer(answer, parse_mode='Markdown')
    await message.answer("Хотите уточнить параметры? (да/нет)")
    await state.set_state(TravelForm.clarify)

async def process_clarify(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    if text in ["да", "yes", "y", "д"]:
        await message.answer("Ок, давай начнём заново. Какой отдых предпочитаешь? (пляжный, экскурсионный, активный и т.д.)")
        await state.set_state(TravelForm.vacation_type)
    else:
        await message.answer("Спасибо за использование бота! Если захочешь спланировать ещё — напиши /start.")
        await state.clear()


async def main():
    bot = Bot(token=TG_BOT_TOKEN)
    dp = Dispatcher()
    dp.message.register(start, Command("start"))
    dp.message.register(process_vacation_type, TravelForm.vacation_type)
    dp.message.register(process_companions, TravelForm.companions)
    dp.message.register(process_budget, TravelForm.budget)
    dp.message.register(process_dates, TravelForm.dates)
    dp.message.register(process_clarify, TravelForm.clarify)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main()) 