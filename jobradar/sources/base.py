"""Общий интерфейс источника вакансий.

Чтобы добавить источник: создайте модуль в jobradar/sources/, унаследуйтесь от Source,
пометьте класс @register и импортируйте модуль в jobradar/sources/__init__.py.
Ключ `name` — это имя секции в config.yaml.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import date
from typing import ClassVar

from ..config import HttpSettings, QueryConfig
from ..http import HttpClient
from ..models import DEFERMENT_YES, Vacancy

SOURCES: dict[str, type["Source"]] = {}


def register(cls: type["Source"]) -> type["Source"]:
    SOURCES[cls.name] = cls
    return cls


class Source(ABC):
    name: ClassVar[str]  # ключ в config.yaml
    title: ClassVar[str]  # человекочитаемое имя для логов и отчёта
    implemented: ClassVar[bool] = True

    def __init__(
        self,
        http: HttpClient | None,
        settings: HttpSettings | None = None,
        *,
        today: date | None = None,
    ) -> None:
        self.http = http
        self.settings = settings or HttpSettings()
        self.today = today

    # --- сетевая часть ---------------------------------------------------------

    def iter_list_pages(self, query: QueryConfig) -> Iterator[list[Vacancy]]:
        """Отдаёт вакансии постранично, не больше settings.max_pages страниц.

        FetchError с первой страницы пробрасывается; вызывающий код логирует и идёт дальше.
        """
        assert self.http is not None
        url: str | None = query.url
        page_no = 0
        while url and page_no < self.settings.max_pages:
            page_no += 1
            html = self.http.get(url).text
            yield self.parse_list(html, url)
            url = self.next_page_url(html, url, page_no)

    def fetch_details(self, vacancy: Vacancy) -> Vacancy:
        assert self.http is not None
        html = self.http.get(vacancy.url).text
        return self.parse_detail(html, vacancy)

    def query_deferment(self, query: QueryConfig) -> str | None:
        """Признак бронирования, который гарантирует сам запрос (фильтр на сайте)."""
        return DEFERMENT_YES if query.deferment else None

    # --- разбор HTML (чистые функции, покрыты тестами на фикстурах) --------------

    @abstractmethod
    def parse_list(self, html: str, page_url: str) -> list[Vacancy]:
        """Вакансии со страницы списка."""

    def next_page_url(self, html: str, page_url: str, page_no: int) -> str | None:
        """Ссылка на следующую страницу списка или None."""
        return None

    @abstractmethod
    def parse_detail(self, html: str, vacancy: Vacancy) -> Vacancy:
        """Дополняет вакансию данными со страницы вакансии (полный текст и пропущенные поля)."""
