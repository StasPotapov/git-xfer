"""Проекция коммитов под другой префикс путей.

Когда проект живёт в подкаталоге одного из репозиториев пары, диффы
не сходятся: в монорепозитории путь `apps/mobile/icons/x`, в отдельном
репозитории — просто `icons/x`. `cherry-pick` пути не переписывает, поэтому
перед ним синтезируется временный коммит, дифф которого уже в нужных путях:
берётся поддерево `<sha>:<префикс>` и такое же поддерево у родителя, при
необходимости вкладывается под префикс другой стороны, и пара деревьев
склеивается в коммит с одним родителем. Дальше работает обычный
трёхсторонний merge, и весь остальной механизм — очередь, конфликт,
continue/abort, сухой прогон — остаётся нетронутым.

Обратное направление ничего не удаляет: база слияния содержит только
поддерево под префиксом, файлы вне его отсутствуют и в базе, и в theirs,
а в ours есть — для merge это «добавлено нами», и они остаются на месте.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import XferError
from .gitcmd import Git

US = "\x1f"

#: Служебная личность коммита-основы. Даты фиксированы намеренно: sha
#: проекции должен зависеть только от деревьев и сообщения, иначе один
#: и тот же коммит при каждом запуске даёт новый объект.
FIXED_IDENTITY: dict[str, str] = {
    "GIT_AUTHOR_NAME": "git-xfer",
    "GIT_AUTHOR_EMAIL": "git-xfer@localhost",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "git-xfer",
    "GIT_COMMITTER_EMAIL": "git-xfer@localhost",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+0000",
}

TRAILER = "(cherry picked from commit {sha})"


def empty_tree(git: Git) -> str:
    """Хеш пустого дерева для формата объектов этого репозитория."""
    return git.out("hash-object", "-t", "tree", "/dev/null")


class NotATree(XferError):
    """По пути префикса лежит файл, а не каталог."""


def subtree(git: Git, rev: str, prefix: str) -> str | None:
    """Дерево `<rev>:<prefix>`; None — такого пути в этом коммите нет.

    Через `ls-tree`, а не `rev-parse`: в `rev-parse "<rev>:<путь>^{tree}"`
    суффикс уезжает внутрь пути и команда падает, а без суффикса блоб
    на месте каталога прошёл бы молча. `--full-tree` — чтобы путь считался
    от корня репозитория, а не от текущего каталога.
    """
    if not prefix:
        return git.run("rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}", check=False).text or None
    found = git.run(
        "ls-tree", "--full-tree", "--format=%(objecttype) %(objectname)",
        rev, "--", prefix, check=False,
    )
    if not found.ok or not found.text:
        return None
    kind, _, name = found.text.partition(" ")
    if kind != "tree":
        what = {"blob": "файл", "commit": "сабмодуль"}.get(kind, kind)
        raise NotATree(f"{prefix!r} в {rev} — это {what}, а не каталог")
    return name.strip()


def nest(git: Git, tree: str, prefix: str) -> str:
    """Дерево, в котором `tree` лежит под `prefix`. Без префикса — как есть."""
    if not prefix:
        return tree
    handle, path = tempfile.mkstemp(prefix="git-xfer-index.", suffix=".tmp")
    os.close(handle)
    # read-tree заводит индекс сам; пустой файл он считает повреждённым.
    Path(path).unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": path}
    try:
        git.run("read-tree", f"--prefix={prefix}/", tree, env=env)
        return git.out("write-tree", env=env)
    finally:
        Path(path).unlink(missing_ok=True)


def touches_outside(git: Git, sha: str, prefix: str) -> bool:
    """Задевает ли коммит файлы вне подкаталога."""
    if not prefix:
        return False
    # -m --first-parent: без них merge-коммит не печатает ни строки,
    # и пометка «частичный» у него не появилась бы вовсе. На обычном
    # коммите эти флаги ничего не меняют.
    outside = git.lines(
        "diff-tree", "--no-commit-id", "--name-only", "-r", "--root",
        "-m", "--first-parent", sha, "--", f":(exclude){prefix}",
    )
    return bool(outside)


class Projector:
    """Переписывание префикса путей для одного направления переноса.

    `enabled` False — оба префикса пусты, и проекция не нужна вовсе:
    все методы тогда возвращают исходный sha, и поведение не меняется.
    """

    def __init__(
        self,
        git: Git,
        *,
        source_prefix: str = "",
        target_prefix: str = "",
        pin_ref: str = "",
    ) -> None:
        self.git = git
        self.source_prefix = source_prefix
        self.target_prefix = target_prefix
        self.pin_ref = pin_ref
        self._cache: dict[tuple[str, bool], str | None] = {}
        self._empty: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.source_prefix or self.target_prefix)

    def _empty_tree(self) -> str:
        if self._empty is None:
            self._empty = empty_tree(self.git)
        return self._empty

    def _tree(self, rev: str) -> str:
        found = subtree(self.git, rev, self.source_prefix)
        return nest(self.git, found or self._empty_tree(), self.target_prefix)

    def commit(self, sha: str, *, trailer: bool = True) -> str | None:
        """Синтезировать коммит с переписанными путями.

        None — коммит не задел подкаталог, переносить нечего.
        """
        if not self.enabled:
            return sha
        key = (sha, trailer)
        if key in self._cache:
            return self._cache[key]
        result = self._build(sha, trailer)
        self._cache[key] = result
        return result

    def _build(self, sha: str, trailer: bool) -> str | None:
        new = self._tree(sha)
        # Родителя может не быть вовсе (корневой коммит) — тогда база пуста.
        parent = self.git.run("rev-parse", "--verify", "--quiet", f"{sha}^", check=False).text
        old = self._tree(parent) if parent else nest(
            self.git, self._empty_tree(), self.target_prefix
        )
        if new == old:
            return None
        base = self.git.out(
            "commit-tree", old, "-m", "git-xfer: база проекции", env=FIXED_IDENTITY
        )
        message = self.git.out("show", "-s", "--format=%B", sha).rstrip("\n")
        if trailer:
            # -x сюда не годится: он вписал бы sha синтетического коммита,
            # и оба трейлерных канала дедупликации перестали бы работать.
            message = f"{message}\n\n{TRAILER.format(sha=sha)}"
        env = dict(FIXED_IDENTITY)
        name, email, date = self.git.out(
            "show", "-s", f"--format=%an{US}%ae{US}%aI", sha
        ).split(US)
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_AUTHOR_DATE"] = date
        return self.git.out(
            "commit-tree", new, "-p", base, env=env, stdin=message + "\n"
        )

    # -- удержание объектов от gc ----------------------------------------

    def pin(self, sha: str) -> None:
        """Держать синтетический коммит ссылкой, пока идёт cherry-pick.

        Пережить паузу на конфликте нужно только текущему коммиту: все
        предыдущие уже стали настоящими коммитами целевой ветки.
        """
        if self.pin_ref and self.enabled:
            self.git.run("update-ref", self.pin_ref, sha, mutating=True)

    def unpin(self) -> None:
        if self.pin_ref and self.enabled:
            self.git.run("update-ref", "-d", self.pin_ref, check=False, mutating=True)
