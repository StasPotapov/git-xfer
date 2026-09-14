"""Цикл переноса: apply / continue / skip / abort.

Коммиты применяются по одному, а не одной командой `cherry-pick A B C`.
При одном rev git идёт в `single_pick()` и каталог `.git/sequencer` не создаёт —
своя очередь в state даёт точный прогресс, честный `abort` (откатывает только
текущий коммит, а не всю уже разрешённую серию) и опции на каждый коммит.

Точка расширения: `Backend` — сейчас единственный бэкенд `cherry-pick`,
сюда же ляжет `format-patch` + `am -3`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

from .config import Profile
from .errors import EXIT_CONFLICT, EXIT_OK, StateError, XferError
from .gitcmd import Git, GitResult
from .preflight import git_path, head_sha
from .state import Progress, State

#: Статусы шага.
OK = "ok"
EMPTY = "empty"
SKIPPED = "skipped"
CONFLICT = "conflict"
FAILED = "failed"


@dataclass
class Options:
    """Опции применения. Дефолты — как описано в README."""

    trailer: bool = True                  # -x: (cherry picked from commit <sha>)
    empty: str = "drop"                   # коммит, ставший пустым, — уже перенесённый
    allow_merges: bool = False            # -m 1 схлопывает влитую ветку, почти никогда не то
    hooks: bool = False                   # падающий линтер не должен рвать серию
    gpg_sign: bool = False
    keep_committer_date: bool = False     # ломает предположение git о неубывающих таймстампах

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict | None) -> "Options":
        data = data or {}
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)


@dataclass
class StepResult:
    sha: str
    status: str
    dst: str = ""
    detail: str = ""


@dataclass
class Outcome:
    results: list[StepResult] = field(default_factory=list)
    conflict: str | None = None
    remaining: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_CONFLICT if self.conflict else EXIT_OK

    def count(self, status: str) -> int:
        return sum(1 for result in self.results if result.status == status)


Reporter = Callable[[str], None]


def _noop(_: str) -> None:
    return None


# -- один шаг -------------------------------------------------------------


def _pick_args(git: Git, sha: str, options: Options, is_merge: bool) -> tuple[list[str], list[str], dict[str, str]]:
    args = ["cherry-pick"]
    if options.trailer:
        args.append("-x")
    args.append(f"--empty={options.empty}")
    args.append("--gpg-sign" if options.gpg_sign else "--no-gpg-sign")
    if is_merge:
        args += ["-m", "1"]
    args += ["--", sha]

    config: list[str] = []
    if not options.hooks:
        # Переносится уже проверенный коммит; падающий pre-commit не должен рвать серию.
        config.append("core.hooksPath=/dev/null")

    env: dict[str, str] = {}
    if options.keep_committer_date:
        env["GIT_COMMITTER_DATE"] = git.out("show", "-s", "--format=%cI", sha)
    return args, config, env


def is_merge_commit(git: Git, sha: str) -> bool:
    parents = git.out("rev-list", "--parents", "-n", "1", sha).split()
    return len(parents) > 2


def pick(git: Git, sha: str, options: Options) -> tuple[str, GitResult, str]:
    """Применить один коммит. Возвращает (статус, результат git, новый HEAD)."""
    before = head_sha(git)
    merge = is_merge_commit(git, sha)
    if merge and not options.allow_merges:
        return SKIPPED, GitResult((), 0, "", ""), before or ""
    args, config, env = _pick_args(git, sha, options, merge)
    result = git.run(*args, check=False, config=config, env=env, mutating=True)
    after = head_sha(git) or ""
    if result.ok:
        return (OK if after != before else EMPTY), result, after
    if git_path(git, "CHERRY_PICK_HEAD").exists():
        return CONFLICT, result, after
    return FAILED, result, after


# -- очередь --------------------------------------------------------------


def _drain(
    git: Git,
    state: State,
    progress: Progress,
    options: Options,
    outcome: Outcome,
    report: Reporter,
) -> Outcome:
    """Прокрутить очередь до конца или до первого конфликта."""
    total = progress.total
    while progress.queue:
        sha = progress.queue.pop(0)
        progress.current = sha
        index = len(progress.done) + 1
        subject = git.out("show", "-s", "--format=%s", sha)
        report(f"[{index}/{total}] {sha[:12]} {subject}")
        status, result, head = pick(git, sha, options)
        if status == CONFLICT:
            progress.expected_head = head
            state.in_progress = progress
            state.save()
            outcome.conflict = sha
            outcome.remaining = list(progress.queue)
            report("  конфликт — разрешите его и выполните: git xfer continue")
            report(result.stdout.strip() or result.stderr.strip())
            return outcome
        if status == FAILED:
            progress.queue.insert(0, sha)
            progress.current = None
            state.in_progress = progress
            state.save()
            raise XferError(f"cherry-pick {sha[:12]} не удался:\n{result.describe()}")
        _record(state, progress, outcome, sha, status, head)
        if status == EMPTY:
            report("  пусто после переноса — пропущен")
        elif status == SKIPPED:
            report("  merge-коммит — пропущен (нужен --allow-merges)")
        progress.expected_head = head or progress.expected_head
        state.save()
    progress.current = None
    state.in_progress = None
    state.save()
    return outcome


def _record(
    state: State,
    progress: Progress,
    outcome: Outcome,
    sha: str,
    status: str,
    head: str,
    detail: str = "",
) -> None:
    """Записать итог шага. Сообщение пользователю — забота вызывающего:
    один и тот же SKIPPED значит разное в apply и в skip."""
    if status == OK and head:
        state.remember(sha, head)
    progress.done.append({"src": sha, "dst": head if status == OK else "", "status": status})
    progress.current = None
    outcome.results.append(
        StepResult(sha=sha, status=status, dst=head if status == OK else "", detail=detail)
    )


def start(
    git: Git,
    profile: Profile,
    state: State,
    shas: list[str],
    options: Options,
    report: Reporter = _noop,
) -> Outcome:
    """Начать новую серию переноса."""
    if state.in_progress:
        raise StateError(
            "есть незавершённый перенос. Закончите его: git xfer continue / skip / abort"
        )
    head = head_sha(git)
    if not head:
        raise XferError("в целевой ветке нет коммитов")
    progress = Progress(
        profile=profile.name,
        head_before=head,
        expected_head=head,
        queue=list(shas),
        done=[],
        opts=options.to_json(),
    )
    state.in_progress = progress
    state.save()
    return _drain(git, state, progress, options, Outcome(), report)


def _load_progress(state: State, profile: Profile) -> Progress:
    progress = state.in_progress
    if not progress:
        raise StateError("незавершённого переноса нет")
    if progress.profile != profile.name:
        raise StateError(
            f"незавершённый перенос принадлежит профилю {progress.profile!r}, "
            f"а вызван {profile.name!r}"
        )
    return progress


def _drift_error(progress: Progress, head: str) -> StateError:
    return StateError(
        "пока перенос стоял на паузе, HEAD ушёл не туда:\n"
        f"  оставляли {progress.expected_head[:12]}, сейчас {head[:12] or '—'}\n"
        "Гадать, что из этого перенос, мы не будем. Разберитесь руками; "
        "забыть незавершённую серию: git xfer cleanup --state"
    )


def _require_expected_head(git: Git, progress: Progress) -> None:
    head = head_sha(git) or ""
    if head != progress.expected_head:
        raise _drift_error(progress, head)


def _one_commit_ahead(git: Git, base: str, head: str) -> bool:
    """Ровно один коммит поверх `base` — так выглядит ручной коммит разрешения."""
    if not base or not head:
        return False
    ahead = git.lines("rev-list", f"{base}..{head}", "--")
    return len(ahead) == 1


def _check_head(git: Git, progress: Progress) -> str:
    """HEAD должен быть там, где мы его оставили."""
    head = head_sha(git) or ""
    if head != progress.expected_head:
        raise StateError(
            "HEAD сдвинулся с тех пор, как перенос встал на паузу:\n"
            f"  ожидали {progress.expected_head[:12]}, а сейчас {head[:12] or '—'}\n"
            "Разберитесь руками; чтобы забыть незавершённую серию: git xfer cleanup --state"
        )
    return head


def resume(
    git: Git,
    profile: Profile,
    state: State,
    *,
    skip: bool = False,
    report: Reporter = _noop,
) -> Outcome:
    """Докрутить очередь после конфликта: `continue` или `skip`."""
    progress = _load_progress(state, profile)
    options = Options.from_json(progress.opts)
    outcome = Outcome()
    current = progress.current
    in_pick = git_path(git, "CHERRY_PICK_HEAD").exists()

    if current:
        if skip:
            if in_pick:
                git.run("cherry-pick", "--skip", check=False, mutating=True)
            else:
                # Без активного cherry-pick пропускать можно только с того
                # места, где перенос встал: иначе очередь поедет поверх
                # чужого HEAD, а уже перенесённые коммиты тихо пропадут.
                _require_expected_head(git, progress)
            _record(state, progress, outcome, current, SKIPPED, "", detail="пропущен вручную")
            report(f"  {current[:12]} пропущен")
        elif in_pick:
            result = git.run("cherry-pick", "--continue", check=False, mutating=True)
            if not result.ok:
                raise XferError(
                    "git cherry-pick --continue не прошёл — конфликт ещё не разрешён:\n"
                    + result.describe()
                )
            head = head_sha(git) or ""
            status = OK if head != progress.expected_head else EMPTY
            _record(state, progress, outcome, current, status, head)
            report(
                f"  {current[:12]} доведён до коммита"
                if status == OK
                else f"  {current[:12]} не дал изменений — пропущен"
            )
        else:
            head = head_sha(git) or ""
            if head == progress.expected_head:
                _record(state, progress, outcome, current, SKIPPED, "", detail="без изменений")
                report(f"  {current[:12]} не оставил изменений — пропущен")
            elif _one_commit_ahead(git, progress.expected_head, head):
                # Человек закоммитил разрешение сам — принимаем как есть.
                _record(state, progress, outcome, current, OK, head, detail="закоммичен вручную")
                report(f"  {current[:12]} уже закоммичен вручную ({head[:12]})")
            else:
                raise _drift_error(progress, head)
        progress.expected_head = head_sha(git) or progress.expected_head
        state.save()
    else:
        _check_head(git, progress)

    return _drain(git, state, progress, options, outcome, report)


def abort(git: Git, profile: Profile, state: State, report: Reporter = _noop) -> Outcome:
    """Откатить только текущий коммит; уже перенесённые остаются на месте."""
    progress = _load_progress(state, profile)
    if git_path(git, "CHERRY_PICK_HEAD").exists():
        result = git.run("cherry-pick", "--abort", check=False, mutating=True)
        if not result.ok:
            raise XferError("git cherry-pick --abort не прошёл:\n" + result.describe())
    done = len(progress.done)
    report(
        f"Серия прервана. Перенесённых коммитов оставлено: {done}; "
        f"не применено: {len(progress.queue) + (1 if progress.current else 0)}"
    )
    state.in_progress = None
    state.save()
    return Outcome(results=[])
