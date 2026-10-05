import socket
from datetime import date
from pathlib import Path

import pytest

from jobradar.config import AISettings, Config, HttpSettings, Keywords, QueryConfig, SourceConfig
from jobradar.db import Database
from jobradar.models import Vacancy

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 4)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Тесты не ходят в сеть: любая попытка открыть соединение — ошибка."""

    def guard(*args, **kwargs):
        raise RuntimeError("сеть в тестах запрещена")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    # curl_cffi открывает соединения внутри libcurl, мимо модуля socket
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return
    monkeypatch.setattr(curl_requests.Session, "request", guard)


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def config(tmp_path) -> Config:
    profile = tmp_path / "profile.md"
    profile.write_text("Junior Python developer: Python, Django, pytest, PostgreSQL.", encoding="utf-8")
    return Config(
        database=tmp_path / "test.db",
        profile=profile,
        reports_dir=tmp_path / "reports",
        http=HttpSettings(delay_seconds=0, max_pages=3, max_details_per_run=50),
        keywords=Keywords(exclude=["senior", "lead"]),
        sources={
            "workua": SourceConfig(queries=[QueryConfig(name="python", url="https://www.work.ua/jobs-python/?deferment=1")]),
            "dou": SourceConfig(queries=[QueryConfig(name="python", url="https://jobs.dou.ua/vacancies/?category=Python&exp=0-1")]),
            "robotaua": SourceConfig(enabled=False),
        },
        ai=AISettings(batch_size=2, concurrency=1, max_attempts=3),
    )


def make_vacancy(n: int = 1, **overrides) -> Vacancy:
    data = dict(
        source="workua",
        url=f"https://www.work.ua/jobs/{1000 + n}/",
        title=f"Python Developer {n}",
        company="Альфа",
        city="Київ",
        salary="30 000 грн",
        published_at=date(2026, 10, 1),
        description="Python, Django, PostgreSQL.",
    )
    data.update(overrides)
    return Vacancy(**data)
