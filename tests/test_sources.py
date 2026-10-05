"""Разбор сохранённых HTML-страниц work.ua и DOU."""

from datetime import date

import pytest
from conftest import TODAY, fixture_text, make_vacancy

from jobradar.config import QueryConfig
from jobradar.http import FetchError
from jobradar.models import DEFERMENT_YES
from jobradar.sources import SOURCES
from jobradar.sources.dou import DouSource, parse_xhr, xhr_url
from jobradar.sources.robotaua import RobotaUaSource
from jobradar.sources.workua import WorkUaSource

WORKUA_LIST = "https://www.work.ua/jobs-python/?deferment=1"
DOU_LIST = "https://jobs.dou.ua/vacancies/?category=Python&exp=0-1"


@pytest.fixture
def workua():
    return WorkUaSource(http=None, today=TODAY)


@pytest.fixture
def dou():
    return DouSource(http=None, today=TODAY)


def test_registry_has_all_sources():
    assert set(SOURCES) == {"workua", "dou", "robotaua"}
    assert SOURCES["robotaua"].implemented is False


# --- work.ua -----------------------------------------------------------------

def test_workua_list_parses_cards(workua):
    vacancies = workua.parse_list(fixture_text("workua_list.html"), WORKUA_LIST)

    # рекламный блок без ссылки на /jobs/<id>/ не считается вакансией
    assert [v.url for v in vacancies] == [
        "https://www.work.ua/jobs/5900001/",
        "https://www.work.ua/jobs/5900002/",
        "https://www.work.ua/jobs/5900003/",
        "https://www.work.ua/jobs/5900004/",
    ]
    first = vacancies[0]
    assert first.source == "workua"
    assert first.title == "Junior Python розробник"
    assert first.company == "ТОВ «Альфа Софт»"
    assert first.city == "Київ"
    assert first.salary == "35 000 – 45 000 грн"
    assert first.published_at == date(2026, 10, 2)
    assert first.snippet.startswith("Шукаємо Junior Python")


def test_workua_list_handles_missing_salary_and_distance(workua):
    vacancies = {v.url: v for v in workua.parse_list(fixture_text("workua_list.html"), WORKUA_LIST)}

    remote = vacancies["https://www.work.ua/jobs/5900002/"]
    assert remote.salary is None
    assert remote.company == "Beta Systems"
    assert remote.city == "Дистанційно"

    lviv = vacancies["https://www.work.ua/jobs/5900003/"]
    assert lviv.salary == "$4000 – 5000"
    assert lviv.city == "Львів"  # «· 2,5 км від вас» отброшено


def test_workua_legacy_markup(workua):
    vacancies = workua.parse_list(fixture_text("workua_list_legacy.html"), "https://www.work.ua/jobs-python/")

    assert len(vacancies) == 2
    assert vacancies[0].company == "Zeta Labs"
    assert vacancies[0].salary == "20 000 – 30 000 грн"
    assert vacancies[0].city == "Одеса"
    assert vacancies[1].salary is None
    assert vacancies[1].company == "Eta Group"
    assert vacancies[1].published_at == date(2026, 1, 30)


def test_workua_cards_with_salary(workua):
    """Баг: уточнение к зарплате принималось за компанию, а строка зарплаты — за город."""
    cards = {v.url: v for v in workua.parse_list(fixture_text("workua_cards_with_salary.html"), WORKUA_LIST)}

    net = cards["https://www.work.ua/jobs/8524609/"]
    assert (net.company, net.city) == ("Vyriy Industries", "Київ")
    assert net.salary == "75 000 грн · Після всіх відрахувань"

    kpi = cards["https://www.work.ua/jobs/8555601/"]
    assert (kpi.company, kpi.city) == ("Placeholder Company", "Львів")
    assert kpi.salary == "100 000 – 120 000 грн · Після всіх відрахувань · є система KPI"

    by_interview = cards["https://www.work.ua/jobs/5900010/"]
    assert (by_interview.company, by_interview.city) == ("Omega Soft", "Дистанційно")
    assert by_interview.salary == "За результатами співбесіди"

    for vacancy in cards.values():
        for field in (vacancy.company, vacancy.city):
            assert "грн" not in field
            assert "відрахувань" not in field
            assert "KPI" not in field
            assert "співбесіди" not in field


def test_workua_cards_without_salary(workua):
    cards = workua.parse_list(fixture_text("workua_cards_without_salary.html"), WORKUA_LIST)

    assert [(v.url, v.company, v.city, v.salary) for v in cards] == [
        ("https://www.work.ua/jobs/8381297/", "Placeholder Systems", "Одеса", None),
        ("https://www.work.ua/jobs/6502018/", "ТОВ «Заглушка»", "Київ", None),  # «· 3 км від вас» отброшено
    ]


@pytest.mark.parametrize(
    ("salary_html", "expected_salary"),
    [
        # сумма обычным текстом, уточнение жирным
        ('<div><span>75 000 грн</span> · <b>Після всіх відрахувань</b></div>', "75 000 грн · Після всіх відрахувань"),
        # уточнение отдельной строкой под суммой
        (
            '<div><span class="strong-600">75 000 грн</span></div><div><span class="strong-500">Після всіх відрахувань</span></div>',
            "75 000 грн",
        ),
        # сумма вне div-строки
        ('<span class="strong-600">від 40 000 грн</span>', "від 40 000 грн"),
    ],
)
def test_workua_salary_markup_variants(workua, salary_html, expected_salary):
    html = f"""
    <div class="card job-link">
      <h2><a href="/jobs/8524609/" title="Python Developer, вакансія від 2 жовтня 2026">Python Developer</a></h2>
      {salary_html}
      <div><span class="mr-xs"><span class="strong-600">Vyriy Industries</span></span> <span>Київ</span></div>
    </div>"""
    (vacancy,) = workua.parse_list(html, WORKUA_LIST)
    assert (vacancy.company, vacancy.city, vacancy.salary) == ("Vyriy Industries", "Київ", expected_salary)


def test_workua_pagination(workua):
    page1 = fixture_text("workua_list.html")
    assert workua.next_page_url(page1, WORKUA_LIST, 1) == "https://www.work.ua/jobs-python/?deferment=1&page=2"
    assert workua.next_page_url(fixture_text("workua_list_page2.html"), WORKUA_LIST + "&page=2", 2) is None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (QueryConfig(url="https://www.work.ua/jobs-python/?deferment=1"), DEFERMENT_YES),
        (QueryConfig(url="https://www.work.ua/jobs-python/?page=2&deferment=1"), DEFERMENT_YES),
        (QueryConfig(url="https://www.work.ua/jobs-python/"), None),
        (QueryConfig(url="https://www.work.ua/jobs-python/?deferment=1", deferment=False), None),
        (QueryConfig(url="https://www.work.ua/jobs-python/", deferment=True), DEFERMENT_YES),
    ],
)
def test_workua_deferment_from_query(workua, query, expected):
    assert workua.query_deferment(query) == expected


def test_workua_detail_fills_text_and_keeps_list_fields(workua):
    vacancy = make_vacancy(url="https://www.work.ua/jobs/5900001/", city="Київ", salary=None, company=None, description=None)
    workua.parse_detail(fixture_text("workua_vacancy.html"), vacancy)

    assert vacancy.salary == "35 000 – 45 000 грн"
    assert vacancy.company == "ТОВ «Альфа Софт»"
    assert vacancy.city == "Київ"
    text = vacancy.description
    assert "Умови й вимоги: Повна зайнятість" in text
    assert "• писати тести на pytest;" in text
    assert "бронювання працівників від мобілізації" in text
    assert "trackView" not in text  # содержимое <script> выброшено


def test_workua_detail_without_description(workua):
    vacancy = make_vacancy(description=None)
    workua.parse_detail(fixture_text("workua_vacancy_no_text.html"), vacancy)
    assert vacancy.description is None


# --- DOU ---------------------------------------------------------------------

def test_dou_list_parses_items(dou):
    vacancies = dou.parse_list(fixture_text("dou_list.html"), DOU_LIST)

    assert len(vacancies) == 3
    hot = vacancies[0]
    # ?from=list_hot отрезан — иначе одна вакансия считалась бы разными
    assert hot.url == "https://jobs.dou.ua/companies/theta-soft/vacancies/330001/"
    assert hot.title == "Junior Python Developer"
    assert hot.company == "Theta Soft"
    assert hot.city == "Київ, віддалено"
    assert hot.salary == "$800–1200"
    assert hot.published_at == date(2026, 10, 3)  # «3 жовтня» без года
    assert vacancies[1].salary is None
    assert "бронювання" in vacancies[1].snippet


def test_dou_xhr_payload(dou):
    html, last = parse_xhr(fixture_text("dou_xhr.json"))
    more = dou.parse_list(html, DOU_LIST)

    assert last is True
    assert [v.title for v in more] == ["Junior Data Engineer (Python)", "Junior Python Developer"]
    assert more[0].published_at == date(2026, 9, 25)


def test_dou_xhr_bad_payload():
    with pytest.raises(FetchError):
        parse_xhr("<html>Too many requests</html>")


def test_dou_xhr_url():
    assert xhr_url(DOU_LIST) == "https://jobs.dou.ua/vacancies/xhr-load/?category=Python&exp=0-1"


def test_dou_detail(dou):
    vacancy = make_vacancy(source="dou", company=None, city=None, salary=None, published_at=None, description=None)
    dou.parse_detail(fixture_text("dou_vacancy.html"), vacancy)

    assert vacancy.company == "Theta Soft"
    assert vacancy.city == "Київ, віддалено"
    assert vacancy.salary == "$800–1200"
    assert vacancy.published_at == date(2026, 10, 3)
    assert vacancy.description.startswith("Необхідні навички\n• Python 3")
    assert "Можливе бронювання для співробітників." in vacancy.description
    assert "Відгукнутися" not in vacancy.description


# --- robota.ua ---------------------------------------------------------------

def test_robotaua_is_stub():
    with pytest.raises(NotImplementedError):
        RobotaUaSource(http=None).parse_list("<html></html>", "https://robota.ua/")
