"""Модели данных: вакансия и AI-оценка."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# Значения признака бронирования в базе.
DEFERMENT_YES = "yes"  # подтверждено фильтром источника (work.ua ?deferment=1)
DEFERMENT_MENTIONED = "mentioned"  # бронирование упоминается в тексте — надо проверить


class VacancyScore(BaseModel):
    """Структурированный ответ модели. Схема уходит в Claude через structured outputs."""

    score: int = Field(ge=0, le=100, description="Наскільки вакансія підходить кандидату, 0-100")
    verdict: Literal["відгукуватись", "можна спробувати", "не варто"]
    matches: list[str] = Field(description="Що з вимог збігається з резюме")
    gaps: list[str] = Field(description="Чого кандидату бракує для цієї вакансії")
    deferment: Literal["є", "немає", "треба уточнити"] = Field(
        description="Чи пропонує роботодавець бронювання від мобілізації"
    )
    note: str = Field(description="Один короткий підсумок одним реченням")


@dataclass
class Vacancy:
    source: str
    url: str
    title: str
    company: str | None = None
    city: str | None = None
    salary: str | None = None
    published_at: date | None = None
    deferment: str | None = None
    snippet: str | None = None  # краткое описание со страницы списка
    description: str | None = None  # полный текст со страницы вакансии

    # заполняются базой
    id: int | None = None
    first_seen_at: datetime | None = None
    score: VacancyScore | None = None
    scored_model: str | None = None

    @property
    def text(self) -> str:
        return self.description or self.snippet or ""
