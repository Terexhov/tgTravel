import asyncio
import csv
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from dotenv import load_dotenv
import anthropic

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN", "")
AVIASALES_MARKER = os.getenv("AVIASALES_MARKER", "")
PARTNER_URL_BASE = os.getenv("PARTNER_URL_BASE", "https://www.aviasales.ru/search")
# TravelPayouts affiliate for hotels: tp.media/r?marker=M&trs=T&p=P&u=URL
TP_TRS = os.getenv("TP_TRS", "")
TP_HOTEL_PROGRAM = os.getenv("TP_HOTEL_PROGRAM", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
REDIS_URL = os.getenv("REDIS_URL", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")
DB_PATH = os.getenv("METRICS_DB", "metrics.db")
RATE_MSG_PER_MIN = int(os.getenv("RATE_MSG_PER_MIN", "30"))
RATE_LLM_PER_HOUR = int(os.getenv("RATE_LLM_PER_HOUR", "10"))

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Problem 6: IATA in-memory cache (loaded once at startup)
# ---------------------------------------------------------------------------

_IATA_BY_CITY: dict[str, str] = {}
_IATA_CODES_SET: set[str] = set()


def _load_iata_codes(csv_path: str = "codes.csv") -> None:
    global _IATA_BY_CITY, _IATA_CODES_SET
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                city = row.get("City", "").strip()
                iata = row.get("IATA", "").strip()
                if city and iata:
                    _IATA_BY_CITY[city.lower()] = iata
                    _IATA_CODES_SET.add(iata)
        logging.info(f"Loaded {len(_IATA_BY_CITY)} IATA codes into memory")
    except Exception as e:
        logging.error(f"Failed to load IATA codes: {e}")


def get_iata_code(city_name: str) -> str | None:
    return _IATA_BY_CITY.get(city_name.strip().lower())


async def get_iata_code_online(city_name: str) -> str | None:
    url = (
        f"https://www.travelpayouts.com/widgets_suggest_params"
        f"?q=Из%20Москвы%20в%20{city_name}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                iata = (data.get("destination") or {}).get("iata")
                if iata:
                    _IATA_CODES_SET.add(iata)
                    logging.info(f"Online IATA for '{city_name}': {iata}")
                    return iata
    except Exception as e:
        logging.error(f"Online IATA lookup error for '{city_name}': {e}")
    return None


# ---------------------------------------------------------------------------
# Problem 2: Redis client (optional — FSM + rate limiter)
# ---------------------------------------------------------------------------

redis_client = None


async def _init_redis() -> None:
    global redis_client
    if not REDIS_URL:
        return
    try:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logging.info("Redis connected")
    except Exception as e:
        logging.error(f"Redis connection failed — running without Redis: {e}")
        redis_client = None


# ---------------------------------------------------------------------------
# Problem 3: Rate-limit middleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not redis_client:
            return await handler(event, data)

        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        now = datetime.utcnow()
        key = f"rl:msg:{user.id}:{now.strftime('%Y%m%d%H%M')}"
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 60)

        if count > RATE_MSG_PER_MIN:
            if isinstance(event, Message):
                await event.answer("⚠️ Слишком много запросов. Подожди минуту.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Слишком много запросов!", show_alert=True)
            return None

        return await handler(event, data)


async def _check_llm_rate_limit(user_id: int) -> tuple[bool, int]:
    """Returns (allowed, minutes_until_reset)."""
    if not redis_client:
        return True, 0
    key = f"rl:llm:{user_id}:{datetime.utcnow().strftime('%Y%m%d%H')}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 3600)
    if count > RATE_LLM_PER_HOUR:
        ttl = await redis_client.ttl(key)
        return False, max(ttl // 60, 1)
    return True, 0


# ---------------------------------------------------------------------------
# Metrics (SQLite)
# ---------------------------------------------------------------------------

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            username TEXT,
            event    TEXT NOT NULL,
            ts       TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def log_event(user_id: int, username: str | None, event: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO events (user_id, username, event, ts) VALUES (?, ?, ?, ?)",
            (user_id, username or "", event, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Metrics write error: {e}")


def get_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    mau = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= datetime('now', '-30 days')"
    ).fetchone()[0]
    dau = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= date('now')"
    ).fetchone()[0]
    total_users = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM events"
    ).fetchone()[0]
    searches_30d = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event='search_start'"
        " AND ts >= datetime('now', '-30 days')"
    ).fetchone()[0]
    found_30d = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event='search_found_price'"
        " AND ts >= datetime('now', '-30 days')"
    ).fetchone()[0]
    conn.close()
    return {
        "mau": mau,
        "dau": dau,
        "total_users": total_users,
        "searches_30d": searches_30d,
        "found_30d": found_30d,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MONTHS_RU = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]

_MONTH_MAP: dict[str, int] = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

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

# Problem 5: trip duration rules per vacation type
_DURATION_RULES: dict[str, tuple[int, int]] = {
    "пляжный":         (7, 10),
    "экскурсионный":   (4, 7),
    "активный":        (5, 10),
    "горнолыжный":     (5, 8),
    "оздоровительный": (7, 14),
}


def normalize_date(s: str) -> str | None:
    s = s.strip()
    today = date.today()

    def bump(y: int, m: int) -> int:
        if y < today.year or (y == today.year and m < today.month):
            return today.year if m >= today.month else today.year + 1
        return y

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        y, m, d_ = int(s[:4]), int(s[5:7]), int(s[8:10])
        if 1 <= m <= 12 and 1 <= d_ <= 31:
            return f"{bump(y, m)}-{m:02d}-{d_:02d}"

    mt = re.fullmatch(r"(\d{1,2})[-./](\d{1,2})[-./](\d{4})", s)
    if mt:
        d_, mo, y = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
        if 1 <= mo <= 12 and 1 <= d_ <= 31:
            return f"{bump(y, mo)}-{mo:02d}-{d_:02d}"

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
    return re.sub(r"\s*\([^)]*\)", "", city).strip()


def _ddmm(date_str: str) -> str:
    mt = re.match(r"\d{4}-(\d{2})-(\d{2})", date_str)
    if mt:
        return mt.group(2) + mt.group(1)
    mt = re.match(r"\d{4}-(\d{2})", date_str)
    if mt:
        return "01" + mt.group(1)
    return "0101"


def _parse_adults(companions: str) -> int:
    try:
        return min(max(int(companions), 1), 6)
    except (ValueError, TypeError):
        return 1


def build_partner_link(
    origin: str, dest: str, dep: str, ret: str, adults: int = 1
) -> str:
    path = f"/{origin}{_ddmm(dep)}{dest}{_ddmm(ret)}{adults}"
    url = PARTNER_URL_BASE.rstrip("/") + path
    if AVIASALES_MARKER:
        url += f"?marker={AVIASALES_MARKER}"
    return url


def build_hotel_link(
    city_en: str, dep_date: str, ret_date: str, adults: int = 1
) -> str:
    """Build Yandex Hotels deeplink wrapped in TravelPayouts affiliate."""
    dep = dep_date[:10]
    ret = ret_date[:10]
    city_slug = re.sub(r"\s+", "-", city_en.lower().strip())
    hotel_url = (
        f"https://travel.yandex.ru/hotels/{city_slug}/"
        f"?adults={adults}&checkinDate={dep}&checkoutDate={ret}"
        f"&childrenAges=&flexibleDatesType&selectedSortId=relevant-first"
    )
    if AVIASALES_MARKER and TP_TRS and TP_HOTEL_PROGRAM:
        hotel_url = (
            f"https://tp.media/r"
            f"?marker={AVIASALES_MARKER}&trs={TP_TRS}&p={TP_HOTEL_PROGRAM}"
            f"&u={quote(hotel_url, safe='')}"
        )
    return hotel_url


def _is_affiliate(url: str) -> bool:
    """Return True if URL already contains affiliate marker."""
    if not url:
        return False
    return (
        "marker=" in url
        or url.startswith("https://tp.media/r")
    )


async def affiliatize_links(urls: list[str]) -> dict[str, str]:
    """
    Convert non-affiliate URLs via TravelPayouts links API.
    Returns mapping original_url -> affiliate_url.
    Leaves already-affiliate or unconvertible URLs unchanged.
    """
    if not (AVIASALES_TOKEN and AVIASALES_MARKER and TP_TRS):
        return {u: u for u in urls}

    to_convert = [u for u in urls if u and not _is_affiliate(u)]
    result = {u: u for u in urls}

    if not to_convert:
        return result

    payload = {
        "trs": int(TP_TRS),
        "marker": int(AVIASALES_MARKER),
        "shorten": False,
        "links": [{"url": u} for u in to_convert],
    }
    headers = {
        "X-Access-Token": AVIASALES_TOKEN,
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.travelpayouts.com/links/v1/create",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json()
                for item in data if isinstance(data, list) else []:
                    original = item.get("url") or item.get("original_url", "")
                    affiliate = item.get("affiliate_url") or item.get("link", "")
                    if original and affiliate:
                        result[original] = affiliate
                logging.info(f"affiliatize_links: converted {len(to_convert)} URLs")
    except Exception as e:
        logging.error(f"affiliatize_links error: {e}")

    return result


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class TravelForm(StatesGroup):
    origin = State()
    vacation_type = State()
    companions = State()
    baggage = State()
    transfers = State()
    budget = State()
    budget_custom = State()
    dates = State()
    result = State()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def kb_origin() -> InlineKeyboardMarkup:
    items = list(POPULAR_ORIGINS.items())
    rows = [
        [
            InlineKeyboardButton(
                text=f"🛫 {items[i][0]}", callback_data=f"org:{items[i][1]}"
            ),
            InlineKeyboardButton(
                text=f"🛫 {items[i + 1][0]}", callback_data=f"org:{items[i + 1][1]}"
            ),
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


def kb_baggage() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ С багажом", callback_data="bg:with"),
            InlineKeyboardButton(text="🎒 Только ручная кладь", callback_data="bg:without"),
        ],
        [InlineKeyboardButton(text="🤷 Неважно", callback_data="bg:any")],
    ])


def kb_transfers() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Только прямые", callback_data="tr:direct")],
        [
            InlineKeyboardButton(text="1️⃣ До 1 пересадки", callback_data="tr:one"),
            InlineKeyboardButton(text="🤷 Любые", callback_data="tr:any"),
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
    rows: list[list] = []
    row: list = []
    for i in range(6):
        month_0 = (today.month - 1 + i) % 12
        year = today.year + (today.month - 1 + i) // 12
        label = f"{_MONTHS_RU[month_0].capitalize()} {year}"
        row.append(
            InlineKeyboardButton(text=label, callback_data=f"dt:{year}-{month_0 + 1:02d}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Ввести даты вручную", callback_data="dt:custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_result(
    flight_link: str | None,
    flight_price: int | None,
    hotel_link: str | None = None,
    search_link: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list] = []
    if flight_link and flight_price is not None:
        price_str = f"{flight_price:,}".replace(",", "\u202f")
        rows.append([InlineKeyboardButton(
            text=f"✈️ Купить билет — {price_str} ₽", url=flight_link
        )])
    elif search_link:
        rows.append([InlineKeyboardButton(text="🔍 Найти билеты", url=search_link)])
    if hotel_link:
        rows.append([InlineKeyboardButton(text="🏨 Найти отель", url=hotel_link)])
    rows.append([
        InlineKeyboardButton(text="🔄 Другое направление", callback_data="action:another"),
        InlineKeyboardButton(text="🔁 Заново", callback_data="action:restart"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Problem 4: LLM — tool-calling with structured output
# ---------------------------------------------------------------------------

_SEASONALITY = (
    "Учитывай сезон: "
    "пляжный декабрь–март → Таиланд, ОАЭ, Вьетнам, Гоа, Занзибар, Египет; "
    "пляжный апрель–май → Таиланд, ОАЭ, Мальдивы; "
    "пляжный июнь–сентябрь → Турция, Черногория, Греция, Кипр, Хорватия; "
    "горнолыжный декабрь–март → Сочи, Шерегеш, Андорра, Грузия (Гудаури). "
)

_BUDGET_HINTS: list[tuple[int, str]] = [
    (30_000,       "до 30 000 ₽ → СНГ: Тбилиси, Ереван, Баку, Алматы, Ташкент, Бишкек"),
    (60_000,       "30–60 000 ₽ → Стамбул, Анталья, Батуми, Белград, Дубай (лоукостер)"),
    (100_000,      "60–100 000 ₽ → Бангкок, Пхукет, Бали, Гоа, Дубай, Египет, Барселона"),
    (150_000,      "100–150 000 ₽ → Токио, Сингапур, Мальдивы, Маврикий"),
    (10_000_000,   "150 000+ ₽ → Нью-Йорк, Лос-Анджелес, Сидней, Рио"),
]


def get_travel_candidates(
    vacation_type: str,
    companions: str,
    budget: int,
    dates: str,
    exclude_iata: list[str],
    baggage: str = "any",
    transfers: str = "any",
) -> list[dict]:
    """
    Calls Groq via tool-calling to get 3-5 structured destination candidates.
    Each candidate is validated: IATA must exist in codes.csv, dates must be sane.
    """
    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        **({"base_url": ANTHROPIC_BASE_URL} if ANTHROPIC_BASE_URL else {}),
    )
    today = date.today()

    min_days, max_days = _DURATION_RULES.get(vacation_type, (5, 10))
    budget_hint = next(label for threshold, label in _BUDGET_HINTS if budget <= threshold)
    exclude_note = (
        f"Не предлагай эти IATA (уже показаны): {', '.join(exclude_iata)}. "
        if exclude_iata else ""
    )

    system_msg = (
        f"Ты — туристический менеджер. Сегодня {today.strftime('%d.%m.%Y')}. "
        "Предлагай только безвизовые направления для граждан России (или с e-Visa). "
        f"{_SEASONALITY}"
        f"Длительность для '{vacation_type}': {min_days}–{max_days} дней. "
        "Вызови suggest_destinations с 3-5 разнообразными вариантами. "
        "Используй только реальные IATA-коды аэропортов. "
        "city_en — название города на английском для URL."
    )

    baggage_note = {
        "with": "Обязательно с регистрируемым багажом.",
        "without": "Предпочтительно тариф без багажа (только ручная кладь).",
        "any": "",
    }.get(baggage, "")
    transfers_note = {
        "direct": "Только прямые рейсы без пересадок.",
        "one": "Не более 1 пересадки.",
        "any": "",
    }.get(transfers, "")
    extra = " ".join(filter(None, [baggage_note, transfers_note]))

    user_msg = (
        f"Подбери направления: {vacation_type} отдых.\n"
        f"Туристов: {companions}. Бюджет на перелёт на человека: {budget} ₽ ({budget_hint}).\n"
        f"Период: {dates}.\n"
        + (f"{extra}\n" if extra else "")
        + exclude_note
    )

    tools = [{
        "name": "suggest_destinations",
        "description": "Предлагает 3-5 направлений для путешествия",
        "input_schema": {
            "type": "object",
            "properties": {
                "destinations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "iata": {
                                "type": "string",
                                "description": "IATA-код аэропорта назначения (3 буквы заглавные)",
                            },
                            "city": {
                                "type": "string",
                                "description": "Название города на русском",
                            },
                            "city_en": {
                                "type": "string",
                                "description": "City name in English (for URL slug)",
                            },
                            "country": {
                                "type": "string",
                                "description": "Страна на русском",
                            },
                            "dep_date": {
                                "type": "string",
                                "description": f"Дата вылета YYYY-MM-DD в пределах {dates}",
                            },
                            "ret_date": {
                                "type": "string",
                                "description": (
                                    f"Дата возврата YYYY-MM-DD, через "
                                    f"{min_days}–{max_days} дней после dep_date"
                                ),
                            },
                        },
                        "required": ["iata", "city", "city_en", "country", "dep_date", "ret_date"],
                    },
                    "minItems": 3,
                    "maxItems": 5,
                }
            },
            "required": ["destinations"],
        },
    }]

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            system=system_msg,
            messages=[{"role": "user", "content": user_msg}],
            tools=tools,
            tool_choice={"type": "tool", "name": "suggest_destinations"},
            max_tokens=700,
        )
        tool_use_block = next(
            (block for block in response.content if block.type == "tool_use"),
            None,
        )
        if not tool_use_block:
            logging.error("LLM returned no tool calls")
            return []

        args = tool_use_block.input
        raw_candidates = args.get("destinations", [])
        logging.info(f"LLM raw candidates: {raw_candidates}")

        validated: list[dict] = []
        for c in raw_candidates:
            iata = c.get("iata", "").upper().strip()
            city = clean_city(c.get("city", ""))
            city_en = c.get("city_en", city).strip()
            country = clean_city(c.get("country", ""))

            if iata in exclude_iata:
                continue
            if _IATA_CODES_SET and iata not in _IATA_CODES_SET:
                logging.warning(f"LLM hallucinated IATA '{iata}' for {city} — skipping")
                continue

            dep = normalize_date(c.get("dep_date", ""))
            ret = normalize_date(c.get("ret_date", ""))
            if not dep or not ret:
                logging.warning(f"Bad dates for {city}: {c.get('dep_date')} / {c.get('ret_date')}")
                continue

            # Problem 5: validate duration
            try:
                duration = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
                if not (2 <= duration <= 30):
                    logging.warning(f"Duration {duration}d out of range for {city} — skipping")
                    continue
            except ValueError:
                continue

            validated.append({
                "iata": iata,
                "city": city,
                "city_en": city_en,
                "country": country,
                "dep_date": dep,
                "ret_date": ret,
            })

        return validated

    except Exception as e:
        logging.error(f"LLM tool-call error: {e}")
        return []


# ---------------------------------------------------------------------------
# Aviasales Data API
# ---------------------------------------------------------------------------

async def search_aviasales(
    origin: str, destination: str, dep_date: str, ret_date: str, adults: int = 1
) -> dict | None:
    """Query Travelpayouts v1/prices/cheap for cheapest ticket."""
    if not (origin and destination and AVIASALES_TOKEN):
        return None
    url = "https://api.travelpayouts.com/v1/prices/cheap"
    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": dep_date[:7],
        "return_date": ret_date[:7],
        "currency": "rub",
        "token": AVIASALES_TOKEN,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                if not data.get("success") or not data.get("data"):
                    return None
                dest_data: dict = data["data"].get(destination, {})
                best = None
                for flight in dest_data.values():
                    if best is None or flight["price"] < best["price"]:
                        best = flight
                if best:
                    link = build_partner_link(
                        origin,
                        destination,
                        best.get("departure_at") or dep_date,
                        best.get("return_at") or ret_date,
                        adults,
                    )
                    return {"price": best["price"], "link": link}
    except Exception as e:
        logging.error(f"Aviasales error for {destination}: {e}")
    return None


# ---------------------------------------------------------------------------
# Problem 1: Core search — Variant B (parallel price fetch + budget filter)
# ---------------------------------------------------------------------------

def _fmt_price(price_per_person: int, adults: int) -> str:
    total = price_per_person * adults
    s = f"{total:,}".replace(",", "\u202f")
    suffix = f" на {adults} чел." if adults > 1 else ""
    return f"{s} ₽{suffix}"


async def _run_search(message: Message, state: FSMContext) -> None:
    user_data = await state.get_data()
    origin_iata: str = user_data.get("origin_iata", "MOW")
    budget_str: str = user_data.get("budget", "100000")
    companions_str: str = user_data.get("companions", "1")
    tried_iata: list[str] = user_data.get("tried_iata", [])
    baggage: str = user_data.get("baggage", "any")
    transfers: str = user_data.get("transfers", "any")

    digits = re.sub(r"[^\d]", "", budget_str)
    budget = int(digits) if digits else 100_000
    adults = _parse_adults(companions_str)

    user = message.from_user
    user_id = user.id if user else 0
    username = user.username if user else None

    # LLM rate limit
    allowed, wait_min = await _check_llm_rate_limit(user_id)
    if not allowed:
        await message.answer(
            f"⏳ Слишком много AI-запросов. Попробуй через {wait_min} мин."
        )
        return

    log_event(user_id, username, "search_start")

    # Step 1: get candidates from LLM (blocking call → thread pool)
    candidates = await asyncio.to_thread(
        get_travel_candidates,
        user_data.get("vacation_type", ""),
        companions_str,
        budget,
        user_data.get("dates", ""),
        tried_iata,
        baggage,
        transfers,
    )

    if not candidates:
        log_event(user_id, username, "search_failed")
        await message.answer(
            "😔 Не удалось подобрать направления. Попробуй изменить параметры: /start"
        )
        await state.clear()
        return

    # Step 2: fetch prices in parallel
    async def fetch_price(c: dict) -> tuple[dict, dict | None]:
        flight = await search_aviasales(
            origin_iata, c["iata"], c["dep_date"], c["ret_date"], adults
        )
        return c, flight

    results: list[tuple[dict, dict | None]] = await asyncio.gather(
        *[fetch_price(c) for c in candidates]
    )

    # Step 3: pick top-3 in budget, fallback to cheapest over budget or search link
    in_budget = sorted(
        [(c, f) for c, f in results if f and f["price"] <= budget],
        key=lambda x: x[1]["price"],
    )
    shown: list[tuple[dict, dict | None]] = []

    # Collect all raw links first, then affiliatize in one batch
    raw_items: list[tuple[dict, dict | None, str | None, str | None, str]] = []
    # (candidate, flight_info, flight_link, hotel_link, mode)
    # mode: "budget" | "over_budget" | "no_price"

    if in_budget:
        top3 = in_budget[:3]
        log_event(user_id, username, "search_found_price")
        for candidate, flight_info in top3:
            hotel_link = build_hotel_link(
                candidate["city_en"], candidate["dep_date"], candidate["ret_date"], adults
            )
            raw_items.append((candidate, flight_info, flight_info["link"], hotel_link, "budget"))
    else:
        with_price = sorted([(c, f) for c, f in results if f], key=lambda x: x[1]["price"])
        top3_fallback = with_price[:3]
        if top3_fallback:
            log_event(user_id, username, "search_over_budget")
            for candidate, flight_info in top3_fallback:
                hotel_link = build_hotel_link(
                    candidate["city_en"], candidate["dep_date"], candidate["ret_date"], adults
                )
                raw_items.append(
                    (candidate, flight_info, flight_info["link"], hotel_link, "over_budget")
                )
        else:
            log_event(user_id, username, "search_no_price")
            for candidate in candidates[:3]:
                search_link = build_partner_link(
                    origin_iata, candidate["iata"],
                    candidate["dep_date"], candidate["ret_date"], adults,
                )
                hotel_link = build_hotel_link(
                    candidate["city_en"], candidate["dep_date"], candidate["ret_date"], adults
                )
                raw_items.append((candidate, None, search_link, hotel_link, "no_price"))

    # Affiliatize all links in one API call
    all_raw_urls = list({
        url for _, _, fl, hl, _ in raw_items for url in (fl, hl) if url
    })
    aff_map = await affiliatize_links(all_raw_urls)

    budget_fmt = f"{budget:,}".replace(",", "\u202f")
    for candidate, flight_info, raw_flight, raw_hotel, mode in raw_items:
        flight_link = aff_map.get(raw_flight, raw_flight) if raw_flight else None
        hotel_link = aff_map.get(raw_hotel, raw_hotel) if raw_hotel else None

        if mode == "budget":
            price_label = _fmt_price(flight_info["price"], adults)
            text = (
                f"✈️ *{candidate['city']}, {candidate['country']}*\n"
                f"📅 {candidate['dep_date']} — {candidate['ret_date']}\n\n"
                f"🎫 Билет в бюджете: *{price_label}*"
            )
            markup = kb_result(
                flight_link, flight_info["price"] * adults, hotel_link=hotel_link
            )
        elif mode == "over_budget":
            price_label = _fmt_price(flight_info["price"], adults)
            text = (
                f"✈️ *{candidate['city']}, {candidate['country']}*\n"
                f"📅 {candidate['dep_date']} — {candidate['ret_date']}\n\n"
                f"⚠️ В бюджет {budget_fmt} ₽ не вписались.\n"
                f"Ближайший вариант: *{price_label}*"
            )
            markup = kb_result(
                flight_link, flight_info["price"] * adults, hotel_link=hotel_link
            )
        else:
            text = (
                f"✈️ *{candidate['city']}, {candidate['country']}*\n"
                f"📅 {candidate['dep_date']} — {candidate['ret_date']}\n\n"
                f"🔍 Актуальные цены — по кнопке ниже"
            )
            markup = kb_result(None, None, hotel_link=hotel_link, search_link=flight_link)

        await message.answer(text, parse_mode="Markdown", reply_markup=markup)
        shown.append((candidate, flight_info))

    shown_iata = [c["iata"] for c, _ in shown]
    last_candidate = shown[-1][0] if shown else (candidates[0] if candidates else {})
    await state.update_data(
        tried_iata=tried_iata + shown_iata,
        last_candidate=last_candidate,
    )
    await state.set_state(TravelForm.result)


# ---------------------------------------------------------------------------
# Handlers — admin
# ---------------------------------------------------------------------------

async def cmd_stats(message: Message) -> None:
    if not ADMIN_ID or str(message.from_user.id) != ADMIN_ID:
        return
    s = get_stats()
    rate = (
        f"{s['found_30d'] / s['searches_30d'] * 100:.0f}%"
        if s["searches_30d"] else "—"
    )
    await message.answer(
        f"📊 *Метрики бота*\n\n"
        f"👥 MAU (30 дней): *{s['mau']}*\n"
        f"👤 DAU (сегодня): *{s['dau']}*\n"
        f"🌐 Всего пользователей: *{s['total_users']}*\n"
        f"🔍 Поисков за 30 дней: *{s['searches_30d']}*\n"
        f"✅ Найдено в бюджете: *{s['found_30d']}* ({rate})",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Handlers — origin
# ---------------------------------------------------------------------------

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

_BUDGET_LABELS = {
    "50000":  "до 50 000 ₽",
    "100000": "до 100 000 ₽",
}


async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    log_event(user.id if user else 0, user.username if user else None, "start")
    await message.answer(
        "✈️ *Привет! Давай спланируем путешествие.*\n\nИз какого города летишь?",
        parse_mode="Markdown",
        reply_markup=kb_origin(),
    )
    await state.set_state(TravelForm.origin)


async def cb_origin(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":")[1]
    if value == "custom":
        await callback.message.edit_text("Напиши название своего города:")
        await callback.answer()
        return
    city_name = next((k for k, v in POPULAR_ORIGINS.items() if v == value), value)
    await callback.message.edit_text(f"Вылет из: 🛫 {city_name}")
    await state.update_data(origin_iata=value, origin_name=city_name)
    await callback.answer()
    await callback.message.answer("Какой отдых предпочитаешь?", reply_markup=kb_vacation_type())
    await state.set_state(TravelForm.vacation_type)


async def process_origin(message: Message, state: FSMContext) -> None:
    city = message.text.strip()
    iata = get_iata_code(city) or await get_iata_code_online(city)
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

async def cb_vacation_type(callback: CallbackQuery, state: FSMContext) -> None:
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


async def process_vacation_type(message: Message, state: FSMContext) -> None:
    await state.update_data(vacation_type=message.text)
    await message.answer("С кем едешь?", reply_markup=kb_companions())
    await state.set_state(TravelForm.companions)


# ---------------------------------------------------------------------------
# Handlers — companions
# ---------------------------------------------------------------------------

async def cb_companions(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":")[1]
    await callback.message.edit_text(f"Путешественники: {_CP_LABELS.get(value, value)}")
    await state.update_data(companions=value)
    await callback.answer()
    await callback.message.answer("Нужен ли багаж?", reply_markup=kb_baggage())
    await state.set_state(TravelForm.baggage)


async def process_companions(message: Message, state: FSMContext) -> None:
    await state.update_data(companions=message.text)
    await message.answer("Нужен ли багаж?", reply_markup=kb_baggage())
    await state.set_state(TravelForm.baggage)


# ---------------------------------------------------------------------------
# Handlers — baggage & transfers
# ---------------------------------------------------------------------------

_BG_LABELS = {
    "with":    "✅ С багажом",
    "without": "🎒 Только ручная кладь",
    "any":     "🤷 Неважно",
}

_TR_LABELS = {
    "direct": "✈️ Только прямые",
    "one":    "1️⃣ До 1 пересадки",
    "any":    "🤷 Любые",
}


async def cb_baggage(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":")[1]
    await callback.message.edit_text(f"Багаж: {_BG_LABELS.get(value, value)}")
    await state.update_data(baggage=value)
    await callback.answer()
    await callback.message.answer("Сколько пересадок допустимо?", reply_markup=kb_transfers())
    await state.set_state(TravelForm.transfers)


async def cb_transfers(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":")[1]
    await callback.message.edit_text(f"Пересадки: {_TR_LABELS.get(value, value)}")
    await state.update_data(transfers=value)
    await callback.answer()
    await callback.message.answer("Какой бюджет на человека?", reply_markup=kb_budget())
    await state.set_state(TravelForm.budget)


# ---------------------------------------------------------------------------
# Handlers — budget
# ---------------------------------------------------------------------------

async def cb_budget(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":")[1]
    if value == "custom":
        await callback.message.edit_text(
            "Введи свой бюджет в рублях на человека (например: 75000):"
        )
        await callback.answer()
        await state.set_state(TravelForm.budget_custom)
        return
    await callback.message.edit_text(f"Бюджет: {_BUDGET_LABELS.get(value, value + ' ₽')}")
    await state.update_data(budget=value)
    await callback.answer()
    await callback.message.answer("В какой месяц хочешь поехать?", reply_markup=kb_dates())
    await state.set_state(TravelForm.dates)


async def process_budget_custom(message: Message, state: FSMContext) -> None:
    digits = re.sub(r"[^\d]", "", message.text.strip())
    if not digits:
        await message.answer("Пожалуйста, введи число, например: 75000")
        return
    await state.update_data(budget=digits)
    amount = f"{int(digits):,}".replace(",", "\u202f")
    await message.answer(f"Бюджет: {amount} ₽")
    await message.answer("В какой месяц хочешь поехать?", reply_markup=kb_dates())
    await state.set_state(TravelForm.dates)


async def process_budget(message: Message, state: FSMContext) -> None:
    await state.update_data(budget=message.text)
    await message.answer("В какой месяц хочешь поехать?", reply_markup=kb_dates())
    await state.set_state(TravelForm.dates)


# ---------------------------------------------------------------------------
# Handlers — dates
# ---------------------------------------------------------------------------

async def cb_dates(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":")[1]
    if value == "custom":
        await callback.message.edit_text(
            "Напиши когда хочешь поехать:\n\n"
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


async def process_dates(message: Message, state: FSMContext) -> None:
    await state.update_data(dates=message.text)
    await message.answer("⏳ Подбираю направление...")
    await _run_search(message, state)


# ---------------------------------------------------------------------------
# Handlers — result actions
# ---------------------------------------------------------------------------

async def cb_result_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    user = callback.from_user
    user_id = user.id if user else 0
    username = user.username if user else None

    if action == "restart":
        log_event(user_id, username, "action_restart")
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
        log_event(user_id, username, "action_another")
        user_data = await state.get_data()
        tried = list(user_data.get("tried_iata", []))
        last = user_data.get("last_candidate", {})
        if last and last.get("iata") and last["iata"] not in tried:
            tried.append(last["iata"])
        await state.update_data(tried_iata=tried)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Ищу другое направление…")
        await callback.message.answer("⏳ Ищу другое направление...")
        await _run_search(callback.message, state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    init_db()
    _load_iata_codes()
    await _init_redis()

    # Problem 2: persist FSM in Redis, fall back to memory if unavailable
    if redis_client:
        from aiogram.fsm.storage.redis import RedisStorage
        storage = RedisStorage.from_url(REDIS_URL)
        logging.info("FSM: RedisStorage")
    else:
        from aiogram.fsm.storage.memory import MemoryStorage
        storage = MemoryStorage()
        logging.warning("FSM: MemoryStorage — sessions lost on restart")

    bot = Bot(token=TG_BOT_TOKEN)
    dp = Dispatcher(storage=storage)

    # Problem 3: register rate-limit middleware
    dp.message.middleware(RateLimitMiddleware())
    dp.callback_query.middleware(RateLimitMiddleware())

    dp.message.register(start, Command("start"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(process_origin, TravelForm.origin)
    dp.message.register(process_vacation_type, TravelForm.vacation_type)
    dp.message.register(process_companions, TravelForm.companions)
    dp.message.register(process_budget, TravelForm.budget)
    dp.message.register(process_budget_custom, TravelForm.budget_custom)
    dp.message.register(process_dates, TravelForm.dates)

    dp.callback_query.register(cb_origin, TravelForm.origin, F.data.startswith("org:"))
    dp.callback_query.register(cb_vacation_type, TravelForm.vacation_type, F.data.startswith("vt:"))
    dp.callback_query.register(cb_companions, TravelForm.companions, F.data.startswith("cp:"))
    dp.callback_query.register(cb_baggage, TravelForm.baggage, F.data.startswith("bg:"))
    dp.callback_query.register(cb_transfers, TravelForm.transfers, F.data.startswith("tr:"))
    dp.callback_query.register(cb_budget, TravelForm.budget, F.data.startswith("bgt:"))
    dp.callback_query.register(cb_dates, TravelForm.dates, F.data.startswith("dt:"))
    dp.callback_query.register(cb_result_action, TravelForm.result, F.data.startswith("action:"))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
