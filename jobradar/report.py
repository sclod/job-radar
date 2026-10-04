"""Отчёт по вакансиям: markdown и CSV. Вакансии с бронированием — отдельным блоком сверху."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from .models import DEFERMENT_MENTIONED, DEFERMENT_YES, Vacancy

_VERDICT_ICON = {"відгукуватись": "✅", "можна спробувати": "🤔", "не варто": "⛔"}
_SOURCE_TITLE = {"workua": "work.ua", "dou": "DOU", "robotaua": "robota.ua"}

CSV_COLUMNS = [
    "score", "verdict", "deferment_priority", "deferment_site", "deferment_ai",
    "title", "company", "city", "salary", "published_at", "source", "url",
    "matches", "gaps", "note", "first_seen_at",
]


def has_deferment(v: Vacancy) -> bool:
    """Приоритет: бронирование подтверждено фильтром сайта или найдено AI в тексте."""
    return v.deferment == DEFERMENT_YES or (v.score is not None and v.score.deferment == "є")


def deferment_label(v: Vacancy) -> str:
    ai = v.score.deferment if v.score else None
    if v.deferment == DEFERMENT_YES:
        return f"є · фільтр {_SOURCE_TITLE.get(v.source, v.source)}"
    if ai == "є":
        return "є · за текстом (AI)"
    if ai == "немає":
        return "немає (AI)"
    if v.deferment == DEFERMENT_MENTIONED:
        return "згадується в тексті — уточнити"
    if ai == "треба уточнити":
        return "треба уточнити"
    return "—"


def sort_key(v: Vacancy) -> tuple:
    score = v.score.score if v.score else -1
    published = v.published_at.toordinal() if v.published_at else 0
    return (-score, -published, -(v.id or 0))


def render_markdown(vacancies: list[Vacancy], *, scope: str, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now()
    priority = sorted((v for v in vacancies if has_deferment(v)), key=sort_key)
    others = sorted((v for v in vacancies if not has_deferment(v)), key=sort_key)
    scored = sum(1 for v in vacancies if v.score)

    out = [
        "# Job Radar — нові вакансії",
        "",
        f"Згенеровано {generated_at:%Y-%m-%d %H:%M} · {scope}",
        "",
        f"Усього: **{len(vacancies)}** · з бронюванням: **{len(priority)}** · оцінено AI: **{scored}/{len(vacancies)}**",
        "",
    ]
    if not vacancies:
        out += ["Нових вакансій немає.", ""]
        return "\n".join(out)
    if not scored:
        out += ["> AI-оцінка ще не запускалась: `python -m jobradar rank` (потрібен `ANTHROPIC_API_KEY`).", ""]

    out += [f"## 🛡️ З бронюванням — пріоритет ({len(priority)})", ""]
    out += _table(priority) if priority else ["_Немає вакансій з підтвердженим бронюванням._", ""]
    out += [f"## Інші вакансії ({len(others)})", ""]
    out += _table(others) if others else ["_Немає._", ""]

    detailed = [v for v in priority + others if v.score]
    if detailed:
        out += ["## Деталі оцінок", ""]
        for v in detailed:
            out += _details(v)
    return "\n".join(out).rstrip() + "\n"


def _table(vacancies: list[Vacancy]) -> list[str]:
    rows = [
        "| Оцінка | Вердикт | Вакансія | Компанія | Місто | Зарплата | Бронювання | Дата | Джерело |",
        "|---:|---|---|---|---|---|---|---|---|",
    ]
    for v in vacancies:
        score = f"**{v.score.score}**" if v.score else "—"
        verdict = f"{_VERDICT_ICON.get(v.score.verdict, '')} {v.score.verdict}" if v.score else "не оцінено"
        rows.append(
            "| "
            + " | ".join(
                [
                    score,
                    verdict,
                    f"[{_cell(v.title)}]({v.url})",
                    _cell(v.company),
                    _cell(v.city),
                    _cell(v.salary),
                    deferment_label(v),
                    v.published_at.strftime("%d.%m") if v.published_at else "—",
                    _SOURCE_TITLE.get(v.source, v.source),
                ]
            )
            + " |"
        )
    return rows + [""]


def _details(v: Vacancy) -> list[str]:
    assert v.score is not None
    s = v.score
    meta = " · ".join(
        part
        for part in (
            _SOURCE_TITLE.get(v.source, v.source),
            v.city,
            v.salary,
            f"опубліковано {v.published_at:%d.%m.%Y}" if v.published_at else None,
            f"бронювання: {deferment_label(v)}",
        )
        if part
    )
    title = f"{v.title} — {v.company}" if v.company else v.title
    return [
        f"### {s.score} · [{_inline(title)}]({v.url})",
        "",
        meta,
        "",
        f"**{_VERDICT_ICON.get(s.verdict, '')} {s.verdict}.** {_inline(s.note)}",
        "",
        f"- **Збігається:** {_inline('; '.join(s.matches)) or '—'}",
        f"- **Бракує:** {_inline('; '.join(s.gaps)) or '—'}",
        "",
    ]


def _cell(text: str | None) -> str:
    return _inline(text).replace("|", "\\|") or "—"


def _inline(text: str | None) -> str:
    return " ".join((text or "").split())


def render_csv(vacancies: list[Vacancy]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for v in sorted(vacancies, key=lambda v: (not has_deferment(v), *sort_key(v))):
        s = v.score
        writer.writerow(
            {
                "score": s.score if s else "",
                "verdict": s.verdict if s else "",
                "deferment_priority": "yes" if has_deferment(v) else "",
                "deferment_site": v.deferment or "",
                "deferment_ai": s.deferment if s else "",
                "title": v.title,
                "company": v.company or "",
                "city": v.city or "",
                "salary": v.salary or "",
                "published_at": v.published_at.isoformat() if v.published_at else "",
                "source": v.source,
                "url": v.url,
                "matches": "; ".join(s.matches) if s else "",
                "gaps": "; ".join(s.gaps) if s else "",
                "note": s.note if s else "",
                "first_seen_at": v.first_seen_at.isoformat(sep=" ") if v.first_seen_at else "",
            }
        )
    return buffer.getvalue()
