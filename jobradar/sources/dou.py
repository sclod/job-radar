"""jobs.dou.ua — список вакансий + подгрузка «Більше вакансій» через XHR."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from ..config import QueryConfig
from ..http import FetchError
from ..models import Vacancy
from ..textutil import absolute_url, canonical_url, html_to_text, node_text, parse_date
from .base import Source, register

log = logging.getLogger(__name__)

_CSRF_RE = re.compile(r"CSRF_TOKEN\s*=\s*[\"']([^\"']+)")


@register
class DouSource(Source):
    name = "dou"
    title = "DOU"

    def iter_list_pages(self, query: QueryConfig) -> Iterator[list[Vacancy]]:
        """Первая страница — обычный GET, остальные — POST на /vacancies/xhr-load/,
        как делает кнопка «Більше вакансій» на сайте."""
        assert self.http is not None
        first = self.http.get(query.url)
        items = self.parse_list(first.text, query.url)
        yield items
        loaded = len(items)

        csrf = self.http.cookies.get("csrftoken")
        if not csrf and (m := _CSRF_RE.search(first.text)):
            csrf = m[1]
        for _ in range(self.settings.max_pages - 1):
            if not loaded or not csrf:
                if not csrf:
                    log.debug("DOU: нет csrftoken — читаю только первую страницу")
                return
            response = self.http.post(
                xhr_url(query.url),
                data={"csrfmiddlewaretoken": csrf, "count": str(loaded)},
                headers={"Referer": query.url, "X-Requested-With": "XMLHttpRequest"},
            )
            html, last = parse_xhr(response.text)
            more = self.parse_list(html, query.url)
            yield more
            loaded += len(more)
            if last or not more:
                return

    # --- список ----------------------------------------------------------------

    def parse_list(self, html: str, page_url: str) -> list[Vacancy]:
        tree = LexborHTMLParser(html)
        vacancies = []
        for item in tree.css("li.l-vacancy"):
            link = item.css_first("a.vt")
            if link is None:
                continue
            href = link.attributes.get("href") or ""
            vacancies.append(
                Vacancy(
                    source=self.name,
                    url=canonical_url(absolute_url(href, page_url)),
                    title=node_text(link),
                    company=node_text(item.css_first("a.company")) or None,
                    city=node_text(item.css_first(".cities")) or None,
                    salary=node_text(item.css_first(".salary")) or None,
                    published_at=parse_date(node_text(item.css_first(".date")), self.today),
                    snippet=node_text(item.css_first(".sh-info")) or None,
                )
            )
        return vacancies

    # --- страница вакансии -------------------------------------------------------

    def parse_detail(self, html: str, vacancy: Vacancy) -> Vacancy:
        tree = LexborHTMLParser(html)
        box = tree.css_first(".b-vacancy") or tree.body
        if box is None:
            return vacancy

        vacancy.company = vacancy.company or node_text(box.css_first(".b-compinfo .l-n a")) or None
        vacancy.city = vacancy.city or node_text(box.css_first(".sh-info .place")) or None
        vacancy.salary = vacancy.salary or node_text(box.css_first(".sh-info .salary")) or None
        if vacancy.published_at is None:
            vacancy.published_at = parse_date(node_text(box.css_first(".date")), self.today)

        sections = [html_to_text(section) for section in box.css(".vacancy-section")]
        text = "\n\n".join(part for part in sections if part)
        vacancy.description = text or None
        return vacancy


def xhr_url(list_url: str) -> str:
    """https://jobs.dou.ua/vacancies/?category=Python → …/vacancies/xhr-load/?category=Python"""
    parts = urlsplit(list_url)
    path = parts.path.rstrip("/") + "/xhr-load/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def parse_xhr(payload: str) -> tuple[str, bool]:
    """Ответ XHR: {"html": "<li class=l-vacancy>…", "last": bool, "num": int}."""
    try:
        data = json.loads(payload)
        return str(data["html"]), bool(data.get("last", False))
    except (ValueError, KeyError, TypeError) as exc:
        raise FetchError(f"DOU: неожиданный ответ xhr-load: {exc}") from exc
