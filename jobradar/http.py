"""Вежливый HTTP-клиент: браузерный User-Agent, пауза между запросами, один повтор."""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from .config import HttpSettings

log = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 30.0


class FetchError(Exception):
    """Источник ответил ошибкой или не ответил вовсе."""


class HttpClient:
    def __init__(
        self,
        settings: HttpSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        save_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._last_request = 0.0
        self._save_dir = save_dir
        self._saved = 0
        self.requests_made = 0
        self._client = httpx.Client(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
            },
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempts = 2
        for attempt in range(1, attempts + 1):
            self._pause()
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
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

    def _maybe_save(self, url: str, response: httpx.Response) -> None:
        if self._save_dir is None:
            return
        self._save_dir.mkdir(parents=True, exist_ok=True)
        self._saved += 1
        ext = "json" if "json" in response.headers.get("content-type", "") else "html"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1]).strip("_")[:120]
        path = self._save_dir / f"{self._saved:03d}_{slug}.{ext}"
        path.write_text(response.text, encoding="utf-8")
        log.debug("сохранено: %s", path)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(float(response.headers.get("retry-after", "5")), _MAX_RETRY_AFTER)
    except ValueError:
        return 5.0
