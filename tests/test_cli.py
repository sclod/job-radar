"""Команды целиком: fetch → rank → report на фикстурах."""

import csv

import httpx
import pytest
import yaml
from test_fetcher import FakeSites

from jobradar import cli
from jobradar.http import HttpClient


@pytest.fixture
def config_file(tmp_path):
    (tmp_path / "profile.md").write_text("Junior Python developer", encoding="utf-8")
    data = {
        "database": "data/test.db",
        "reports_dir": "out",
        "http": {"delay_seconds": 0},
        "keywords": {"exclude": ["senior", "lead"]},
        "sources": {
            "workua": {"queries": [{"name": "python", "url": "https://www.work.ua/jobs-python/?deferment=1"}]},
            "dou": {"queries": [{"url": "https://jobs.dou.ua/vacancies/?category=Python&exp=0-1"}]},
            "robotaua": {"enabled": False},
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def fake_sites(monkeypatch):
    sites = FakeSites()

    def factory(settings, **kwargs):
        return HttpClient(settings, transport=httpx.MockTransport(sites), sleep=lambda s: None, **kwargs)

    monkeypatch.setattr(cli, "HttpClient", factory)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return sites


def test_full_cycle_without_api_key(config_file, tmp_path, capsys):
    assert cli.main(["-c", str(config_file), "fetch", "--rank"]) == 0
    printed = capsys.readouterr().out
    assert "Новые вакансии (7):" in printed
    assert "🛡 Junior Python розробник" in printed

    assert cli.main(["-c", str(config_file), "rank"]) == 0

    assert cli.main(["-c", str(config_file), "report"]) == 0
    (report,) = (tmp_path / "out").glob("report-*.md")
    text = report.read_text(encoding="utf-8")
    assert "## 🛡️ З бронюванням — пріоритет (4)" in text  # все с work.ua; «згадується» на DOU — не приоритет
    assert "Junior Python розробник" in text

    csv_path = tmp_path / "r.csv"
    assert cli.main(["-c", str(config_file), "report", "--csv", "-o", str(csv_path)]) == 0
    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM для Excel
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    assert len(rows) == 7


def test_second_fetch_reports_nothing_new(config_file, capsys):
    cli.main(["-c", str(config_file), "fetch"])
    cli.main(["-c", str(config_file), "fetch"])
    assert "Новых вакансий нет." in capsys.readouterr().out

    assert cli.main(["-c", str(config_file), "report", "-o", "-"]) == 0
    assert "Нових вакансій немає." in capsys.readouterr().out

    assert cli.main(["-c", str(config_file), "report", "--all", "-o", "-"]) == 0
    assert "Усього: **7**" in capsys.readouterr().out


def test_report_before_fetch(config_file):
    assert cli.main(["-c", str(config_file), "report"]) == 1


def test_bad_config(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("http:\n  dellay_seconds: 1\n", encoding="utf-8")
    assert cli.main(["-c", str(path), "fetch"]) == 1
    assert "dellay_seconds" in caplog.text


def test_all_sources_down_gives_error_code(config_file, fake_sites):
    fake_sites.fail["work.ua"] = 500
    fake_sites.fail["dou.ua"] = 403
    assert cli.main(["-c", str(config_file), "fetch"]) == 1


def test_dotenv_is_loaded_but_env_wins(tmp_path, monkeypatch):
    from jobradar.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text("# comment\nANTHROPIC_API_KEY='from-file'\nexport OTHER=1\n", encoding="utf-8")
    monkeypatch.delenv("OTHER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")

    load_dotenv(env)

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "from-env"
    assert os.environ["OTHER"] == "1"
    monkeypatch.delenv("OTHER")
