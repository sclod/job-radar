"""AI-оценка: разбор ответа модели, ретраи, работа без ключа, Batches API."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pydantic
import pytest
from conftest import fixture_text, make_vacancy

from jobradar.models import DEFERMENT_YES, VacancyScore
from jobradar.ranking import (
    FatalRankingError,
    build_system,
    build_user_message,
    run_rank,
    run_rank_batch_api,
    score_vacancy,
)

VALID = json.loads(fixture_text("claude_score_valid.json"))


# --- разбор ответа модели --------------------------------------------------------

def test_valid_answer_parses():
    score = VacancyScore.model_validate_json(fixture_text("claude_score_valid.json"))
    assert score.score == 82
    assert score.verdict == "відгукуватись"
    assert score.deferment == "є"
    assert score.gaps == ["комерційний досвід з Docker"]


def test_invalid_answer_is_rejected():
    with pytest.raises(pydantic.ValidationError) as exc:
        VacancyScore.model_validate_json(fixture_text("claude_score_invalid.json"))
    bad_fields = {err["loc"][0] for err in exc.value.errors()}
    assert bad_fields == {"score", "verdict", "matches", "gaps"}


@pytest.mark.parametrize("field,value", [("score", -1), ("score", 101), ("deferment", "так"), ("verdict", "")])
def test_out_of_range_values(field, value):
    with pytest.raises(pydantic.ValidationError):
        VacancyScore(**{**VALID, field: value})


def test_schema_sent_to_claude_is_strict():
    schema = anthropic.transform_schema(VacancyScore)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"score", "verdict", "matches", "gaps", "deferment", "note"}
    assert schema["properties"]["verdict"]["enum"] == ["відгукуватись", "можна спробувати", "не варто"]
    assert schema["properties"]["deferment"]["enum"] == ["є", "немає", "треба уточнити"]


def test_sdk_parse_roundtrip_offline(config):
    """Настоящий SDK + messages.parse, но HTTP-ответ — из фикстуры."""
    sent = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=json.loads(fixture_text("claude_message_response.json")))

    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    vacancy = make_vacancy(1, deferment=DEFERMENT_YES)
    score = score_vacancy(client, config.ai, build_system("Моє резюме"), vacancy)

    assert score == VacancyScore(**VALID)
    body = sent[0]
    assert body["model"] == "claude-haiku-4-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["properties"]["verdict"]["enum"][0] == "відгукуватись"
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "Моє резюме" in body["system"][1]["text"]
    assert "Python Developer 1" in body["messages"][0]["content"]


# --- синхронный режим с подставным клиентом ---------------------------------------------

class FakeMessages:
    def __init__(self, script):
        self.script = list(script)  # VacancyScore | Exception | ("refusal"|"max_tokens", None)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else VacancyScore(**VALID)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return SimpleNamespace(stop_reason=item[0], parsed_output=item[1])
        return SimpleNamespace(stop_reason="end_turn", parsed_output=item)


def fake_client(*script):
    return SimpleNamespace(messages=FakeMessages(script))


def validation_error():
    try:
        VacancyScore.model_validate_json('{"score": 5')
    except pydantic.ValidationError as exc:
        return exc


def api_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, json={"error": {"message": "boom"}})
    return cls("boom", response=response, body=None)


def add_vacancies(db, n):
    run = db.start_run()
    for i in range(1, n + 1):
        db.add_vacancy(make_vacancy(i), run)


def test_rank_scores_and_never_rescores(config, db):
    add_vacancies(db, 3)
    client = fake_client()

    stats = run_rank(config, db, client=client, sleep=lambda s: None)
    assert (stats.scored, stats.failed) == (3, 0)
    assert len(client.messages.calls) == 3
    assert all(v.score is not None for v in db.vacancies())

    again = run_rank(config, db, client=client, sleep=lambda s: None)
    assert again.scored == 0
    assert len(client.messages.calls) == 3


def test_request_contents(config, db):
    add_vacancies(db, 1)
    client = fake_client()
    run_rank(config, db, client=client, sleep=lambda s: None)

    call = client.messages.calls[0]
    assert call["output_format"] is VacancyScore
    assert call["model"] == "claude-haiku-4-5"
    assert "effort" not in call.get("output_config", {})  # Haiku 4.5 не принимает effort
    assert "Junior Python developer" in call["system"][1]["text"]
    assert "Python, Django, PostgreSQL." in call["messages"][0]["content"]


def test_retries_on_bad_json_with_more_tokens(config, db):
    add_vacancies(db, 1)
    client = fake_client(validation_error(), VacancyScore(**VALID))

    stats = run_rank(config, db, client=client, sleep=lambda s: None)

    assert stats.scored == 1
    first, second = client.messages.calls
    assert second["max_tokens"] == first["max_tokens"] * 2


def test_retries_on_overload_then_gives_up(config, db):
    add_vacancies(db, 1)
    overloaded = api_error(anthropic.InternalServerError, 529)
    client = fake_client(overloaded, overloaded, overloaded)
    sleeps = []

    stats = run_rank(config, db, client=client, sleep=sleeps.append)

    assert (stats.scored, stats.failed) == (0, 1)
    assert len(client.messages.calls) == config.ai.max_attempts
    assert len(sleeps) == config.ai.max_attempts - 1
    assert db.count_unscored() == 1  # останется на следующий запуск


def test_refusal_is_skipped_without_retry(config, db):
    add_vacancies(db, 2)
    client = fake_client(("refusal", None), VacancyScore(**VALID))

    stats = run_rank(config, db, client=client, sleep=lambda s: None)

    assert (stats.scored, stats.failed) == (1, 1)
    assert len(client.messages.calls) == 2


def test_auth_error_stops_everything(config, db):
    add_vacancies(db, 3)
    config.ai.batch_size = 1
    client = fake_client(VacancyScore(**VALID), api_error(anthropic.AuthenticationError, 401))

    with pytest.raises(FatalRankingError):
        run_rank(config, db, client=client, sleep=lambda s: None)

    assert len(client.messages.calls) == 2  # третью уже не отправляли
    assert db.count_unscored() == 2  # первая оценка сохранена


def test_limit(config, db):
    add_vacancies(db, 5)
    stats = run_rank(config, db, client=fake_client(), limit=2, sleep=lambda s: None)
    assert (stats.scored, stats.remaining) == (2, 3)


def test_without_api_key_parser_keeps_working(config, db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    add_vacancies(db, 2)

    stats = run_rank(config, db)

    assert stats.skipped_no_key is True
    assert db.count_unscored() == 2


def test_missing_profile_is_fatal(config, db, tmp_path):
    add_vacancies(db, 1)
    config.profile = tmp_path / "nope.md"
    with pytest.raises(FatalRankingError, match="profile"):
        run_rank(config, db, client=fake_client())


def test_user_message_truncates_long_text(config):
    vacancy = make_vacancy(1, description="x" * 5000, deferment=DEFERMENT_YES)
    message = build_user_message(vacancy, max_chars=1000)
    assert "[…текст обрізано]" in message
    assert "x" * 1001 not in message
    assert "вакансія у фільтрі бронювання" in message
    assert message.startswith("<vacancy>") and message.endswith("</vacancy>")


# --- Message Batches API -------------------------------------------------------------

class FakeBatches:
    def __init__(self, results, status="ended"):
        self.status = status
        self.results_data = results
        self.created = []

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    def results(self, batch_id):
        return iter(self.results_data)


def batch_item(vacancy_id, kind="succeeded", text=None, stop_reason="end_turn"):
    message = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text or json.dumps(VALID))])
    return SimpleNamespace(custom_id=f"vacancy-{vacancy_id}", result=SimpleNamespace(type=kind, message=message))


def test_batch_api_collects_results(config, db):
    add_vacancies(db, 3)
    batches = FakeBatches([batch_item(1), batch_item(2, kind="errored"), batch_item(3, text='{"score": 999}')])
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))

    stats = run_rank_batch_api(config, db, client=client, sleep=lambda s: None)

    (requests,) = batches.created
    assert {r["custom_id"] for r in requests} == {"vacancy-1", "vacancy-2", "vacancy-3"}
    fmt = requests[0]["params"]["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert (stats.scored, stats.failed, stats.pending) == (1, 2, 0)
    assert db.get(1).score.score == 82
    assert {v.id for v in db.unscored()} == {2, 3}  # ошибки — в следующий раз
    assert db.pending_batches() == []


def test_batch_api_resumes_unfinished_batch(config, db):
    add_vacancies(db, 2)
    batches = FakeBatches([batch_item(1), batch_item(2)], status="in_progress")
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))

    first = run_rank_batch_api(config, db, client=client, wait_minutes=0, sleep=lambda s: None)
    assert (first.scored, first.pending) == (0, 2)
    assert db.unscored() == []  # уже в пакете — повторно не отправляем

    batches.status = "ended"
    second = run_rank_batch_api(config, db, client=client, wait_minutes=0, sleep=lambda s: None)
    assert (second.scored, second.pending) == (2, 0)
    assert len(batches.created) == 1
