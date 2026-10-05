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
_SEPARATORS = re.compile(r"\s*[·•|]\s*")
# зарплата без суммы
_SALARY_PHRASE = re.compile(
    r"за результатами співбесіди|за домовленістю|договірна"
    r"|по результатам собеседования|по договоренности|договорная",
    re.IGNORECASE,
)
# сумма начинается с цифры, валюты или «від/до»: «75 000 грн», «від 18 000 грн», «$4000 – 5000»
_SALARY_START = re.compile(r"^(?:від|до|от|from|up to)?\s*[$€]?\s*\d", re.IGNORECASE)
# уточнения к зарплате, которые на карточке стоят рядом с суммой (сравнивается весь текст
# элемента, чтобы не отбросить компанию вроде «KPI Solutions»)
_SALARY_NOTE = re.compile(
    r"після\s+(?:всіх\s+)?(?:відрахувань|вирахувань|сплати\s+податків)"
    r"|до\s+(?:відрахування|вирахування|сплати)\s+податків|на\s+руки|(?:є\s+)?система\s+kpi"
    r"|после\s+(?:всех\s+)?вычетов|до\s+вычета\s+налогов|(?:есть\s+)?система\s+kpi",
    re.IGNORECASE,
)


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

        # Поля определяются по смыслу, а не по порядку: строка зарплаты (сумма + уточнения
        # вроде «Після всіх відрахувань») целиком уходит в salary, компания — первый жирный
        # элемент вне этой строки, город — остаток строки компании.
        salary_row, salary = _salary_row(card)
        company = city = None
        for el in card.css(_STRONG):
            if _has_ancestor(el, card, _not_info):
                continue
            if salary_row is not None and _inside(el, salary_row):
                continue
            text = node_text(el)
            if not text or text == title:
                continue
            if _is_salary(text) or _is_salary_note(text):
                salary = salary or (text if _is_salary(text) else None)
                continue
            company = text
            city = _city_from_row(_row_of(el, card), text)
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


def _not_info(node: LexborNode) -> bool:
    """Заголовок, краткое описание и метки («Гаряча») — не строки с данными вакансии."""
    return node.tag in {"h2", "p"} or "label" in _cls(node)


def _inside(node: LexborNode, ancestor: LexborNode) -> bool:
    while node is not None:
        if node.mem_id == ancestor.mem_id:
            return True
        node = node.parent
    return False


def _is_salary(text: str) -> bool:
    """Сумма («75 000 грн», «від 18 000 грн», «$4000 – 5000») или «За результатами співбесіди»."""
    text = clean(text)
    return bool(_SALARY_PHRASE.search(text) or (looks_like_salary(text) and _SALARY_START.match(text)))


def _is_salary_note(text: str) -> bool:
    """«Після всіх відрахувань», «є система KPI» и т.п. — уточнение к зарплате целиком."""
    return bool(_SALARY_NOTE.fullmatch(clean(text).strip(" .,;")))


def _salary_row(card: LexborNode) -> tuple[LexborNode | None, str | None]:
    """Строка карточки, которая начинается с зарплаты, и её текст вместе с уточнениями:
    «100 000 – 120 000 грн · Після всіх відрахувань · є система KPI»."""
    for row in card.css("div"):
        # нужны только «листовые» строки, не обёртки (css() включает и сам узел)
        if any(inner.mem_id != row.mem_id for inner in row.css("div")) or _has_ancestor(row, card, _not_info):
            continue
        parts = [part for part in _SEPARATORS.split(node_text(row)) if part]
        if parts and _is_salary(parts[0]):
            return row, " · ".join(parts)
    return None, None


def _city_from_row(row: LexborNode, company: str) -> str | None:
    rest = _CITY_NOISE.sub("", node_text(row).replace(company, "", 1))
    parts = [part.strip(" ,.-–—") for part in _SEPARATORS.split(rest)]
    # зарплата и уточнения к ней в город не попадают, даже если стоят в той же строке
    parts = [part for part in parts if part and not _is_salary(part) and not _is_salary_note(part)]
    return " · ".join(parts) or None


def _first_strong(node: LexborNode) -> str | None:
    el = node.css_first(_STRONG) or node.css_first("a")
    return node_text(el) or None
