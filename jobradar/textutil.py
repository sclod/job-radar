"""Утилиты для текста: HTML→текст, даты, ссылки, бронирование."""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborNode

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dd", "dt",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


def clean(text: str | None) -> str:
    """Схлопывает пробелы (включая неразрывные) в один."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def node_text(node: LexborNode | None) -> str:
    return clean(node.text(deep=True)) if node is not None else ""


def html_to_text(node: LexborNode | None) -> str:
    """Текст узла с переносами строк между блоками и маркерами «•» для пунктов списков.

    Удаляет из дерева script/style и т.п. — вызывать на свежеразобранной странице.
    """
    if node is None:
        return ""
    for junk in node.css(", ".join(sorted(_SKIP_TAGS))):
        junk.decompose()
    parts: list[str] = []
    prev_block: int | None = None
    bulleted: set[int] = set()
    for child in node.traverse(include_text=True):
        if child.tag == "br":
            parts.append("\n")
            continue
        if child.tag != "-text":
            continue
        block, item = _enclosing_block(child, node)
        if block != prev_block:
            prev_block = block
            parts.append("\n")
            if item is not None and item not in bulleted:
                bulleted.add(item)
                parts.append("• ")
        parts.append(child.text_content or "")
    lines = (clean(line) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line and line != "•").strip()


def _enclosing_block(node: LexborNode, root: LexborNode) -> tuple[int | None, int | None]:
    """mem_id ближайшего блочного предка и ближайшего <li> (если есть)."""
    block = item = None
    parent = node.parent
    while parent is not None and parent.mem_id != root.mem_id:
        if block is None and parent.tag in _BLOCK_TAGS:
            block = parent.mem_id
        if parent.tag == "li":
            item = parent.mem_id
            break
        parent = parent.parent
    return block if block is not None else root.mem_id, item


# --- даты --------------------------------------------------------------------

_MONTHS = {
    # укр. (родительный падеж)
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
    # рус.
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    # англ.
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+([^\W\d_]+)(?:,?\s+(\d{4}))?", re.UNICODE)
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DOTTED_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_AGO_RE = re.compile(
    r"(\d+)\s*(хв|мин|min|год|час|hour|дн|день|дні|day|тиж|нед|week|міс|мес|month)",
    re.IGNORECASE,
)


def parse_date(text: str | None, today: date | None = None) -> date | None:
    """Разбирает «3 жовтня 2024», «3 жовтня», «2024-10-03», «вчора», «5 днів тому»."""
    if not text:
        return None
    today = today or date.today()
    low = clean(text).lower()

    if m := _ISO_RE.search(low):
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    if m := _DOTTED_RE.search(low):
        return _safe_date(int(m[3]), int(m[2]), int(m[1]))
    for m in _DATE_RE.finditer(low):
        month = _MONTHS.get(m[2])
        if not month:
            continue
        year = int(m[3]) if m[3] else today.year
        result = _safe_date(year, month, int(m[1]))
        # «28 грудня» без года в январе — это прошлый год
        if result and not m[3] and result > today + timedelta(days=1):
            result = _safe_date(year - 1, month, int(m[1]))
        return result

    if any(word in low for word in ("сьогодні", "сегодня", "today", "щойно", "только что")):
        return today
    if any(word in low for word in ("вчора", "вчера", "yesterday")):
        return today - timedelta(days=1)
    if m := _AGO_RE.search(low):
        amount, unit = int(m[1]), m[2].lower()
        if unit.startswith(("хв", "мин", "min", "год", "час", "hour")):
            return today
        if unit.startswith(("тиж", "нед", "week")):
            return today - timedelta(weeks=amount)
        if unit.startswith(("міс", "мес", "month")):
            return today - timedelta(days=30 * amount)
        return today - timedelta(days=amount)
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --- ссылки ------------------------------------------------------------------

def absolute_url(href: str, base: str) -> str:
    return urljoin(base, href.strip())


def canonical_url(url: str) -> str:
    """Ссылка на вакансию без query/fragment — по ней идёт дедупликация."""
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), path, "", ""))


# --- бронирование ------------------------------------------------------------

_DEFERMENT_RE = re.compile(
    r"бронюван|бронь|бронюєм|бронюю|відстрочк|бронирован|отсрочк"
    r"|military\s+(?:deferment|reservation|exemption)|deferment|reservation\s+from\s+(?:mobili[sz]ation|the\s+army|military)",
    re.IGNORECASE,
)
_SALARY_RE = re.compile(r"\d[\d\s  \xa0]*\s*(?:грн|uah|\$|usd|€|eur)|[$€]\s*\d", re.IGNORECASE)


def mentions_deferment(text: str | None) -> bool:
    return bool(text and _DEFERMENT_RE.search(text))


def looks_like_salary(text: str | None) -> bool:
    return bool(text and _SALARY_RE.search(text))


def matches_any(text: str, words: list[str]) -> bool:
    """Есть ли в тексте слово, начинающееся с одного из `words` (без учёта регистра).

    Сравнение по началу слова: «стажист» найдёт и «стажиста», а «lead» не найдётся в «pleader».
    """
    return any(
        re.search(r"(?<!\w)" + re.escape(word.strip()), text, re.IGNORECASE)
        for word in words
        if word.strip()
    )
