"""Вежливый HTTP-клиент: пауза между запросами, один повтор, два бэкенда.

curl_cffi (основной) повторяет TLS-отпечаток настоящего браузера: work.ua отвечает 403
клиентам, которых по отпечатку видно как Python. httpx — запасной: включается в config.yaml
(`http.backend: httpx`) или сам, если curl_cffi не установлен.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import HttpSettings

log = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 30.0
_ACCEPT_LANGUAGE = "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7"


class FetchError(Exception):
    """Источник ответил ошибкой или не ответил вовсе."""


class Response(Protocol):
    """Общее у ответов httpx и curl_cffi, чем пользуются источники."""

    status_code: int
    text: str
    headers: Mapping[str, str]


class _HttpxBackend:
    name = "httpx"

    def __init__(self, settings: HttpSettings, transport: httpx.BaseTransport | None = None) -> None:
        self.description = "httpx"
        self.errors: tuple[type[Exception], ...] = (httpx.HTTPError,)
        self._client = httpx.Client(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": _ACCEPT_LANGUAGE,
            },
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    def request(self, method: str, url: str, **kwargs) -> Response:
        return self._client.request(method, url, **kwargs)

    def close(self) -> None:
        self._client.close()


class _CurlCffiBackend:
    name = "curl_cffi"

    def __init__(self, settings: HttpSettings) -> None:
        from curl_cffi import requests as curl_requests  # ImportError → откат на httpx

        impersonate = _checked_impersonate(settings.impersonate)
        self.description = f"curl_cffi (impersonate={impersonate})"
        self.errors = (curl_requests.RequestsError,)
        # User-Agent, Accept, sec-ch-ua и т.п. берутся от имитируемого браузера: свой UA
        # разошёлся бы с TLS-отпечатком. Добавляем только язык.
        self._session = curl_requests.Session(
            impersonate=impersonate,
            headers={"Accept-Language": _ACCEPT_LANGUAGE},
            timeout=settings.timeout_seconds,
            allow_redirects=True,
        )

    @property
    def cookies(self) -> Any:
        return self._session.cookies

    def request(self, method: str, url: str, **kwargs) -> Response:
        return self._session.request(method, url, **kwargs)

    def close(self) -> None:
        self._session.close()


def _checked_impersonate(value: str) -> str:
    """Неизвестный curl_cffi браузер падал бы на каждом запросе — заменяем на chrome сразу."""
    try:
        from curl_cffi.requests.impersonate import BrowserType, resolve_latest_browser_type
    except ImportError:  # другая версия curl_cffi — пусть проверяет сама
        return value
    supported = {browser.value for browser in BrowserType}
    if resolve_latest_browser_type(value) in supported:
        return value
    log.warning(
        "http.impersonate=%r не поддерживается установленной версией curl_cffi — использую 'chrome'. "
        "Подходят: chrome, edge, firefox, safari, chrome_android, safari_ios или конкретная версия: %s",
        value, ", ".join(sorted(supported)),
    )
    return "chrome"


def _make_backend(settings: HttpSettings, transport: httpx.BaseTransport | None):
    if transport is not None or settings.backend == "httpx":
        return _HttpxBackend(settings, transport)
    try:
        return _CurlCffiBackend(settings)
    except ImportError:
        log.warning(
            "curl_cffi не установлен — работаю через httpx. work.ua может отвечать 403: он отличает "
            "Python-клиент по TLS-отпечатку. Установите: pip install curl_cffi"
        )
        return _HttpxBackend(settings)


class HttpClient:
    def __init__(
        self,
        settings: HttpSettings,
        *,
        transport: httpx.BaseTransport | None = None,  # подменный транспорт для тестов, всегда с httpx
        sleep: Callable[[float], None] = time.sleep,
        save_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._last_request = 0.0
        self._save_dir = save_dir
        self._saved = 0
        self.requests_made = 0
        self._backend = _make_backend(settings, transport)
        log.info("HTTP-клиент: %s", self._backend.description)

    @property
    def backend(self) -> str:
        return self._backend.name

    @property
    def cookies(self) -> Any:
        """Cookies сессии; у обоих бэкендов есть .get(name)."""
        return self._backend.cookies

    def get(self, url: str, **kwargs) -> Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Response:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> Response:
        attempts = 2
        for attempt in range(1, attempts + 1):
            self._pause()
            try:
                response = self._backend.request(method, url, **kwargs)
            except self._backend.errors as exc:
                if attempt < attempts:
                    log.debug("%s %s: %s — повторяю", method, url, exc)
                    continue
                raise FetchError(f"{url}: {type(exc).__name__}: {exc}") from exc
            finally:
                self._last_request = time.monotonic()
                self.requests_made += 1

            if response.status_code in _RETRY_STATUSES and attempt < attempts:
                wait = _retry_after(response)
                log.debug("%s %s: HTTP %s — повтор через %.0f с", method, url, response.status_code, wait)
                self._sleep(wait)
                continue
            if response.status_code >= 400:
                raise FetchError(f"{url}: HTTP {response.status_code}")
            self._maybe_save(url, response)
            return response
        raise AssertionError("unreachable")

    def _pause(self) -> None:
        if not self._last_request:
            return
        delay = self.settings.delay_seconds * (1 + random.uniform(0, 0.5))
        remaining = delay - (time.monotonic() - self._last_request)
        if remaining > 0:
            self._sleep(remaining)

    def _maybe_save(self, url: str, response: Response) -> None:
        if self._save_dir is None:
            return
        self._save_dir.mkdir(parents=True, exist_ok=True)
        self._saved += 1
        ext = "json" if "json" in (response.headers.get("content-type") or "") else "html"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1]).strip("_")[:120]
        path = self._save_dir / f"{self._saved:03d}_{slug}.{ext}"
        path.write_text(response.text, encoding="utf-8")
        log.debug("сохранено: %s", path)

    def close(self) -> None:
        self._backend.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _retry_after(response: Response) -> float:
    try:
        return min(float(response.headers.get("retry-after") or "5"), _MAX_RETRY_AFTER)
    except ValueError:
        return 5.0
