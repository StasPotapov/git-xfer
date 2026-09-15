"""Таблица коммитов и интерактивный выбор.

Модуль называется picker, а не select: `select` — имя модуля стандартной
библиотеки, и файл с таким именем внутри пакета затеняет его всюду, где
каталог пакета попадает в sys.path. Первым от этого падает subprocess,
который импортирует select у себя внутри.

Ширину колонок считаем по `unicodedata`, а не по `len()`: кириллица тут ни
при чём, а вот CJK и комбинирующие символы в заголовках ломают вёрстку.
Внешний пейджер не запускаем — `less` съест stdin, из которого мы читаем ввод.
"""

from __future__ import annotations

import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass

from .discover import NEW, PARTIAL, PARTIAL_HINT, SIMILAR, STATUS_HINT, TRANSFERRED, Row
from .errors import XferError

PAGE_SIZE = 40

_TOKEN_RE = re.compile(r"^(?P<neg>!)?(?P<start>\d+)(?:-(?P<end>\d+))?$")


class SelectionError(XferError):
    """Пользователь ввёл диапазон, который мы не понимаем."""


# -- ширина текста --------------------------------------------------------


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def clip(text: str, width: int) -> str:
    """Обрезать по видимой ширине, добавив многоточие."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        step = char_width(char)
        if used + step > width - 1:
            break
        out.append(char)
        used += step
    return "".join(out) + "…"


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


# -- разбор выбора --------------------------------------------------------


def parse_selection(spec: str, count: int) -> list[int]:
    """`1,3,5-7`, `all`, `!5` → отсортированный список номеров.

    Весь ввод валидируется целиком до того, как что-то произойдёт.
    """
    spec = spec.strip()
    if not spec:
        raise SelectionError("пустой выбор")
    tokens = [token for token in re.split(r"[\s,]+", spec) if token]
    selected: set[int] = set()
    excluded: set[int] = set()
    for token in tokens:
        low = token.lower()
        if low in ("all", "*", "все"):
            selected |= set(range(1, count + 1))
            continue
        match = _TOKEN_RE.match(token)
        if not match:
            raise SelectionError(f"не понимаю {token!r}; ожидается 1, 3-7, !5 или all")
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if start > end:
            start, end = end, start
        if start < 1 or end > count:
            raise SelectionError(
                f"{token!r} выходит за пределы списка (1..{count})"
            )
        target = excluded if match.group("neg") else selected
        target |= set(range(start, end + 1))
    result = sorted(selected - excluded)
    if not result:
        raise SelectionError("после исключений не осталось ни одного коммита")
    return result


# -- вывод таблицы --------------------------------------------------------


@dataclass
class Layout:
    number: int
    mark: int
    short: int
    date: int
    author: int
    subject: int


def _layout(rows: list[Row], total_width: int) -> Layout:
    number = max(2, len(str(max((row.number for row in rows), default=1))))
    # Метка бывает двухсимвольной («+*»), и ширину колонки считаем по факту:
    # с фиксированной единицей вёрстка поехала бы на первой же такой строке.
    mark = max((display_width(row.mark) for row in rows), default=1)
    short = max((display_width(row.commit.short) for row in rows), default=7)
    date = max((display_width(row.commit.date) for row in rows), default=10)
    author = min(18, max((display_width(row.commit.author) for row in rows), default=6))
    # номер + пробел + метка + пробел + хеш + пробел + дата + пробел + автор + пробел
    fixed = number + 1 + mark + 1 + short + 1 + date + 1 + author + 1
    subject = max(20, total_width - fixed)
    return Layout(number, mark, short, date, author, subject)


def render_rows(rows: list[Row], width: int | None = None) -> list[str]:
    if not rows:
        return []
    width = width or shutil.get_terminal_size((100, 24)).columns
    layout = _layout(rows, width)
    lines = []
    for row in rows:
        commit = row.commit
        lines.append(
            " ".join(
                [
                    str(row.number).rjust(layout.number),
                    pad(row.mark, layout.mark),
                    pad(clip(commit.short, layout.short), layout.short),
                    pad(clip(commit.date, layout.date), layout.date),
                    pad(clip(commit.author, layout.author), layout.author),
                    clip(commit.subject, layout.subject),
                ]
            ).rstrip()
        )
    return lines


def legend(rows: list[Row]) -> str:
    counts = {NEW: 0, SIMILAR: 0, TRANSFERRED: 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    parts = [f"{mark} {STATUS_HINT[mark]}: {counts.get(mark, 0)}" for mark in (NEW, SIMILAR, TRANSFERRED)]
    partial = sum(1 for row in rows if row.partial)
    if partial:
        parts.append(f"{PARTIAL} {PARTIAL_HINT}: {partial}")
    return "   ".join(parts)


# -- интерактив -----------------------------------------------------------

PROMPT = (
    "Выбор [1,3,5-7 | all | !N | /текст — фильтр | n/p — страница | q — отмена]: "
)


def require_tty() -> None:
    if not sys.stdin.isatty():
        raise XferError(
            "интерактивный выбор требует терминала. "
            "В скрипте укажите коммиты явно: --commits 1,3,5-7 или --sha <hash>..."
        )


def choose(rows: list[Row], *, page_size: int = PAGE_SIZE) -> list[Row]:
    """Показать список и вернуть выбранные строки. Пустой ввод — отмена."""
    require_tty()
    if not rows:
        return []
    view = rows
    page = 0
    query = ""
    while True:
        pages = max(1, (len(view) + page_size - 1) // page_size)
        page = min(page, pages - 1)
        chunk = view[page * page_size : (page + 1) * page_size]
        print()
        for line in render_rows(chunk):
            print(line)
        print()
        tail = f"страница {page + 1}/{pages}, коммитов {len(view)}"
        if query:
            tail += f", фильтр /{query}"
        print(f"  {legend(rows)}   ({tail})")
        try:
            answer = input(PROMPT).strip()
        except EOFError:
            print()
            return []
        if not answer or answer.lower() in ("q", "quit", "отмена"):
            return []
        low = answer.lower()
        if low in ("n", "next", ">"):
            page = min(page + 1, pages - 1)
            continue
        if low in ("p", "prev", "<"):
            page = max(page - 1, 0)
            continue
        if answer.startswith("/"):
            query = answer[1:].strip()
            needle = query.lower()
            view = [
                row
                for row in rows
                if needle in row.commit.subject.lower()
                or needle in row.commit.author.lower()
                or row.commit.sha.startswith(needle)
            ] if needle else rows
            page = 0
            if not view:
                print(f"  Ничего не нашлось по {query!r}")
                view = rows
                query = ""
            continue
        try:
            numbers = parse_selection(answer, len(rows))
        except SelectionError as exc:
            print(f"  {exc}")
            continue
        index = {row.number: row for row in rows}
        return [index[number] for number in numbers]
