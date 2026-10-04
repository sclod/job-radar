"""work.ua — главный источник: фильтр ?deferment=1 оставляет вакансии с бронированием."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from ..config import QueryConfig
from ..models import DEFERMENT_YES, Vacancy
from ..textutil import (
    absolute_url,
    canonical_url,
    clean,
    html_to_text,
    looks_like_salary,
    node_text,
    parse_date,
)
from .base import Source, register

_JOB_HREF = re.compile(r"/(?:ru/|en/)?jobs/\d+/?")
_PUBLISHED = re.compile(r"(?:вакансія|вакансия|vacancy)\s+(?:від|от|from)\s+(.+)$", re.IGNORECASE)
_STRONG = "span.strong-600, span.strong-500, b, strong"
_CITY_NOISE = re.compile(r"\d+([.,]\d+)?\s*км.*$|показати на карті|показать на карте", re.IGNORECASE)


@register
class WorkUaSource(Source):
    name = "workua"
    title = "work.ua"

    def query_deferment(self, query: QueryConfig) -> str | None:
        if query.deferment is not None:
            return DEFERMENT_YES if query.deferment else None
        params = parse_qs(urlsplit(query.url).query)
        return DEFERMENT_YES if params.get("deferment") == ["1"] else None

    # --- список ----------------------------------------------------------------

    def parse_list(self, html: str, page_url: str) -> list[Vacancy]:
        tree = LexborHTMLParser(html)
        cards = tree.css("div.job-link") or _cards_by_title_link(tree)
        vacancies = []
        for card in cards:
            vacancy = self._parse_card(card, page_url)
            if vacancy:
                vacancies.append(vacancy)
        return vacancies

    def _parse_card(self, card: LexborNode, page_url: str) -> Vacancy | None:
        link = _title_link(card)
        if link is None:
            return None
        title = node_text(link)
        url = canonical_url(absolute_url(link.attributes.get("href") or "", page_url))

        salary = company = city = None
        for el in card.css(_STRONG):
            if _has_ancestor(el, card, lambda n: n.tag in {"h2", "p"} or "label" in _cls(n)):
                continue
            text = node_text(el)
            if not text or text == title:
                continue
            if salary is None and looks_like_salary(text):
                salary = text
            elif company is None and not looks_like_salary(text):
                company = text
                city = _city_from_row(_row_of(el, card), text)
            if salary and company:
                break

        published = None
        if m := _PUBLISHED.search(clean(link.attributes.get("title"))):
            published = parse_date(m[1], self.today)
        if published is None and (time_el := card.css_first("time[datetime]")):
            published = parse_date(time_el.attributes.get("datetime"), self.today)

        snippet_el = card.css_first("p")
        return Vacancy(
            source=self.name,
            url=url,
            title=title,
            company=company,
            city=city,
            salary=salary,
            published_at=published,
            snippet=node_text(snippet_el) or None,
        )

    def next_page_url(self, html: str, page_url: str, page_no: int) -> str | None:
        tree = LexborHTMLParser(html)
        for a in tree.css("ul.pagination a[href], nav a[href]"):
            href = a.attributes.get("href") or ""
            m = re.search(r"[?&]page=(\d+)", href)
            if m and int(m[1]) == page_no + 1:
                return absolute_url(href, page_url)
        return None

    # --- страница вакансии -------------------------------------------------------

    def parse_detail(self, html: str, vacancy: Vacancy) -> Vacancy:
        tree = LexborHTMLParser(html)

        if not vacancy.title and (h1 := tree.css_first("h1#h1-name") or tree.css_first("h1")):
            vacancy.title = node_text(h1)

        conditions = []
        for icon in tree.css("li span[title]"):
            label = clean(icon.attributes.get("title"))
            kind = label.lower()
            li = icon.parent
            while li is not None and li.tag != "li":
                li = li.parent
            if li is None:
                continue
            text = node_text(li)
            if "зарплат" in kind:
                vacancy.salary = vacancy.salary or _first_strong(li) or text
            elif "компан" in kind:
                vacancy.company = vacancy.company or _first_strong(li) or text
            elif "адрес" in kind or "місце роботи" in kind or "место работы" in kind:
                place = clean(_CITY_NOISE.sub("", text))
                if place:
                    vacancy.city = vacancy.city or place.split(",")[0].strip()
                    conditions.append(f"{label}: {place}")
            elif "умови" in kind or "условия" in kind or "вимоги" in kind:
                conditions.append(f"{label}: {text}")

        if vacancy.published_at is None:
            if m := re.search(r"(?:Вакансія|Вакансия)\s+(?:від|от)\s+(\d{1,2}\s+\S+\s+\d{4})", tree.body.text() if tree.body else ""):
                vacancy.published_at = parse_date(m[1], self.today)
            elif time_el := tree.css_first("time[datetime]"):
                vacancy.published_at = parse_date(time_el.attributes.get("datetime"), self.today)

        body = html_to_text(tree.css_first("#job-description"))
        parts = [*conditions, body] if body else []
        vacancy.description = "\n\n".join(parts) or None
        return vacancy


def _cards_by_title_link(tree: LexborHTMLParser) -> list[LexborNode]:
    """Запасной вариант, если класс job-link переименуют: карточка = блок вокруг h2 со ссылкой."""
    cards = []
    for h2 in tree.css("h2"):
        a = h2.css_first("a[href]")
        if a is None or not _JOB_HREF.search(a.attributes.get("href") or ""):
            continue
        card = h2.parent
        if card is not None:
            cards.append(card)
    return cards


def _title_link(card: LexborNode) -> LexborNode | None:
    for a in card.css("h2 a[href], a[href]"):
        if _JOB_HREF.search(a.attributes.get("href") or "") and node_text(a):
            return a
    return None


def _cls(node: LexborNode) -> str:
    return node.attributes.get("class") or ""


def _has_ancestor(node: LexborNode, stop: LexborNode, predicate) -> bool:
    parent = node.parent
    while parent is not None and parent.mem_id != stop.mem_id:
        if predicate(parent):
            return True
        parent = parent.parent
    return False


def _row_of(node: LexborNode, card: LexborNode) -> LexborNode:
    """Ближайший div-предок внутри карточки — строка «компания · город»."""
    parent = node.parent
    while parent is not None and parent.mem_id != card.mem_id:
        if parent.tag == "div":
            return parent
        parent = parent.parent
    return node


def _city_from_row(row: LexborNode, company: str) -> str | None:
    rest = node_text(row).replace(company, "", 1)
    rest = _CITY_NOISE.sub("", rest)
    rest = rest.strip(" ·•|,.-–—")
    return rest or None


def _first_strong(node: LexborNode) -> str | None:
    el = node.css_first(_STRONG) or node.css_first("a")
    return node_text(el) or None
