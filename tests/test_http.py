"""HTTP-слой: выбор бэкенда (curl_cffi / httpx), откат на httpx, ретраи. Без сети."""

import logging
import sys
from types import SimpleNamespace

import httpx
import pydantic
import pytest
from test_fetcher import FakeSites

from jobradar.config import HttpSettings
from jobradar.fetcher import run_fetch
from jobradar.http import FetchError, HttpClient
from jobradar.models import DEFERMENT_MENTIONED, DEFERMENT_YES

curl_requests = pytest.importorskip("curl_cffi.requests")


class FakeCurlSession:
    """Подмена curl_cffi.requests.Session: отвечает через обработчик httpx.Request → httpx.Response."""

    def __init__(self, handler, **kwargs):
        self.handler = handler
        self.kwargs = kwargs
        self.cookies: dict[str, str] = {}
        self.calls = []
        self.closed = False

    def request(self, method, url, data=None, headers=None, **kwargs):
        self.calls.append(SimpleNamespace(method=method, url=url, data=data, headers=headers))
        response = self.handler(httpx.Request(method, url, data=data, headers=headers))
        if cookie := response.headers.get("set-cookie"):
            name, _, rest = cookie.partition("=")
            self.cookies[name] = rest.split(";")[0]
        return SimpleNamespace(status_code=response.status_code, text=response.text, headers=response.headers)

    def close(self):
        self.closed = True


@pytest.fixture
def curl(monkeypatch):
    """Бэкенд curl_cffi без сети: сессии отдают фикстуры work.ua и DOU."""
    state = SimpleNamespace(sites=FakeSites(), sessions=[], handler=None)

    def factory(**kwargs):
        session = FakeCurlSession(state.handler or state.sites, **kwargs)
        state.sessions.append(session)
        return session

    monkeypatch.setattr(curl_requests, "Session", factory)
    return state


def test_curl_cffi_is_default(curl):
    with HttpClient(HttpSettings(timeout_seconds=7)) as http:
        assert http.backend == "curl_cffi"
    (session,) = curl.sessions
    assert session.kwargs["impersonate"] == "chrome"
    assert session.kwargs["timeout"] == 7
    # UA и прочие заголовки браузера ставит сам curl_cffi — свой UA не подсовываем
    assert set(session.kwargs["headers"]) == {"Accept-Language"}
    assert session.closed


def test_impersonate_from_config(curl):
    HttpClient(HttpSettings(impersonate="safari")).close()
    HttpClient(HttpSettings(impersonate="chrome131")).close()
    assert [s.kwargs["impersonate"] for s in curl.sessions] == ["safari", "chrome131"]


def test_unknown_impersonate_falls_back_to_chrome(curl, caplog):
    with caplog.at_level(logging.WARNING):
        HttpClient(HttpSettings(impersonate="chrome999")).close()
    assert curl.sessions[0].kwargs["impersonate"] == "chrome"
    assert "impersonate='chrome999' не поддерживается" in caplog.text


def test_httpx_backend_from_config(curl):
    with HttpClient(HttpSettings(backend="httpx")) as http:
        assert http.backend == "httpx"
    assert curl.sessions == []


def test_falls_back_to_httpx_without_curl_cffi(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "curl_cffi", None)  # import curl_cffi → ImportError

    with caplog.at_level(logging.WARNING):
        http = HttpClient(HttpSettings())

    assert http.backend == "httpx"
    assert "curl_cffi не установлен — работаю через httpx" in caplog.text
    http.close()


def test_unknown_backend_is_config_error():
    with pytest.raises(pydantic.ValidationError):
        HttpSettings(backend="requests")


def test_curl_errors_are_retried_once(curl):
    calls = []

    def failing(request):
        calls.append(request)
        raise curl_requests.RequestsError("Failed to perform, curl: (35) TLS connect error", 35)

    curl.handler = failing
    with HttpClient(HttpSettings(delay_seconds=0), sleep=lambda s: None) as http:
        with pytest.raises(FetchError, match="TLS connect error"):
            http.get("https://www.work.ua/jobs-python/?deferment=1")
    assert len(calls) == 2


def test_http_403_is_fetch_error(curl):
    curl.sites.fail["work.ua"] = 403
    with HttpClient(HttpSettings(delay_seconds=0), sleep=lambda s: None) as http:
        with pytest.raises(FetchError, match="HTTP 403"):
            http.get("https://www.work.ua/jobs-python/?deferment=1")


def test_full_fetch_through_curl_cffi(curl, config, db):
    with HttpClient(config.http, sleep=lambda s: None) as http:
        assert http.backend == "curl_cffi"
        result = run_fetch(config, db, http)

    assert result.total.new == 7
    assert result.total.errors == 0
    stored = {v.title: v for v in db.vacancies(run_id=result.run_id)}
    assert all(v.deferment == DEFERMENT_YES for v in stored.values() if v.source == "workua")
    assert stored["Junior Python Developer"].deferment == DEFERMENT_MENTIONED
    # DOU «Більше вакансій»: CSRF взят из cookies сессии curl_cffi
    (xhr,) = [c for s in curl.sessions for c in s.calls if c.method == "POST"]
    assert xhr.data == {"csrfmiddlewaretoken": "cookie-token", "count": "3"}
