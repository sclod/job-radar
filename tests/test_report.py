import csv
import io
from datetime import datetime

from conftest import make_vacancy

from jobradar.models import DEFERMENT_MENTIONED, DEFERMENT_YES, VacancyScore
from jobradar.report import CSV_COLUMNS, render_csv, render_markdown


def score(value, deferment="треба уточнити", verdict="можна спробувати"):
    return VacancyScore(score=value, verdict=verdict, matches=["Python"], gaps=["Docker"], deferment=deferment, note="Нотатка.")


def sample():
    return [
        make_vacancy(1, title="Без бронювання, висока оцінка", score=score(95, verdict="відгукуватись")),
        make_vacancy(2, title="Фільтр work.ua", deferment=DEFERMENT_YES, score=score(60)),
        make_vacancy(3, title="AI знайшов бронювання", source="dou", score=score(80, deferment="є")),
        make_vacancy(4, title="Згадка в тексті", deferment=DEFERMENT_MENTIONED),
        make_vacancy(5, title="Назва | з трубою", company="A|B"),
    ]


def test_markdown_puts_deferment_first_and_sorts_by_score():
    md = render_markdown(sample(), scope="тест", generated_at=datetime(2026, 10, 4, 12, 0))

    priority, others = md.split("## Інші вакансії")
    assert "## 🛡️ З бронюванням — пріоритет (2)" in priority
    assert priority.index("AI знайшов бронювання") < priority.index("Фільтр work.ua")  # 80 > 60
    assert "є · фільтр work.ua" in priority
    assert "є · за текстом (AI)" in priority
    assert "Без бронювання" not in priority
    assert others.index("Без бронювання, висока оцінка") < others.index("Згадка в тексті")
    assert "згадується в тексті — уточнити" in others
    assert "Усього: **5** · з бронюванням: **2** · оцінено AI: **3/5**" in md


def test_markdown_escapes_table_cells_and_lists_details():
    md = render_markdown(sample(), scope="тест")
    assert "Назва \\| з трубою" in md
    assert "A\\|B" in md
    assert "## Деталі оцінок" in md
    assert "### 95 · [Без бронювання, висока оцінка — Альфа](https://www.work.ua/jobs/1001/)" in md
    assert "- **Бракує:** Docker" in md


def test_markdown_without_scores_and_empty():
    md = render_markdown([make_vacancy(1)], scope="тест")
    assert "AI-оцінка ще не запускалась" in md
    assert "Деталі оцінок" not in md
    assert "Нових вакансій немає." in render_markdown([], scope="тест")


def test_csv():
    rows = list(csv.DictReader(io.StringIO(render_csv(sample()))))

    assert list(rows[0]) == CSV_COLUMNS
    assert [r["deferment_priority"] for r in rows] == ["yes", "yes", "", "", ""]
    assert [r["title"] for r in rows[:2]] == ["AI знайшов бронювання", "Фільтр work.ua"]
    assert rows[0]["score"] == "80"
    assert rows[0]["matches"] == "Python"
    assert rows[-1]["score"] == ""
