"""
Tests for Travel Bot components.
Run: pytest tests/test_bot.py -v
Requires: pip install pytest pytest-asyncio aioresponses
"""

import csv
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("TG_BOT_TOKEN", "fake:token")
os.environ.setdefault("AVIASALES_TOKEN", "fake_aviasales")
os.environ.setdefault("OPENROUTER_API_KEY", "fake_openrouter")

import bot as b


# ---------------------------------------------------------------------------
# normalize_date
# ---------------------------------------------------------------------------

def test_normalize_date_iso():
    assert b.normalize_date("2026-08-01") == "2026-08-01"


def test_normalize_date_dd_mm_yyyy():
    assert b.normalize_date("25-04-2026") == "2026-04-25"


def test_normalize_date_dot_separator():
    assert b.normalize_date("05.06.2026") == "2026-06-05"


def test_normalize_date_russian_month_with_year():
    assert b.normalize_date("30 апреля 2026") == "2026-04-30"


def test_normalize_date_russian_month_no_year():
    result = b.normalize_date("10 июня")
    assert result is not None
    assert result.endswith("-06-10")


def test_normalize_date_invalid():
    assert b.normalize_date("не дата") is None


def test_normalize_date_bumps_past_month():
    # If month is already past, year should be bumped
    result = b.normalize_date("01-01-2020")
    assert result is not None
    year = int(result[:4])
    from datetime import date
    assert year >= date.today().year


# ---------------------------------------------------------------------------
# clean_city
# ---------------------------------------------------------------------------

def test_clean_city_strips_country():
    assert b.clean_city("Барселона (Испания)") == "Барселона"


def test_clean_city_no_suffix():
    assert b.clean_city("Стамбул") == "Стамбул"


def test_clean_city_trims_whitespace():
    assert b.clean_city("  Токио  ") == "Токио"


# ---------------------------------------------------------------------------
# Problem 6: IATA in-memory cache
# ---------------------------------------------------------------------------

def _make_csv(rows: list[dict], tmp_path) -> str:
    path = tmp_path / "codes.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["City", "IATA"])
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def test_load_iata_codes_populates_dicts(tmp_path):
    csv_path = _make_csv([
        {"City": "Bangkok", "IATA": "BKK"},
        {"City": "Istanbul", "IATA": "IST"},
    ], tmp_path)
    b._IATA_BY_CITY.clear()
    b._IATA_CODES_SET.clear()
    b._load_iata_codes(csv_path)
    assert b._IATA_BY_CITY["bangkok"] == "BKK"
    assert b._IATA_BY_CITY["istanbul"] == "IST"
    assert "BKK" in b._IATA_CODES_SET
    assert "IST" in b._IATA_CODES_SET


def test_get_iata_code_hit(tmp_path):
    b._IATA_BY_CITY["paris"] = "CDG"
    assert b.get_iata_code("Paris") == "CDG"
    assert b.get_iata_code("paris") == "CDG"


def test_get_iata_code_miss():
    b._IATA_BY_CITY.pop("atlantis", None)
    assert b.get_iata_code("Atlantis") is None


def test_load_iata_codes_missing_file():
    b._IATA_BY_CITY.clear()
    b._load_iata_codes("/nonexistent/path.csv")
    assert len(b._IATA_BY_CITY) == 0


# ---------------------------------------------------------------------------
# Link builders
# ---------------------------------------------------------------------------

def test_build_partner_link_no_marker(monkeypatch):
    monkeypatch.setattr(b, "AVIASALES_MARKER", "")
    link = b.build_partner_link("MOW", "BKK", "2026-08-01", "2026-08-10")
    assert "MOW" in link
    assert "BKK" in link
    assert "marker" not in link


def test_build_partner_link_with_marker(monkeypatch):
    monkeypatch.setattr(b, "AVIASALES_MARKER", "12345")
    link = b.build_partner_link("MOW", "BKK", "2026-08-01", "2026-08-10")
    assert "marker=12345" in link


def test_build_hotel_link_no_affiliate(monkeypatch):
    monkeypatch.setattr(b, "AVIASALES_MARKER", "")
    monkeypatch.setattr(b, "TP_TRS", "")
    monkeypatch.setattr(b, "TP_HOTEL_PROGRAM", "")
    link = b.build_hotel_link("bangkok", "2026-08-01", "2026-08-10", adults=2)
    assert "travel.yandex.ru/hotels/bangkok" in link
    assert "adults=2" in link
    assert "checkinDate=2026-08-01" in link
    assert "checkoutDate=2026-08-10" in link


def test_build_hotel_link_with_affiliate(monkeypatch):
    monkeypatch.setattr(b, "AVIASALES_MARKER", "M")
    monkeypatch.setattr(b, "TP_TRS", "T")
    monkeypatch.setattr(b, "TP_HOTEL_PROGRAM", "P")
    link = b.build_hotel_link("dubai", "2026-12-01", "2026-12-08")
    assert "tp.media/r" in link
    assert "marker=M" in link
    assert "trs=T" in link
    assert "p=P" in link
    assert "travel.yandex.ru" in link  # encoded in URL


def test_build_hotel_link_spaces_in_city(monkeypatch):
    monkeypatch.setattr(b, "AVIASALES_MARKER", "")
    monkeypatch.setattr(b, "TP_TRS", "")
    monkeypatch.setattr(b, "TP_HOTEL_PROGRAM", "")
    link = b.build_hotel_link("New York", "2026-10-01", "2026-10-07")
    assert "new-york" in link


def test_parse_adults():
    assert b._parse_adults("1") == 1
    assert b._parse_adults("4") == 4
    assert b._parse_adults("10") == 6  # capped at 6
    assert b._parse_adults("abc") == 1


# ---------------------------------------------------------------------------
# Problem 4: LLM tool-calling candidates — validation logic
# ---------------------------------------------------------------------------

def _make_fake_tool_response(destinations: list[dict]) -> object:
    """Build a mock OpenAI response with tool call."""
    args_json = json.dumps({"destinations": destinations})

    class FakeFunction:
        arguments = args_json

    class FakeToolCall:
        function = FakeFunction()

    class FakeMessage:
        tool_calls = [FakeToolCall()]

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    return FakeClient()


def test_candidates_valid_iata_accepted(monkeypatch):
    b._IATA_CODES_SET.clear()
    b._IATA_CODES_SET.add("BKK")
    monkeypatch.setattr(
        b, "OpenAI",
        lambda api_key, base_url: _make_fake_tool_response([{
            "iata": "BKK",
            "city": "Бангкок",
            "city_en": "Bangkok",
            "country": "Таиланд",
            "dep_date": "2026-08-01",
            "ret_date": "2026-08-10",
        }]),
    )
    results = b.get_travel_candidates("пляжный", "2", 80000, "август 2026", [])
    assert len(results) == 1
    assert results[0]["iata"] == "BKK"
    assert results[0]["city_en"] == "Bangkok"


def test_candidates_hallucinated_iata_rejected(monkeypatch):
    b._IATA_CODES_SET.clear()
    b._IATA_CODES_SET.add("IST")
    monkeypatch.setattr(
        b, "OpenAI",
        lambda api_key, base_url: _make_fake_tool_response([{
            "iata": "XYZ",  # not in set → hallucination
            "city": "НонеСити",
            "city_en": "NonCity",
            "country": "НеРеальная",
            "dep_date": "2026-08-01",
            "ret_date": "2026-08-10",
        }]),
    )
    results = b.get_travel_candidates("пляжный", "1", 50000, "август 2026", [])
    assert len(results) == 0


def test_candidates_excluded_iata_skipped(monkeypatch):
    b._IATA_CODES_SET.clear()
    b._IATA_CODES_SET.update(["BKK", "IST"])
    monkeypatch.setattr(
        b, "OpenAI",
        lambda api_key, base_url: _make_fake_tool_response([
            {"iata": "BKK", "city": "Бангкок", "city_en": "Bangkok",
             "country": "Таиланд", "dep_date": "2026-08-01", "ret_date": "2026-08-10"},
            {"iata": "IST", "city": "Стамбул", "city_en": "Istanbul",
             "country": "Турция", "dep_date": "2026-08-05", "ret_date": "2026-08-12"},
        ]),
    )
    results = b.get_travel_candidates("пляжный", "1", 50000, "август 2026", ["BKK"])
    assert all(r["iata"] != "BKK" for r in results)
    assert any(r["iata"] == "IST" for r in results)


def test_candidates_bad_duration_rejected(monkeypatch):
    b._IATA_CODES_SET.clear()
    b._IATA_CODES_SET.add("IST")
    monkeypatch.setattr(
        b, "OpenAI",
        lambda api_key, base_url: _make_fake_tool_response([{
            "iata": "IST",
            "city": "Стамбул",
            "city_en": "Istanbul",
            "country": "Турция",
            "dep_date": "2026-08-01",
            "ret_date": "2026-08-02",  # 1 day — too short
        }]),
    )
    results = b.get_travel_candidates("пляжный", "1", 50000, "август 2026", [])
    assert len(results) == 0


def test_candidates_llm_exception_returns_empty(monkeypatch):
    class BrokenClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("API down")

    monkeypatch.setattr(b, "OpenAI", lambda api_key, base_url: BrokenClient())
    results = b.get_travel_candidates("пляжный", "1", 50000, "август 2026", [])
    assert results == []


# ---------------------------------------------------------------------------
# Problem 1: Budget filter logic
# ---------------------------------------------------------------------------

def test_budget_filter_in_budget():
    candidates = [
        {"iata": "IST", "city": "Стамбул", "city_en": "Istanbul",
         "country": "Турция", "dep_date": "2026-08-01", "ret_date": "2026-08-08"},
        {"iata": "BKK", "city": "Бангкок", "city_en": "Bangkok",
         "country": "Таиланд", "dep_date": "2026-08-01", "ret_date": "2026-08-10"},
    ]
    prices = {
        "IST": 40_000,
        "BKK": 90_000,
    }
    budget = 50_000

    in_budget = [
        (c, {"price": prices[c["iata"]], "link": "http://x"})
        for c in candidates if prices[c["iata"]] <= budget
    ]
    assert len(in_budget) == 1
    assert in_budget[0][0]["iata"] == "IST"


def test_budget_filter_all_over_picks_cheapest():
    results = [
        ({"iata": "BKK"}, {"price": 95_000, "link": "http://a"}),
        ({"iata": "DXB"}, {"price": 80_000, "link": "http://b"}),
        ({"iata": "IST"}, {"price": 110_000, "link": "http://c"}),
    ]
    budget = 50_000
    in_budget = [(c, f) for c, f in results if f["price"] <= budget]
    assert len(in_budget) == 0

    # Fallback: cheapest overall
    cheapest = min(results, key=lambda x: x[1]["price"])
    assert cheapest[0]["iata"] == "DXB"


# ---------------------------------------------------------------------------
# Aviasales API (mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aviasales_returns_cheapest(aioresponses):
    aioresponses.get(
        re.compile(r"https://api\.travelpayouts\.com/v1/prices/cheap.*"),
        payload={
            "success": True,
            "data": {
                "BKK": {
                    "0": {"price": 85000, "departure_at": "2026-08-01T10:00:00Z",
                          "return_at": "2026-08-10T15:00:00Z"},
                    "1": {"price": 72000, "departure_at": "2026-08-02T12:00:00Z",
                          "return_at": "2026-08-11T18:00:00Z"},
                }
            },
        },
        repeat=True,
    )
    result = await b.search_aviasales("MOW", "BKK", "2026-08-01", "2026-08-10")
    assert result is not None
    assert result["price"] == 72000  # picks cheapest
    assert "MOW" in result["link"]
    assert "BKK" in result["link"]


@pytest.mark.asyncio
async def test_aviasales_empty_data(aioresponses):
    aioresponses.get(
        re.compile(r"https://api\.travelpayouts\.com/v1/prices/cheap.*"),
        payload={"success": True, "data": {}},
        repeat=True,
    )
    result = await b.search_aviasales("MOW", "NYC", "2026-08-01", "2026-08-10")
    assert result is None


@pytest.mark.asyncio
async def test_aviasales_no_token(monkeypatch):
    monkeypatch.setattr(b, "AVIASALES_TOKEN", "")
    result = await b.search_aviasales("MOW", "BKK", "2026-08-01", "2026-08-10")
    assert result is None


@pytest.mark.asyncio
async def test_aviasales_missing_origin():
    assert await b.search_aviasales(None, "BKK", "2026-08-01", "2026-08-10") is None


# ---------------------------------------------------------------------------
# Online IATA lookup (mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_iata_online_found(aioresponses):
    aioresponses.get(
        re.compile(r"https://www\.travelpayouts\.com/widgets_suggest_params.*"),
        payload={"destination": {"iata": "BKK"}},
        repeat=True,
    )
    result = await b.get_iata_code_online("Бангкок")
    assert result == "BKK"
    assert "BKK" in b._IATA_CODES_SET  # dynamically added


@pytest.mark.asyncio
async def test_iata_online_empty(aioresponses):
    aioresponses.get(
        re.compile(r"https://www\.travelpayouts\.com/widgets_suggest_params.*"),
        payload={},
        repeat=True,
    )
    result = await b.get_iata_code_online("UnknownCity")
    assert result is None
