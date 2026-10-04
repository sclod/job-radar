from . import dou, robotaua, workua  # noqa: F401  (модули регистрируют себя в SOURCES)
from .base import SOURCES, Source, register

__all__ = ["SOURCES", "Source", "register"]
