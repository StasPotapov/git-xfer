"""Предполётные проверки (`doctor`), они же — вход в `apply`.

Пути к служебным файлам берём только через `git rev-parse --git-path`:
склейка `.git/<имя>` ломается на linked worktree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Profile
from .errors import PreflightError
from .gitcmd import Git, GitError

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}

#: Файлы и каталоги, наличие которых означает незавершённую операцию.
#: `CHERRY_PICK_HEAD` и `sequencer` проверяем оба: при одном rev git идёт
#: в single_pick() и каталог sequencer не создаёт вовсе.
BUSY_MARKERS: tuple[tuple[str, str], ...] = (
    ("sequencer", "не завершён cherry-pick/revert серии (git cherry-pick --abort)"),
    ("CHERRY_PICK_HEAD", "не завершён cherry-pick (git cherry-pick --abort)"),
    ("REVERT_HEAD", "не завершён revert (git revert --abort)"),
    ("MERGE_HEAD", "не завершён merge (git merge --abort)"),
    # AUTO_MERGE сюда не входит: git 2.54 оставляет его и после успешного
    # cherry-pick, это кэш merge-ort, а не признак незавершённой операции.
    ("rebase-merge", "идёт rebase (git rebase --abort)"),
    ("rebase-apply", "идёт rebase/am (git rebase --abort или git am --abort)"),
    ("BISECT_LOG", "идёт bisect (git bisect reset)"),
)


@dataclass
class Check:
    name: str
    level: str
    detail: str = ""

    def render(self) -> str:
        mark = _MARK[self.level]
        return f"  {mark} {self.name}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str = "") -> None:
        self.checks.append(Check(name, level, detail))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.level == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level == WARN]

    @property
    def clean(self) -> bool:
        return not self.failures

    def render(self) -> str:
        return "\n".join(check.render() for check in self.checks)

    def raise_if_failed(self) -> None:
        if self.failures:
            details = "\n".join(c.render() for c in self.failures)
            raise PreflightError("предполётная проверка не прошла:\n" + details)


# -- мелкие помощники, нужные и в других модулях -------------------------


def git_path(git: Git, name: str) -> Path:
    """Абсолютный путь к служебному файлу репозитория."""
    return Path(git.out("rev-parse", "--path-format=absolute", "--git-path", name))


def head_sha(git: Git) -> str | None:
    result = git.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    return result.text or None


def current_branch(git: Git) -> str | None:
    """Имя текущей ветки; None — detached или unborn с отвязанным HEAD."""
    result = git.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return result.text or None


def busy_marker(git: Git) -> tuple[str, str] | None:
    """Первая найденная незавершённая операция git."""
    for name, hint in BUSY_MARKERS:
        if git_path(git, name).exists():
            return name, hint
    return None


def is_dirty(git: Git) -> list[str]:
    """Отслеживаемые изменения в дереве. Локи разрешаем: иначе git не может
    обновить индекс и stat-dirty файлы поедут как изменённые."""
    result = git.run(
        "status",
        "--porcelain=v2",
        "--untracked-files=no",
        env={"GIT_OPTIONAL_LOCKS": "1"},
    )
    return result.lines()


def untracked(git: Git) -> list[str]:
    result = git.run(
        "status",
        "--porcelain=v2",
        "--untracked-files=normal",
        env={"GIT_OPTIONAL_LOCKS": "1"},
    )
    return [line for line in result.lines() if line.startswith("? ")]


def stale_xfer_refs(git: Git, keep: str | None = None) -> list[str]:
    refs = git.lines("for-each-ref", "--format=%(refname)", "refs/xfer/")
    return [ref for ref in refs if ref != keep]


# -- собственно проверки --------------------------------------------------


def _check_target_repo(git: Git, report: Report) -> bool:
    probe = git.run("rev-parse", "--is-inside-work-tree", check=False)
    if not probe.ok or probe.text != "true":
        report.add("целевой репозиторий", FAIL, f"{git.repo} — не рабочее дерево git")
        return False
    if git.run("rev-parse", "--is-bare-repository", check=False).text == "true":
        report.add("целевой репозиторий", FAIL, f"{git.repo} — bare-репозиторий")
        return False
    report.add("целевой репозиторий", OK, str(git.repo))
    return True


def _check_head(git: Git, profile: Profile, report: Report) -> None:
    if head_sha(git) is None:
        report.add("HEAD", FAIL, "ветка ещё без коммитов (unborn HEAD)")
        return
    branch = current_branch(git)
    if branch is None:
        report.add("HEAD", FAIL, "detached HEAD — переключитесь на ветку")
        return
    if branch != profile.target_branch:
        report.add(
            "ветка",
            FAIL,
            f"сейчас {branch!r}, а профиль ожидает {profile.target_branch!r}",
        )
        return
    report.add("ветка", OK, branch)


def _check_busy(git: Git, report: Report) -> None:
    marker = busy_marker(git)
    if marker:
        name, hint = marker
        report.add("незавершённых операций нет", FAIL, f"{name}: {hint}")
    else:
        report.add("незавершённых операций нет", OK)


def _check_clean(git: Git, report: Report) -> None:
    changed = is_dirty(git)
    if changed:
        report.add("дерево чистое", FAIL, f"изменений в отслеживаемых файлах: {len(changed)}")
    else:
        report.add("дерево чистое", OK)
    extra = untracked(git)
    if extra:
        report.add("untracked-файлы", WARN, f"{len(extra)} шт. — перенос может их перезаписать")


def _check_source(git_target: Git, git_source: Git, profile: Profile, report: Report) -> None:
    source = profile.source
    if not source.exists():
        report.add("источник доступен", FAIL, f"{source} — путь не существует")
        return
    try:
        heads = git_target.lines(
            "ls-remote", "--heads", "--", str(source), f"refs/heads/{profile.source_branch}"
        )
    except GitError as exc:
        report.add("источник доступен", FAIL, f"{source} — {exc.result.stderr.strip()}")
        return
    if not heads:
        report.add(
            "ветка источника",
            FAIL,
            f"{source}: нет ветки {profile.source_branch!r}",
        )
        return
    report.add("источник доступен", OK, f"{source} ({profile.source_branch})")

    try:
        same = source.resolve() == profile.target.resolve()
    except OSError:
        same = False
    if same:
        report.add("source ≠ target", FAIL, "источник и цель — один и тот же каталог")
    else:
        report.add("source ≠ target", OK)

    fmt_target = git_target.run("rev-parse", "--show-object-format", check=False).text
    fmt_source = git_source.run("rev-parse", "--show-object-format", check=False).text
    if fmt_source and fmt_target and fmt_source != fmt_target:
        report.add(
            "формат объектов",
            FAIL,
            f"источник {fmt_source}, цель {fmt_target} — объекты несовместимы",
        )
    else:
        report.add("формат объектов", OK, fmt_target or "sha1")


def _bool_config(git: Git, key: str) -> bool:
    return git.run("config", "--bool", "--get", key, check=False).text == "true"


def _check_warnings(git_target: Git, git_source: Git, profile: Profile, report: Report) -> None:
    if _bool_config(git_target, "commit.gpgsign"):
        report.add("commit.gpgsign", WARN, "включена подпись коммитов; переносим с --no-gpg-sign")
    if _bool_config(git_target, "rerere.enabled"):
        report.add("rerere.enabled", WARN, "rerere может молча подставить прошлое разрешение конфликта")

    crlf_target = git_target.run("config", "--get", "core.autocrlf", check=False).text or "false"
    crlf_source = git_source.run("config", "--get", "core.autocrlf", check=False).text or "false"
    if crlf_target != crlf_source:
        report.add(
            "core.autocrlf",
            WARN,
            f"источник {crlf_source}, цель {crlf_target} — переводы строк поедут, patch-id разойдутся",
        )

    attrs_target = (profile.target / ".gitattributes").exists()
    attrs_source = (profile.source / ".gitattributes").exists()
    if attrs_target != attrs_source:
        report.add(
            ".gitattributes",
            WARN,
            "есть только в одном из репозиториев — нормализация текста разойдётся",
        )

    if (profile.target / ".gitmodules").exists() or (profile.source / ".gitmodules").exists():
        report.add("сабмодули", WARN, "cherry-pick переносит только гитлинк, содержимое — руками")

    stale = stale_xfer_refs(git_target, keep=profile.ref)
    if stale:
        report.add(
            "refs/xfer",
            WARN,
            f"остались от прошлых прогонов: {', '.join(stale)} (git xfer cleanup)",
        )


def run_preflight(git_target: Git, git_source: Git, profile: Profile) -> Report:
    """Полный прогон проверок. Ничего не меняет в репозиториях."""
    report = Report()
    if not _check_target_repo(git_target, report):
        return report
    _check_head(git_target, profile, report)
    _check_busy(git_target, report)
    _check_clean(git_target, report)
    _check_source(git_target, git_source, profile, report)
    _check_warnings(git_target, git_source, profile, report)
    return report
