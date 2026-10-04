"""Хранилище: дедупликация по ссылке, «новые с прошлого запуска», оценки."""

from datetime import datetime, timedelta

from conftest import make_vacancy

from jobradar.models import DEFERMENT_MENTIONED, DEFERMENT_YES, VacancyScore

SCORE = VacancyScore(score=75, verdict="відгукуватись", matches=["Python"], gaps=[], deferment="є", note="Ок.")


def test_same_url_is_stored_once(db):
    run = db.start_run()
    assert db.add_vacancy(make_vacancy(1), run) is True
    assert db.add_vacancy(make_vacancy(1, title="Інший заголовок"), run) is False

    rows = db.vacancies()
    assert len(rows) == 1
    assert rows[0].title == "Python Developer 1"  # первая версия не перезаписывается


def test_known_vacancies_are_not_new_in_next_run(db):
    first = db.start_run()
    db.add_vacancy(make_vacancy(1), first)
    db.add_vacancy(make_vacancy(2), first)
    second = db.start_run()
    db.add_vacancy(make_vacancy(2), second)
    db.add_vacancy(make_vacancy(3), second)

    assert db.latest_run()["id"] == second
    assert [v.url for v in db.vacancies(run_id=second)] == [make_vacancy(3).url]
    assert db.known_urls([make_vacancy(1).url, make_vacancy(9).url]) == {make_vacancy(1).url}


def test_deferment_is_upgraded_but_never_downgraded(db):
    run = db.start_run()
    db.add_vacancy(make_vacancy(1), run)
    db.touch(make_vacancy(1).url, DEFERMENT_MENTIONED)
    assert db.vacancies()[0].deferment == DEFERMENT_MENTIONED

    db.add_vacancy(make_vacancy(1, deferment=DEFERMENT_YES), run)
    assert db.vacancies()[0].deferment == DEFERMENT_YES

    db.touch(make_vacancy(1).url, None)
    db.touch(make_vacancy(1).url, DEFERMENT_MENTIONED)
    assert db.vacancies()[0].deferment == DEFERMENT_YES


def test_scores_roundtrip_and_unscored(db):
    run = db.start_run()
    first, second = make_vacancy(1), make_vacancy(2)
    db.add_vacancy(first, run)
    db.add_vacancy(second, run)
    db.save_score(first.id, SCORE, "claude-haiku-4-5")

    assert [v.id for v in db.unscored()] == [second.id]
    assert db.count_unscored() == 1
    stored = db.get(first.id)
    assert stored.score == SCORE
    assert stored.scored_model == "claude-haiku-4-5"

    # повторная оценка той же вакансии не перезаписывает первую
    db.save_score(first.id, SCORE.model_copy(update={"score": 10}), "other")
    assert db.get(first.id).score.score == 75


def test_vacancies_filters(db):
    run = db.start_run()
    for n in (1, 2, 3):
        db.add_vacancy(make_vacancy(n), run)
    db.save_score(1, SCORE, "m")
    db.save_score(2, SCORE.model_copy(update={"score": 40}), "m")

    assert [v.id for v in db.vacancies(min_score=50)] == [1]
    assert [v.id for v in db.vacancies()] == [1, 2, 3]  # по оценке, неоценённые в конце
    assert len(db.vacancies(since=datetime.now() - timedelta(days=1))) == 3
    assert db.vacancies(since=datetime.now() + timedelta(days=1)) == []


def test_pending_batch_vacancies_are_not_offered_again(db):
    run = db.start_run()
    for n in (1, 2):
        db.add_vacancy(make_vacancy(n), run)
    db.add_batch("msgbatch_1", "m", [1])

    assert [v.id for v in db.unscored()] == [2]
    db.finish_batch("msgbatch_1")
    assert {v.id for v in db.unscored()} == {1, 2}
