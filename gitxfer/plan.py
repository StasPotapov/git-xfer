"""Сухой прогон очереди через `git merge-tree`.

Рабочее дерево и индекс не трогаются вовсе: merge-tree считает слияние
в памяти и печатает получившееся дерево. Выходное дерево становится базой
для следующего коммита, поэтому предсказание работает по всей очереди.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .discover import Commit
from .gitcmd import Git, GitError
from .prefix import Projector, empty_tree


@dataclass
class Step:
    commit: Commit
    tree: str
    conflicts: list[str] = field(default_factory=list)
    messages: str = ""
    empty: bool = False

    @property
    def clean(self) -> bool:
        return not self.conflicts


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    base: str = ""

    @property
    def conflicted(self) -> list[Step]:
        return [step for step in self.steps if step.conflicts]

    @property
    def empty(self) -> list[Step]:
        return [step for step in self.steps if step.empty]


def _merge_base_for(git: Git, sha: str, empty: str) -> str:
    """База трёхстороннего слияния — дерево `C^`; для корневого коммита пустое."""
    parent = git.run("rev-parse", "--verify", "--quiet", f"{sha}^", check=False).text
    return parent or empty


def _parse(output: str) -> tuple[str, list[str], str]:
    lines = output.split("\n")
    tree = lines[0].strip() if lines else ""
    conflicts: list[str] = []
    index = 1
    while index < len(lines) and lines[index].strip():
        conflicts.append(lines[index])
        index += 1
    messages = "\n".join(lines[index + 1 :]).strip()
    return tree, conflicts, messages


def dry_run(
    git: Git,
    base: str,
    commits: list[Commit],
    projector: Projector | None = None,
) -> Plan:
    """Прогнать очередь поверх `base`, ничего не меняя в репозитории.

    При смене префикса в merge-tree идёт не сам коммит, а его проекция —
    иначе предсказание считалось бы по чужим путям. Проекция пишет объекты
    в целевой репозиторий, но ни на что не ссылается: их соберёт `git gc`.
    """
    empty = empty_tree(git)
    plan = Plan(base=base)
    current = base
    for commit in commits:
        rev = commit.sha
        if projector and projector.enabled:
            built = projector.commit(commit.sha)
            if built is None:
                # Подкаталог не затронут — шаг не изменит ничего.
                plan.steps.append(Step(commit=commit, tree=current, empty=True))
                continue
            rev = built
        merge_base = _merge_base_for(git, rev, empty)
        result = git.run(
            "merge-tree",
            "--write-tree",
            "--name-only",
            f"--merge-base={merge_base}",
            current,
            rev,
            check=False,
        )
        if result.returncode > 1:
            # 0 — чисто, 1 — конфликт, всё остальное — настоящая ошибка.
            raise GitError(result)
        tree, conflicts, messages = _parse(result.stdout)
        before_tree = git.out("rev-parse", f"{current}^{{tree}}")
        plan.steps.append(
            Step(
                commit=commit,
                tree=tree,
                conflicts=conflicts,
                messages=messages,
                empty=bool(tree) and tree == before_tree and not conflicts,
            )
        )
        if tree:
            current = tree
    return plan
