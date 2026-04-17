"""
Tests for Travel Bot components.
Run: pytest tests/test_bot.py -v
Requires: pip install pytest pytest-asyncio aioresponses
"""

import asyncio
import csv
import os
import re
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Patch env before importing bot
os.environ.setdefault("TG_BOT_TOKEN", "fake:token")
os.environ.setdefault("AVIASALES_TOKEN", "fake_aviasales")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("OLLAMA_MODEL", "qwen2.5:0.5b")

import bot as b


# ---------------------------------------------------------------------------
# normalize_date
# ---------------------------------------------------------------------------

def test_normalize_date_already_correct():
    result = b.normalize_date("2026-08-01")
    assert result == "2026-08-01"

def test_normalize_date_dd_mm_yyyy():
    result = b.normalize_date("25-04-2026")
    assert result == "2026-04-25"

def test_normalize_date_dd_dot_mm_yyyy():
    result = b.normalize_date("05.06.2026")
    assert result == "2026-06-05"

def test_normalize_date_russian_month():
    result = b.normalize_date("30 апреля 2026")
    assert result == "2026-04-30"

def test_normalize_date_russian_month_no_year():
    # Should use current or next year
    result = b.normalize_date("10 июня")
    assert result is not None
    assert result.endswith("-06-10")

def test_normalize_date_invalid():
    result = b.normalize_date("не дата вообще")
    assert result is None


# ---------------------------------------------------------------------------
# clean_city
# ---------------------------------------------------------------------------

def test_clean_city_strips_parenthetical():
    assert b.clean_city("Барселона (Испания)") == "Барселона"

def test_clean_city_no_parenthetical():
    assert b.clean_city("Стамбул") == "Стамбул"

def test_clean_city_nested():
    assert b.clean_city("Бангкок (Таиланд)") == "Бангкок"


# ---------------------------------------------------------------------------
# get_iata_code_csv
# ---------------------------------------------------------------------------

def _make_csv(rows: list[dict], tmp_path) -> str:
    path = tmp_path / "codes.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["City", "IATA"])
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def test_iata_csv_found(tmp_path):
    csv_path = _make_csv([{"City": "Bangkok", "IATA": "BKK"}], tmp_path)
    assert b.get_iata_code_csv("Bangkok", csv_path) == "BKK"


def test_iata_csv_case_insensitive(tmp_path):
    csv_path = _make_csv([{"City": "Paris", "IATA": "CDG"}], tmp_path)
    assert b.get_iata_code_csv("paris", csv_path) == "CDG"
    assert b.get_iata_code_csv("PARIS", csv_path) == "CDG"


def test_iata_csv_not_found(tmp_path):
    csv_path = _make_csv([{"City": "London", "IATA": "LHR"}], tmp_path)
    assert b.get_iata_code_csv("Tokyo", csv_path) is None


def test_iata_csv_missing_file():
    assert b.get_iata_code_csv("Anywhere", "/nonexistent/path.csv") is None


# ---------------------------------------------------------------------------
# get_iata_code_online (mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_iata_online_found(aioresponses):
    aioresponses.get(
        re.compile(r"https://www\.travelpayouts\.com/widgets_suggest_params.*"),
        payload={"destination": {"iata": "BKK", "name": "Bangkok"}},
        repeat=True,
    )
    result = await b.get_iata_code_online("Бангкок")
    assert result == "BKK"


@pytest.mark.asyncio
async def test_iata_online_empty_response(aioresponses):
    aioresponses.get(
        re.compile(r"https://www\.travelpayouts\.com/widgets_suggest_params.*"),
        payload={},
        repeat=True,
    )
    result = await b.get_iata_code_online("UnknownCity")
    assert result is None


# ---------------------------------------------------------------------------
# get_travel_idea (mocked OpenAI client)
# ---------------------------------------------------------------------------

class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeChat:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        return _FakeCompletion(self._content)


class _FakeClient:
    def __init__(self, content):
        self.chat = type("C", (), {"completions": _FakeChat(content)})()


def test_travel_idea_parses_valid(monkeypatch):
    monkeypatch.setattr(
        b,
        "OpenAI",
        lambda api_key, base_url: _FakeClient("Бангкок, Таиланд, 2026-08-01, 2026-08-10"),
    )
    direction, city, country, dep, ret, raw = b.get_travel_idea("пляжный", "2", "80000", "август 2026")
    assert city == "Бангкок"
    assert country == "Таиланд"
    assert dep == "2026-08-01"
    assert ret == "2026-08-10"
    assert direction == "Бангкок, Таиланд"


def test_travel_idea_with_exclude(monkeypatch):
    monkeypatch.setattr(
        b,
        "OpenAI",
        lambda api_key, base_url: _FakeClient("Стамбул, Турция, 2025-08-01, 2025-08-10"),
    )
    direction, city, country, dep, ret, raw = b.get_travel_idea(
        "пляжный", "2", "40000", "август 2025", exclude=["Бангкок"]
    )
    assert city == "Стамбул"
    assert country == "Турция"


def test_travel_idea_returns_none_on_bad_format(monkeypatch):
    monkeypatch.setattr(
        b,
        "OpenAI",
        lambda api_key, base_url: _FakeClient("Не могу определить направление."),
    )
    direction, city, country, dep, ret, raw = b.get_travel_idea("пляжный", "2", "80000", "август 2025")
    assert city is None
    assert direction is None


def test_travel_idea_handles_llm_exception(monkeypatch):
    class _BrokenClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise ConnectionError("Ollama is not running")

    monkeypatch.setattr(b, "OpenAI", lambda api_key, base_url: _BrokenClient())
    direction, city, country, dep, ret, raw = b.get_travel_idea("пляжный", "1", "50000", "май 2025")
    assert city is None
    assert "Ollama" in raw


# ---------------------------------------------------------------------------
# search_aviasales (mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aviasales_returns_cheapest_flight(aioresponses):
    # v1/prices/cheap response format: data keyed by destination IATA
    aioresponses.get(
        re.compile(r"https://api\.travelpayouts\.com/v1/prices/cheap.*"),
        payload={
            "success": True,
            "data": {
                "BKK": {
                    "0": {
                        "price": 85000,
                        "airline": "SU",
                        "departure_at": "2026-08-01T10:00:00Z",
                        "return_at": "2026-08-10T15:00:00Z",
                    }
                }
            },
        },
        repeat=True,
    )
    result = await b.search_aviasales("MOW", "BKK", "2026-08-01", "2026-08-10")
    assert result is not None
    assert result["price"] == 85000
    assert "aviasales.ru" in result["link"]
    assert "MOW" in result["link"]
    assert "BKK" in result["link"]


@pytest.mark.asyncio
async def test_aviasales_empty_response(aioresponses):
    aioresponses.get(
        re.compile(r"https://api\.travelpayouts\.com/v1/prices/cheap.*"),
        payload={"success": True, "data": {}},
        repeat=True,
    )
    result = await b.search_aviasales("MOW", "NYC", "2026-08-01", "2026-08-10")
    assert result is None


@pytest.mark.asyncio
async def test_aviasales_missing_iata():
    result = await b.search_aviasales(None, "BKK", "2026-08-01", "2026-08-10")
    assert result is None

    result = await b.search_aviasales("MOW", None, "2026-08-01", "2026-08-10")
    assert result is None


# ---------------------------------------------------------------------------
# Hotellook tests — disabled (API temporarily unavailable)
# ---------------------------------------------------------------------------
# Uncomment when Hotellook API is restored and functions are re-enabled in bot.py
#
# @pytest.mark.asyncio
# async def test_hotellook_location_id_found(aioresponses): ...
# @pytest.mark.asyncio
# async def test_hotellook_hotel_found(aioresponses): ...
