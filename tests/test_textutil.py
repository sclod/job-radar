from datetime import date

import pytest
from selectolax.lexbor import LexborHTMLParser

from jobradar.textutil import canonical_url, html_to_text, matches_any, mentions_deferment, parse_date

TODAY = date(2026, 1, 5)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3 жовтня 2025", date(2025, 10, 3)),
        ("Python, вакансія від 1 лютого 2026", date(2026, 2, 1)),
        ("2 января 2026", date(2026, 1, 2)),
        ("3 січня", date(2026, 1, 3)),
        ("28 грудня", date(2025, 12, 28)),  # без года и «в будущем» — значит прошлый год
        ("2025-10-03 12:00:00", date(2025, 10, 3)),
        ("12.09.2025", date(2025, 9, 12)),
        ("сьогодні", TODAY),
        ("вчора", date(2026, 1, 4)),
        ("5 днів тому", date(2025, 12, 31)),
        ("3 години тому", TODAY),
        ("2 тижні тому", date(2025, 12, 22)),
        ("", None),
        ("гаряча вакансія", None),
        ("31 лютого 2026", None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Надаємо бронювання працівників", True),
        ("Офіційне працевлаштування, бронь", True),
        ("Бронюємо всіх співробітників", True),
        ("Предоставляем бронирование", True),
        ("We offer military deferment for employees", True),
        ("Python, Django, PostgreSQL", False),
        (None, False),
    ],
)
def test_mentions_deferment(text, expected):
    assert mentions_deferment(text) is expected


def test_matches_any_word_start():
    assert matches_any("Senior Python Developer", ["senior"])
    assert matches_any("Python/Django developer", ["django"])
    assert matches_any("Шукаємо стажиста", ["стажист"])
    assert not matches_any("Misleading title", ["lead"])
    assert not matches_any("Python Developer", ["", "  "])


def test_canonical_url():
    assert canonical_url("https://Jobs.DOU.ua/companies/x/vacancies/1?from=list_hot#top") == (
        "https://jobs.dou.ua/companies/x/vacancies/1/"
    )


def test_html_to_text_keeps_structure():
    tree = LexborHTMLParser(
        "<div id=d><h3>Вимоги</h3><p>Python <b>3.11</b></p><style>p{}</style>"
        "<ul><li>SQL</li><li><p>Git</p></li></ul>Хвіст<br>кінець</div>"
    )
    assert html_to_text(tree.css_first("#d")) == "Вимоги\nPython 3.11\n• SQL\n• Git\nХвіст\nкінець"
