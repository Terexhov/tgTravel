import pytest
from aioresponses import aioresponses as _aioresponses


@pytest.fixture
def aioresponses():
    with _aioresponses() as m:
        yield m
