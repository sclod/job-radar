"""robota.ua — заглушка.

TODO(robota.ua): парсер не реализован.
- Сайт был недоступен из песочницы, в которой писался код (сетевая политика), поэтому
  разметку и запросы проверить не удалось, а писать парсер вслепую не стали.
- robota.ua — одностраничное приложение: список вакансий, судя по всему, подгружается
  скриптом из их API, а не приходит в HTML. Нужно открыть DevTools → Network на странице
  поиска, найти запрос, который отдаёт вакансии, и выяснить, каким параметром включается
  фильтр «Бронювання співробітників».
- Дальше: реализовать parse_list/parse_detail (или переопределить iter_list_pages, как в dou.py),
  сохранить ответы в tests/fixtures/ и добавить тесты, затем включить источник в config.yaml.
"""

from __future__ import annotations

from ..models import Vacancy
from .base import Source, register


@register
class RobotaUaSource(Source):
    name = "robotaua"
    title = "robota.ua"
    implemented = False

    def parse_list(self, html: str, page_url: str) -> list[Vacancy]:
        raise NotImplementedError("robota.ua: парсер ещё не реализован, см. TODO в jobradar/sources/robotaua.py")

    def parse_detail(self, html: str, vacancy: Vacancy) -> Vacancy:
        raise NotImplementedError("robota.ua: парсер ещё не реализован, см. TODO в jobradar/sources/robotaua.py")
