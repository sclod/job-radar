"""Конвейер fetch на фикстурах через httpx.MockTransport (без сети)."""

import json
import logging

import httpx
import pytest
from conftest import fixture_text

from jobradar.config import QueryConfig, SourceConfig
from jobradar.fetcher import run_fetch
from jobradar.http import HttpClient
from jobradar.models import DEFERMENT_MENTIONED, DEFERMENT_YES


class FakeSites:
    """Отдаёт фикстуры вместо work.ua и DOU и запоминает запросы."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, int] = {}  # подстрока URL -> HTTP-статус

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for part, status in self.fail.items():
            if part in url:
                return httpx.Response(status, text="error")
        if url.startswith("https://www.work.ua/jobs-python/"):
            name = "workua_list_page2.html" if "page=2" in url else "workua_list.html"
            return httpx.Response(200, text=fixture_text(name))
        if url.startswith("https://www.work.ua/jobs/"):
            return httpx.Response(200, text=fixture_text("workua_vacancy.html"))
        if "/vacancies/xhr-load/" in url:
            return httpx.Response(200, text=fixture_text("dou_xhr.json"), headers={"content-type": "application/json"})
        if url.startswith("https://jobs.dou.ua/vacancies/"):
            return httpx.Response(
                200, text=fixture_text("dou_list.html"), headers={"set-cookie": "csrftoken=cookie-token; Path=/"}
            )
        if url.startswith("https://jobs.dou.ua/companies/"):
            return httpx.Response(200, text=fixture_text("dou_vacancy.html"))
        return httpx.Response(404)

    def urls(self, method: str = "GET") -> list[str]:
        return [str(r.url) for r in self.requests if r.method == method]


@pytest.fixture
def sites():
    return FakeSites()


@pytest.fixture
def http(config, sites):
    client = HttpClient(config.http, transport=httpx.MockTransport(sites), sleep=lambda s: None)
    yield client
    client.close()


def test_first_run_collects_and_filters(config, db, http, sites):
    result = run_fetch(config, db, http)

    titles = sorted(v.title for v in result.new_vacancies)
    assert titles == sorted([
        # work.ua: 2 страницы, дубль 5900001 схлопнут, Senior отфильтрован
        "Junior Python розробник", "Python Developer (FastAPI)", "Стажист Python / Data", "Python QA Automation",
        # DOU: первая страница + XHR, Team Lead отфильтрован, дубль из XHR схлопнут
        "Junior Python Developer", "Trainee Python Engineer", "Junior Data Engineer (Python)",
    ])
    assert result.stats["workua"].filtered == 1
    assert result.stats["dou"].filtered == 1
    assert result.total.new == 7
    assert result.total.errors == 0

    stored = {v.title: v for v in db.vacancies(run_id=result.run_id)}
    assert len(stored) == 7
    # work.ua с ?deferment=1 — бронирование подтверждено фильтром
    assert all(v.deferment == DEFERMENT_YES for v in stored.values() if v.source == "workua")
    # DOU: бронирование упомянуто в тексте вакансии — помечено для проверки
    assert stored["Junior Python Developer"].deferment == DEFERMENT_MENTIONED
    assert "Необхідні навички" in stored["Junior Python Developer"].description

    # DOU «Більше вакансій»: POST с CSRF из cookie и числом уже загруженных
    (xhr,) = [r for r in sites.requests if r.method == "POST"]
    assert xhr.url == "https://jobs.dou.ua/vacancies/xhr-load/?category=Python&exp=0-1"
    assert b"csrfmiddlewaretoken=cookie-token" in xhr.content
    assert b"count=3" in xhr.content


def test_second_run_finds_nothing_new(config, db, http, sites):
    run_fetch(config, db, http)
    requests_before = len(sites.requests)

    result = run_fetch(config, db, http)

    assert result.total.new == 0
    assert result.total.found == 7
    assert db.vacancies(run_id=result.run_id) == []
    # известные вакансии повторно не открываются — только страницы списков
    new_requests = sites.requests[requests_before:]
    assert not [r for r in new_requests if "/jobs/59" in str(r.url) or "/companies/" in str(r.url)]


def test_broken_source_does_not_stop_others(config, db, http, sites, caplog):
    sites.fail["www.work.ua"] = 503

    with caplog.at_level(logging.WARNING):
        result = run_fetch(config, db, http)

    assert result.stats["workua"].errors == 1
    assert result.stats["workua"].found == 0
    assert result.stats["dou"].new == 3
    assert "work.ua [python]: ошибка запроса" in caplog.text
    # 503 повторяется один раз, не больше
    assert len([u for u in sites.urls() if u.startswith("https://www.work.ua/")]) == 2


def test_failed_detail_page_is_retried_next_run(config, db, http, sites):
    sites.fail["/jobs/5900002/"] = 500
    first = run_fetch(config, db, http)
    assert "Python Developer (FastAPI)" not in {v.title for v in first.new_vacancies}
    assert first.stats["workua"].errors == 1

    del sites.fail["/jobs/5900002/"]
    second = run_fetch(config, db, http)
    assert [v.title for v in second.new_vacancies] == ["Python Developer (FastAPI)"]


def test_detail_limit_postpones_rest(config, db, http):
    config.http.max_details_per_run = 2
    config.sources = {"workua": config.sources["workua"]}

    first = run_fetch(config, db, http)
    assert first.stats["workua"].new == 2
    assert first.stats["workua"].postponed == 2

    second = run_fetch(config, db, http)
    assert second.stats["workua"].new == 2
    assert second.stats["workua"].postponed == 0


def test_max_pages_is_respected(config, db, http, sites):
    config.http.max_pages = 1
    run_fetch(config, db, http)

    assert not [u for u in sites.urls() if "page=2" in u]
    assert sites.urls("POST") == []


def test_query_keywords_override_global(config, db, http):
    config.sources = {
        "workua": SourceConfig(
            queries=[QueryConfig(url="https://www.work.ua/jobs-python/?deferment=1", include=["fastapi"], exclude=[])]
        )
    }
    result = run_fetch(config, db, http)
    assert [v.title for v in result.new_vacancies] == ["Python Developer (FastAPI)"]


def test_stub_source_is_skipped(config, db, http, sites, caplog):
    config.sources = {"robotaua": SourceConfig(queries=[QueryConfig(url="https://robota.ua/zapros/python")])}

    with caplog.at_level(logging.WARNING):
        result = run_fetch(config, db, http)

    assert result.total.new == 0
    assert sites.requests == []
    assert "robota.ua: парсер не реализован" in caplog.text


def test_details_can_be_disabled(config, db, http, sites):
    config.http.fetch_details = False
    result = run_fetch(config, db, http)

    assert result.total.new == 7
    assert not [u for u in sites.urls() if "/jobs/59" in u or "/companies/" in u]
    stored = db.vacancies(run_id=result.run_id)
    assert all(v.description is None and v.snippet for v in stored)


def test_save_html(config, db, sites, tmp_path):
    save_dir = tmp_path / "debug"
    with HttpClient(config.http, transport=httpx.MockTransport(sites), sleep=lambda s: None, save_dir=save_dir) as http:
        run_fetch(config, db, http, only=["dou"])

    saved = sorted(p.name for p in save_dir.iterdir())
    assert saved[0].startswith("001_jobs_dou_ua_vacancies")
    assert any(name.endswith(".json") for name in saved)
    json.loads(next(p for p in save_dir.iterdir() if p.suffix == ".json").read_text())
