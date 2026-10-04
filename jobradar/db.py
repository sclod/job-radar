"""SQLite-хранилище: вакансии, запуски fetch, AI-оценки."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from .models import DEFERMENT_MENTIONED, DEFERMENT_YES, Vacancy, VacancyScore

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    found       INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vacancies (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    url            TEXT NOT NULL UNIQUE,      -- дедупликация по ссылке
    title          TEXT NOT NULL,
    company        TEXT,
    city           TEXT,
    salary         TEXT,
    published_at   TEXT,                      -- YYYY-MM-DD
    deferment      TEXT,                      -- 'yes' | 'mentioned' | NULL
    description    TEXT,                      -- полный текст вакансии
    snippet        TEXT,                      -- краткое описание из списка
    first_seen_at  TEXT NOT NULL,
    first_seen_run INTEGER REFERENCES runs(id),
    last_seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vacancies_run ON vacancies(first_seen_run);

CREATE TABLE IF NOT EXISTS scores (
    vacancy_id INTEGER PRIMARY KEY REFERENCES vacancies(id) ON DELETE CASCADE,
    score      INTEGER NOT NULL,
    verdict    TEXT NOT NULL,
    matches    TEXT NOT NULL,                 -- JSON-массив
    gaps       TEXT NOT NULL,                 -- JSON-массив
    deferment  TEXT NOT NULL,
    note       TEXT NOT NULL,
    model      TEXT NOT NULL,
    scored_at  TEXT NOT NULL
);

-- пакеты Message Batches API (rank --batch-api)
CREATE TABLE IF NOT EXISTS ai_batches (
    id          TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    vacancy_ids TEXT NOT NULL,                -- JSON-массив id вакансий в пакете
    created_at  TEXT NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0
);
"""

# вакансии, которые сейчас оцениваются в незавершённом пакете Batches API
_IN_PENDING_BATCH = """
v.id IN (SELECT value FROM ai_batches b, json_each(b.vacancy_ids) WHERE b.done = 0)
"""

_SELECT = """
SELECT v.*, s.score, s.verdict, s.matches, s.gaps, s.deferment AS ai_deferment, s.note, s.model
FROM vacancies v LEFT JOIN scores s ON s.vacancy_id = v.id
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- запуски -------------------------------------------------------------

    def start_run(self) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, found: int, new: int, errors: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, found = ?, new = ?, errors = ? WHERE id = ?",
                (_now(), found, new, errors, run_id),
            )

    def latest_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    # --- вакансии ------------------------------------------------------------

    def known_urls(self, urls: Iterable[str]) -> set[str]:
        urls = list(urls)
        known: set[str] = set()
        for i in range(0, len(urls), 500):
            chunk = urls[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(f"SELECT url FROM vacancies WHERE url IN ({marks})", chunk)
            known.update(row["url"] for row in rows)
        return known

    def add_vacancy(self, vacancy: Vacancy, run_id: int | None) -> bool:
        """Сохраняет вакансию. True — новая, False — уже была (обновлён last_seen)."""
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO vacancies (source, url, title, company, city, salary, published_at,
                                       deferment, description, snippet, first_seen_at,
                                       first_seen_run, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO NOTHING
                """,
                (
                    vacancy.source, vacancy.url, vacancy.title, vacancy.company, vacancy.city,
                    vacancy.salary, vacancy.published_at.isoformat() if vacancy.published_at else None,
                    vacancy.deferment, vacancy.description, vacancy.snippet, now, run_id, now,
                ),
            )
        if cur.rowcount:
            vacancy.id = int(cur.lastrowid)
            return True
        self.touch(vacancy.url, vacancy.deferment)
        return False

    def touch(self, url: str, deferment: str | None = None) -> None:
        """Вакансия снова встретилась: обновляем last_seen и, если надо, признак бронирования."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE vacancies SET last_seen_at = :now,
                    deferment = CASE
                        WHEN :new = :yes THEN :yes
                        WHEN :new = :mentioned AND deferment IS NULL THEN :mentioned
                        ELSE deferment END
                WHERE url = :url
                """,
                {
                    "now": _now(), "new": deferment, "url": url,
                    "yes": DEFERMENT_YES, "mentioned": DEFERMENT_MENTIONED,
                },
            )

    def get(self, vacancy_id: int) -> Vacancy | None:
        row = self.conn.execute(_SELECT + " WHERE v.id = ?", (vacancy_id,)).fetchone()
        return _to_vacancy(row) if row else None

    def vacancies(
        self,
        *,
        run_id: int | None = None,
        since: datetime | None = None,
        min_score: int | None = None,
    ) -> list[Vacancy]:
        """Выборка для отчёта: по запуску fetch, по дате первого появления или все."""
        where, params = [], []
        if run_id is not None:
            where.append("v.first_seen_run = ?")
            params.append(run_id)
        if since is not None:
            where.append("v.first_seen_at >= ?")
            params.append(since.isoformat(timespec="seconds"))
        if min_score is not None:
            where.append("s.score >= ?")
            params.append(min_score)
        sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY s.score IS NULL, s.score DESC, v.published_at DESC, v.id DESC"
        return [_to_vacancy(row) for row in self.conn.execute(sql, params)]

    def unscored(self, limit: int | None = None) -> list[Vacancy]:
        """Ещё не оценённые вакансии (кроме тех, что ждут ответа в пакете Batches API), новые первыми."""
        sql = _SELECT + f" WHERE s.vacancy_id IS NULL AND NOT {_IN_PENDING_BATCH} ORDER BY v.id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_to_vacancy(row) for row in self.conn.execute(sql)]

    def count_unscored(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM vacancies v LEFT JOIN scores s ON s.vacancy_id = v.id "
            f"WHERE s.vacancy_id IS NULL AND NOT {_IN_PENDING_BATCH}"
        ).fetchone()[0]

    # --- оценки --------------------------------------------------------------

    def save_score(self, vacancy_id: int, score: VacancyScore, model: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scores (vacancy_id, score, verdict, matches, gaps, deferment, note, model, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vacancy_id) DO NOTHING
                """,
                (
                    vacancy_id, score.score, score.verdict,
                    json.dumps(score.matches, ensure_ascii=False),
                    json.dumps(score.gaps, ensure_ascii=False),
                    score.deferment, score.note, model, _now(),
                ),
            )

    # --- пакеты Batches API ----------------------------------------------------

    def add_batch(self, batch_id: str, model: str, vacancy_ids: list[int]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO ai_batches (id, model, vacancy_ids, created_at) VALUES (?, ?, ?, ?)",
                (batch_id, model, json.dumps(vacancy_ids), _now()),
            )

    def pending_batches(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM ai_batches WHERE done = 0 ORDER BY created_at").fetchall()

    def finish_batch(self, batch_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE ai_batches SET done = 1 WHERE id = ?", (batch_id,))


def _to_vacancy(row: sqlite3.Row) -> Vacancy:
    vacancy = Vacancy(
        source=row["source"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        city=row["city"],
        salary=row["salary"],
        published_at=date.fromisoformat(row["published_at"]) if row["published_at"] else None,
        deferment=row["deferment"],
        snippet=row["snippet"],
        description=row["description"],
        id=row["id"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
    )
    if row["score"] is not None:
        vacancy.score = VacancyScore(
            score=row["score"],
            verdict=row["verdict"],
            matches=json.loads(row["matches"]),
            gaps=json.loads(row["gaps"]),
            deferment=row["ai_deferment"],
            note=row["note"],
        )
        vacancy.scored_model = row["model"]
    return vacancy
