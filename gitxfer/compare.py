"""Сравнение сторон пары: чего нет в цели и что есть только в ней.

Всё считается в целевом репозитории: после `sync` там лежат объекты обеих
сторон, и репозиторий-источник не трогается вовсе — ни ссылкой, ни state.

Утилита здесь только считает. Решать, что из недостающего переносить,
оставлено агенту (скилл) или человеку: для этого `--json` отдаёт всё, на что
можно опереться, — сообщения, файлы, пересечения с правками цели.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .config import Profile, SkipRules
from .discover import (
    NEW,
    RS,
    SIMILAR,
    TRANSFERRED,
    US,
    FileStat,
    Row,
    Survey,
    commit_files,
    survey,
)
from .errors import StateError
from .gitcmd import Git
from .prefix import empty_tree, subtree
from .state import State

STATUS_NAME = {NEW: "new", SIMILAR: "similar", TRANSFERRED: "transferred"}


@dataclass
class FileDiff:
    """Чем деревья проекта отличаются прямо сейчас, по путям проекта."""

    differ: list[str] = field(default_factory=list)
    source_only: list[str] = field(default_factory=list)
    target_only: list[str] = field(default_factory=list)

    @property
    def paths(self) -> set[str]:
        return {*self.differ, *self.source_only, *self.target_only}


@dataclass
class Detail:
    """То, что `compare` знает о коммите сверх строки `list`."""

    body: str = ""
    files: list[FileStat] = field(default_factory=list)
    #: Все файлы коммита сейчас одинаковы по обе стороны. None — нечего
    #: сравнивать (коммит без файлов в подкаталоге).
    in_sync: bool | None = None
    #: Новее самого свежего перенесённого. None — перенесённых в окне нет,
    #: и границы не видно.
    after_last_transferred: bool | None = None
    #: Коммиты «только в цели», задевшие те же файлы.
    overlaps: list[str] = field(default_factory=list)


@dataclass
class Comparison:
    profile: Profile
    limit: int
    files: FileDiff
    forward: Survey
    backward: Survey
    details: dict[str, Detail]
    back_details: dict[str, Detail]
    warnings: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[Row]:
        """Нет в цели и под правило не подпадает — кандидаты на перенос."""
        return [
            row for row in self.forward.rows
            if row.status != TRANSFERRED and not row.skipped
        ]

    @property
    def skipped(self) -> list[Row]:
        return [
            row for row in self.forward.rows
            if row.status != TRANSFERRED and row.skipped
        ]

    @property
    def target_only(self) -> list[Row]:
        return [row for row in self.backward.rows if row.status != TRANSFERRED]

    @property
    def transferred(self) -> int:
        return sum(1 for row in self.forward.rows if row.status == TRANSFERRED)

    def to_json(self) -> dict[str, Any]:
        profile = self.profile
        return {
            "profile": profile.name,
            # Стороны пары: from — откуда смотрим, to — значение для --to.
            "direction": {"from": profile.source_key, "to": profile.target_key},
            "limit": self.limit,
            "source": {
                "path": str(profile.source),
                "branch": profile.source_branch,
                "prefix": profile.source_prefix,
                "ref": profile.ref,
            },
            "target": {
                "path": str(profile.target),
                "branch": profile.target_branch,
                "prefix": profile.target_prefix,
            },
            "files": {
                "differ": self.files.differ,
                "source_only": self.files.source_only,
                "target_only": self.files.target_only,
            },
            # Порядок newest-first, номера — те же, что у `list`.
            "missing": [
                _row_json(row, self.details[row.commit.sha])
                for row in self.forward.rows
                if row.status != TRANSFERRED
            ],
            "transferred_count": self.transferred,
            "target_only": [
                _row_json(row, self.back_details[row.commit.sha])
                for row in self.target_only
            ],
            "warnings": self.warnings,
        }


def _row_json(row: Row, detail: Detail) -> dict[str, Any]:
    commit = row.commit
    return {
        "number": row.number,
        "sha": commit.sha,
        "short": commit.short,
        "date": commit.date,
        "author": commit.author,
        "subject": commit.subject,
        "body": detail.body,
        "status": STATUS_NAME[row.status],
        "mark": row.mark,
        "reason": row.reason,
        "partial": row.partial,
        "skipped": row.skipped or None,
        "files": [
            {"path": item.path, "added": item.added, "deleted": item.deleted}
            for item in detail.files
        ],
        "in_sync": detail.in_sync,
        "after_last_transferred": detail.after_last_transferred,
        "overlaps_target_only": detail.overlaps,
    }


# -- деревья --------------------------------------------------------------


def tree_diff(git: Git, profile: Profile, source_ref: str, target_ref: str) -> FileDiff:
    """Разница деревьев проекта на кончиках обеих веток.

    Подкаталог, которого на стороне нет, считаем пустым: тогда всё
    оказывается «только на другой стороне», что и есть правда.
    `--no-renames`: переименование видно как пара «только тут / только там» —
    зато путь в каждой строке один.
    """
    empty = empty_tree(git)
    src = subtree(git, source_ref, profile.source_prefix) or empty
    dst = subtree(git, target_ref, profile.target_prefix) or empty
    result = FileDiff()
    for line in git.lines("diff-tree", "-r", "--no-renames", "--name-status", dst, src):
        status, _, path = line.partition("\t")
        if not path:
            continue
        if status == "A":
            result.source_only.append(path)
        elif status == "D":
            result.target_only.append(path)
        else:
            result.differ.append(path)
    return result


# -- обратная сторона -----------------------------------------------------


def reversed_profile(profile: Profile) -> Profile:
    """То же направление задом наперёд — для прохода «только в цели»."""
    return replace(
        profile,
        source=profile.target,
        source_branch=profile.target_branch,
        target=profile.source,
        target_branch=profile.source_branch,
        source_prefix=profile.target_prefix,
        target_prefix=profile.source_prefix,
        source_key=profile.target_key,
        target_key=profile.source_key,
        # Правила skip — про то, что не везти ИЗ источника. Коммит, который
        # есть только в цели, под них не подпадает: это расхождение, и агент
        # не должен отбросить его как «служебное».
        skip=SkipRules(),
    )


def reverse_mapping(profile: Profile, state: State, warnings: list[str]) -> dict[str, str]:
    """Маппинг «коммит цели → коммит источника».

    Две половины. Переносы в обратную сторону записаны в state источника —
    его только читаем. Переносы в эту сторону записаны в своём state как
    источник→цель: обращённые, они говорят, что коммит цели сам приехал
    из источника и «только в цели» не считается.
    """
    mapping = {dst: src for src, dst in state.mapping.items()}
    try:
        mapping.update(State.load(profile.source).mapping)
    except StateError as exc:
        warnings.append(f"state источника не читается, обратный маппинг пропущен: {exc}")
    return mapping


# -- подробности по коммитам ----------------------------------------------


def bodies(git: Git, shas: list[str]) -> dict[str, str]:
    """Полные сообщения коммитов — одним вызовом."""
    if not shas:
        return {}
    raw = git.out(
        "log", "--stdin", "--no-walk=unsorted", "--encoding=UTF-8",
        f"--format=%H{US}%B{RS}", stdin="\n".join(shas) + "\n",
    )
    result: dict[str, str] = {}
    for record in raw.split(RS):
        record = record.strip("\n")
        if US in record:
            sha, body = record.split(US, 1)
            result[sha] = body.strip()
    return result


def _details(
    git: Git, view: Survey, prefix: str, changed: set[str]
) -> dict[str, Detail]:
    rows = [row for row in view.rows if row.status != TRANSFERRED]
    shas = [row.commit.sha for row in rows]
    files = commit_files(git, shas, prefix)
    messages = bodies(git, shas)
    # Строки идут newest-first: всё, что выше первой «−», новее последнего
    # переноса.
    boundary = next(
        (index for index, row in enumerate(view.rows) if row.status == TRANSFERRED), None
    )
    result: dict[str, Detail] = {}
    for index, row in enumerate(view.rows):
        if row.status == TRANSFERRED:
            continue
        stats = files.get(row.commit.sha, [])
        result[row.commit.sha] = Detail(
            body=messages.get(row.commit.sha, ""),
            files=stats,
            in_sync=(
                all(item.path not in changed for item in stats) if stats else None
            ),
            after_last_transferred=None if boundary is None else index < boundary,
        )
    return result


def compare(
    git: Git,
    profile: Profile,
    state: State,
    *,
    limit: int | None = None,
    use_patch_id: bool = True,
    allow_merges: bool = False,
) -> Comparison:
    """Собрать сравнение. Кэш patch-id пишется в `state`; сохраняет вызывающий."""
    limit = limit or profile.scan_limit
    warnings: list[str] = []
    files = tree_diff(git, profile, profile.ref, profile.target_branch)
    forward = survey(
        git, profile, state,
        limit=limit, use_patch_id=use_patch_id, allow_merges=allow_merges,
    )
    back_profile = reversed_profile(profile)
    backward = survey(
        git, back_profile, state,
        limit=limit, use_patch_id=use_patch_id, allow_merges=allow_merges,
        source_ref=profile.target_branch,
        target_ref=profile.ref,
        mapping=reverse_mapping(profile, state, warnings),
    )
    changed = files.paths
    details = _details(git, forward, profile.source_prefix, changed)
    back_details = _details(git, backward, profile.target_prefix, changed)

    # Кто из «только в цели» трогал те же файлы — там и жди конфликта.
    # Только «+»: коммит с ≈ — скорее всего двойник источника, и пересечение
    # с ним указывало бы на самого себя.
    touched: dict[str, list[str]] = {}
    for row in backward.rows:
        if row.status != NEW:
            continue
        for item in back_details[row.commit.sha].files:
            touched.setdefault(item.path, []).append(row.commit.short)
    for detail in details.values():
        seen: list[str] = []
        for item in detail.files:
            for short in touched.get(item.path, []):
                if short not in seen:
                    seen.append(short)
        detail.overlaps = seen

    # В state один кэш на оба прохода: горячие ключи — объединение.
    forward.hot_shas |= backward.hot_shas
    return Comparison(
        profile=profile,
        limit=limit,
        files=files,
        forward=forward,
        backward=backward,
        details=details,
        back_details=back_details,
        warnings=warnings,
    )
