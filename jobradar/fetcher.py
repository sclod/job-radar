"""Команда fetch: обойти источники, отфильтровать, сохранить новые вакансии."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config, QueryConfig, SourceConfig
from .db import Database
from .http import FetchError, HttpClient
from .models import DEFERMENT_MENTIONED, DEFERMENT_YES, Vacancy
from .sources import SOURCES, Source
from .textutil import matches_any, mentions_deferment

log = logging.getLogger(__name__)


@dataclass
class SourceStats:
    found: int = 0  # уникальных вакансий после фильтров
    filtered: int = 0  # отброшено ключевыми словами
    new: int = 0
    postponed: int = 0  # новые, не обработанные из-за лимита max_details_per_run
    errors: int = 0


@dataclass
class FetchResult:
    run_id: int
    stats: dict[str, SourceStats] = field(default_factory=dict)
    new_vacancies: list[Vacancy] = field(default_factory=list)

    @property
    def total(self) -> SourceStats:
        total = SourceStats()
        for s in self.stats.values():
            total.found += s.found
            total.filtered += s.filtered
            total.new += s.new
            total.postponed += s.postponed
            total.errors += s.errors
        return total


def run_fetch(
    config: Config,
    db: Database,
    http: HttpClient,
    *,
    only: list[str] | None = None,
) -> FetchResult:
    for name in only or []:
        if name not in config.sources:
            log.warning("Источника %r нет в config.yaml — нечего собирать", name)
    result = FetchResult(run_id=db.start_run())
    for name, source_config in config.sources.items():
        if only and name not in only:
            continue
        cls = SOURCES.get(name)
        if cls is None:
            log.error("Неизвестный источник %r в config.yaml (есть: %s)", name, ", ".join(SOURCES))
            result.stats[name] = SourceStats(errors=1)
            continue
        if not source_config.enabled:
            log.info("%s: выключен в config.yaml", cls.title)
            continue
        if not cls.implemented:
            log.warning("%s: парсер не реализован (заглушка) — пропускаю", cls.title)
            continue
        source = cls(http, config.http)
        try:
            stats = _collect(source, source_config, config, db, result)
        except Exception:  # поломка одного источника не должна валить весь прогон
            log.exception("%s: непредвиденная ошибка, источник пропущен", cls.title)
            stats = SourceStats(errors=1)
        result.stats[name] = stats

    total = result.total
    db.finish_run(result.run_id, found=total.found, new=total.new, errors=total.errors)
    return result


def _collect(
    source: Source,
    source_config: SourceConfig,
    config: Config,
    db: Database,
    result: FetchResult,
) -> SourceStats:
    stats = SourceStats()
    seen: dict[str, Vacancy] = {}

    for query in source_config.queries:
        query_deferment = source.query_deferment(query)
        try:
            for page_no, items in enumerate(source.iter_list_pages(query), start=1):
                log.info("%s [%s] стр. %d: %d вакансий", source.title, query.label, page_no, len(items))
                if page_no == 1 and not items:
                    log.warning(
                        "%s [%s]: на первой странице 0 вакансий — либо по запросу пусто, "
                        "либо изменилась разметка (проверьте через fetch --save-html)",
                        source.title, query.label,
                    )
                for vacancy in items:
                    if not _passes_keywords(vacancy, query, config):
                        stats.filtered += 1
                        continue
                    if query_deferment == DEFERMENT_YES:
                        vacancy.deferment = DEFERMENT_YES
                    _merge(seen, vacancy)
        except FetchError as exc:
            stats.errors += 1
            log.warning("%s [%s]: ошибка запроса, иду дальше: %s", source.title, query.label, exc)

    stats.found = len(seen)
    known = db.known_urls(seen)
    for url in known:
        db.touch(url, seen[url].deferment)
    fresh = [v for url, v in seen.items() if url not in known]

    limit = config.http.max_details_per_run if config.http.fetch_details else len(fresh)
    for vacancy in fresh[:limit]:
        if config.http.fetch_details:
            try:
                source.fetch_details(vacancy)
            except FetchError as exc:
                # не сохраняем — попробуем снова при следующем запуске
                stats.errors += 1
                log.warning("%s: не удалось открыть вакансию, повторю в следующий раз: %s", source.title, exc)
                continue
            if not vacancy.description:
                log.warning(
                    "%s: не нашёл текст вакансии на странице %s — сохраняю краткое описание",
                    source.title, vacancy.url,
                )
        if vacancy.deferment is None and mentions_deferment(vacancy.text):
            vacancy.deferment = DEFERMENT_MENTIONED
        if db.add_vacancy(vacancy, result.run_id):
            stats.new += 1
            result.new_vacancies.append(vacancy)

    stats.postponed = max(0, len(fresh) - limit)
    if stats.postponed:
        log.info(
            "%s: лимит max_details_per_run=%d, ещё %d новых будут обработаны в следующий запуск",
            source.title, limit, stats.postponed,
        )
    log.info(
        "%s: найдено %d (отфильтровано %d), новых %d, ошибок %d",
        source.title, stats.found, stats.filtered, stats.new, stats.errors,
    )
    return stats


def _passes_keywords(vacancy: Vacancy, query: QueryConfig, config: Config) -> bool:
    include = query.include if query.include is not None else config.keywords.include
    exclude = query.exclude if query.exclude is not None else config.keywords.exclude
    if exclude and matches_any(vacancy.title, exclude):
        return False
    if include and not matches_any(f"{vacancy.title} {vacancy.snippet or ''}", include):
        return False
    return True


def _merge(seen: dict[str, Vacancy], vacancy: Vacancy) -> None:
    """Одна вакансия может прийти из нескольких запросов — объединяем признак бронирования."""
    existing = seen.get(vacancy.url)
    if existing is None:
        seen[vacancy.url] = vacancy
    elif vacancy.deferment == DEFERMENT_YES:
        existing.deferment = DEFERMENT_YES
