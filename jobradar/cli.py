"""Командная строка: python -m jobradar fetch | rank | report."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config, ConfigError, load_config, load_dotenv
from .db import Database
from .fetcher import FetchResult, run_fetch
from .http import HttpClient
from .models import DEFERMENT_YES
from .ranking import FatalRankingError, RankStats, run_rank, run_rank_batch_api
from .report import has_deferment, render_csv, render_markdown
from .sources import SOURCES

log = logging.getLogger("jobradar")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobradar",
        description="Собирает вакансии с work.ua и DOU, хранит их в SQLite и оценивает под резюме через Claude.",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="путь к config.yaml (по умолчанию ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробные логи")
    sub = parser.add_subparsers(dest="command", required=True, metavar="КОМАНДА")

    fetch = sub.add_parser("fetch", help="собрать новые вакансии")
    fetch.add_argument("--source", action="append", choices=sorted(SOURCES), help="только этот источник (можно повторять)")
    fetch.add_argument("--rank", action="store_true", help="после сбора сразу оценить новые через Claude (если задан ключ)")
    fetch.add_argument("--save-html", metavar="DIR", type=Path, help="сохранять ответы сайтов в папку (для отладки парсеров)")

    rank = sub.add_parser("rank", help="оценить ещё не оценённые вакансии через Claude")
    rank.add_argument("--limit", type=int, help="не больше N вакансий (по умолчанию ai.max_per_run)")
    rank.add_argument("--batch-api", action="store_true", help="через Message Batches API: в 2 раза дешевле, но ответ не сразу")
    rank.add_argument("--wait", type=float, default=30, metavar="МИН", help="сколько минут ждать пакет с --batch-api (по умолчанию 30)")

    report = sub.add_parser("report", help="отчёт по новым вакансиям (markdown или CSV)")
    report.add_argument("--csv", action="store_true", help="CSV вместо markdown")
    scope = report.add_mutually_exclusive_group()
    scope.add_argument("--days", type=int, help="вакансии, впервые найденные за последние N дней")
    scope.add_argument("--all", action="store_true", help="все вакансии в базе")
    report.add_argument("--min-score", type=int, help="только с оценкой не ниже N")
    report.add_argument("-o", "--output", help="куда записать (по умолчанию reports/…; «-» — в консоль)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    config_path = Path(args.config)
    load_dotenv(config_path.resolve().parent / ".env")
    load_dotenv(Path.cwd() / ".env")
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1

    db = Database(config.database)
    try:
        if args.command == "fetch":
            return cmd_fetch(config, db, args)
        if args.command == "rank":
            return cmd_rank(config, db, args)
        return cmd_report(config, db, args)
    finally:
        db.close()


def cmd_fetch(config: Config, db: Database, args: argparse.Namespace) -> int:
    with HttpClient(config.http, save_dir=args.save_html) as http:
        result = run_fetch(config, db, http, only=args.source)
    total = result.total
    log.info(
        "Итого: найдено %d, новых %d, ошибок %d, запросов к сайтам %d",
        total.found, total.new, total.errors, http.requests_made,
    )
    _print_new(result)

    code = 1 if total.errors and not total.found else 0
    if args.rank and result.new_vacancies:
        code = max(code, _rank(config, db, batch_api=False, limit=None, wait=0))
    return code


def cmd_rank(config: Config, db: Database, args: argparse.Namespace) -> int:
    return _rank(config, db, batch_api=args.batch_api, limit=args.limit, wait=args.wait)


def _rank(config: Config, db: Database, *, batch_api: bool, limit: int | None, wait: float) -> int:
    try:
        if batch_api:
            stats = run_rank_batch_api(config, db, limit=limit, wait_minutes=wait)
        else:
            stats = run_rank(config, db, limit=limit)
    except FatalRankingError as exc:
        log.error("AI-оценка остановлена: %s. Уже сохранённые оценки не потеряны.", exc)
        return 1
    _log_rank(stats)
    return 0


def _log_rank(stats: RankStats) -> None:
    if stats.skipped_no_key:
        return
    log.info("Оценено %d, не удалось %d, ждут пакета %d, ждут лимита %d", stats.scored, stats.failed, stats.pending, stats.remaining)


def cmd_report(config: Config, db: Database, args: argparse.Namespace) -> int:
    if args.all:
        vacancies = db.vacancies(min_score=args.min_score)
        scope = "усі вакансії в базі"
    elif args.days:
        since = datetime.now() - timedelta(days=args.days)
        vacancies = db.vacancies(since=since, min_score=args.min_score)
        scope = f"вакансії, знайдені за останні {args.days} дн."
    else:
        run = db.latest_run()
        if run is None:
            log.error("База пуста — сначала запустите `python -m jobradar fetch`")
            return 1
        vacancies = db.vacancies(run_id=run["id"], min_score=args.min_score)
        scope = f"нові з останнього запуску fetch ({run['started_at'].replace('T', ' ')[:16]})"
        if not vacancies:
            log.info("Последний fetch не нашёл новых вакансий. Старые: report --days 7 или --all")

    if args.min_score is not None:
        scope += f", оцінка ≥ {args.min_score}"
    content = render_csv(vacancies) if args.csv else render_markdown(vacancies, scope=scope)

    if args.output == "-":
        sys.stdout.write(content)
        return 0
    if args.output:
        path = Path(args.output)
    else:
        suffix = "csv" if args.csv else "md"
        path = config.reports_dir / f"report-{datetime.now():%Y-%m-%d_%H%M}.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    # BOM в CSV — чтобы Excel правильно открыл кириллицу
    path.write_text(content, encoding="utf-8-sig" if args.csv else "utf-8")
    priority = sum(1 for v in vacancies if has_deferment(v))
    log.info("Отчёт: %s (вакансий %d, с бронированием %d)", path, len(vacancies), priority)
    return 0


def _print_new(result: FetchResult) -> None:
    if not result.new_vacancies:
        print("Новых вакансий нет.")
        return
    ordered = sorted(result.new_vacancies, key=lambda v: v.deferment != DEFERMENT_YES)
    print(f"\nНовые вакансии ({len(ordered)}):")
    for v in ordered:
        mark = "🛡 " if v.deferment == DEFERMENT_YES else "   "
        details = ", ".join(part for part in (v.company, v.city, v.salary) if part)
        print(f"{mark}{v.title}" + (f" — {details}" if details else ""))
        print(f"   {v.url}")
    print()


def _setup_logging(verbose: bool) -> None:
    # консоль Windows / перенаправление в файл с кодировкой cp125x не должны ронять вывод
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx и SDK пишут каждый запрос в INFO — оставляем только для -v
    for noisy in ("httpx", "httpcore", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)
