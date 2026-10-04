"""Загрузка и валидация config.yaml."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


class _Strict(BaseModel):
    # опечатка в ключе конфига должна давать понятную ошибку, а не молча игнорироваться
    model_config = ConfigDict(extra="forbid")


class HttpSettings(_Strict):
    user_agent: str = DEFAULT_USER_AGENT
    delay_seconds: float = Field(2.0, ge=0)
    timeout_seconds: float = Field(20.0, gt=0)
    max_pages: int = Field(3, ge=1)
    max_details_per_run: int = Field(60, ge=0)
    fetch_details: bool = True


class Keywords(_Strict):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class QueryConfig(_Strict):
    url: str
    name: str | None = None
    # None — источник решает сам (work.ua смотрит на deferment=1 в ссылке)
    deferment: bool | None = None
    # переопределяют глобальные keywords для этого запроса
    include: list[str] | None = None
    exclude: list[str] | None = None

    @property
    def label(self) -> str:
        return self.name or self.url


class SourceConfig(_Strict):
    enabled: bool = True
    queries: list[QueryConfig] = Field(default_factory=list)


class AISettings(_Strict):
    model: str = "claude-haiku-4-5"
    max_tokens: int = Field(1024, ge=256)
    batch_size: int = Field(10, ge=1)
    concurrency: int = Field(4, ge=1)
    max_attempts: int = Field(3, ge=1)
    max_per_run: int = Field(100, ge=1)
    max_vacancy_chars: int = Field(12000, ge=1000)
    effort: str | None = None


class Config(_Strict):
    database: Path = Path("jobradar.db")
    profile: Path = Path("profile.md")
    reports_dir: Path = Path("reports")
    http: HttpSettings = Field(default_factory=HttpSettings)
    keywords: Keywords = Field(default_factory=Keywords)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    ai: AISettings = Field(default_factory=AISettings)

    def resolve_paths(self, base: Path) -> "Config":
        for field in ("database", "profile", "reports_dir"):
            value = getattr(self, field)
            if not value.is_absolute():
                setattr(self, field, base / value)
        return self


class ConfigError(Exception):
    pass


def load_config(path: str | os.PathLike[str]) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Файл конфигурации не найден: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: не удалось разобрать YAML: {exc}") from exc
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: ошибка в настройках:\n{exc}") from exc
    return config.resolve_paths(path.resolve().parent)


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Подхватывает KEY=VALUE из .env, не перезаписывая уже заданные переменные."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
