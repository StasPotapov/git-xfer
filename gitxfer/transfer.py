"""Цикл переноса: apply / continue / skip / abort.

Коммиты применяются по одному, а не одной командой `cherry-pick A B C`.
При одном rev git идёт в `single_pick()` и каталог `.git/sequencer` не создаёт —
своя очередь в state даёт точный прогресс, честный `abort` (откатывает только
текущий коммит, а не всю уже разрешённую серию) и опции на каждый коммит.

Точка расширения: `Backend` — сейчас единственный бэкенд `cherry-pick`,
сюда же ляжет `format-patch` + `am -3`.

Смена префикса путей сделана не бэкендом, а подстановкой: `Projector`
синтезирует коммит с переписанными путями, и `cherry-pick` получает его
вместо оригинала. В очереди, в state и в отчётах при этом всюду остаются
оригинальные sha — синтетический живёт ровно от `pick()` до коммита.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

from . import logbook
from .config import Profile
from .errors import EXIT_CONFLICT, EXIT_OK, StateError, XferError
from .gitcmd import Git, GitResult
from .preflight import git_path, head_sha
from .prefix import Projector
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
    #: Подкаталоги сторон. Живут здесь, а не берутся из профиля на каждом
    #: шаге, ровно по той же причине, что и остальные опции: серия должна
    #: доиграться теми правилами, с которыми начиналась, даже если конфиг
    #: тем временем поправили. `from_json` игнорирует незнакомые ключи,
    #: так что старый state читается без миграции.
    source_prefix: str = ""
    target_prefix: str = ""

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


def _pick_args(
    git: Git,
    rev: str,
    options: Options,
    is_merge: bool,
    *,
    origin: str = "",
    projected: bool = False,
) -> tuple[list[str], list[str], dict[str, str]]:
    """Аргументы cherry-pick. `rev` — что применяем, `origin` — откуда родом.

    Для обычного переноса это одно и то же; у проекции `rev` синтетический,
    и всё, что читается из коммита-источника, берётся по `origin`.
    """
    origin = origin or rev
    args = ["cherry-pick"]
    if options.trailer and not projected:
        # У проекции трейлер уже в сообщении, и он указывает на оригинал;
        # -x вписал бы сюда sha синтетического коммита.
        args.append("-x")
    args.append(f"--empty={options.empty}")
    args.append("--gpg-sign" if options.gpg_sign else "--no-gpg-sign")
    if is_merge and not projected:
        # У проекции родитель всегда один: дифф уже посчитан относительно
        # первого родителя, и -m 1 git отверг бы как «это не merge».
        args += ["-m", "1"]
    args += ["--", rev]

    config: list[str] = []
    if not options.hooks:
        # Переносится уже проверенный коммит; падающий pre-commit не должен рвать серию.
        config.append("core.hooksPath=/dev/null")

    env: dict[str, str] = {}
    if options.keep_committer_date:
        env["GIT_COMMITTER_DATE"] = git.out("show", "-s", "--format=%cI", origin)
    return args, config, env


def is_merge_commit(git: Git, sha: str) -> bool:
    parents = git.out("rev-list", "--parents", "-n", "1", sha).split()
    return len(parents) > 2


def pick(
    git: Git,
    sha: str,
    options: Options,
    projector: Projector | None = None,
) -> tuple[str, GitResult, str]:
    """Применить один коммит. Возвращает (статус, результат git, новый HEAD)."""
    before = head_sha(git)
    merge = is_merge_commit(git, sha)
    if merge and not options.allow_merges:
        return SKIPPED, GitResult((), 0, "", ""), before or ""
    projected = bool(projector and projector.enabled)
    rev = sha
    if projected:
        built = projector.commit(sha, trailer=options.trailer)
        if built is None:
            # Коммит не задел подкаталог — переносить нечего.
            return EMPTY, GitResult((), 0, "", ""), before or ""
        rev = built
        projector.pin(rev)
    args, config, env = _pick_args(
        git, rev, options, merge, origin=sha, projected=projected
    )
    result = git.run(*args, check=False, config=config, env=env, mutating=True)
    after = head_sha(git) or ""
    if result.ok:
        return (OK if after != before else EMPTY), result, after
    if git_path(git, "CHERRY_PICK_HEAD").exists():
        return CONFLICT, result, after
    return FAILED, result, after


# -- очередь --------------------------------------------------------------


def projector_for(git: Git, profile: Profile, options: Options | None = None) -> Projector:
    """Переписывание префикса; без префиксов — пустышка.

    У начатой серии префиксы берутся из её опций, а не из профиля:
    `continue` после паузы обязан доиграть очередь теми же путями, какими
    начинал, даже если конфиг тем временем поправили. Без опций (сухой
    прогон, у которого серии ещё нет) — из профиля.
    """
    return Projector(
        git,
        source_prefix=options.source_prefix if options else profile.source_prefix,
        target_prefix=options.target_prefix if options else profile.target_prefix,
        pin_ref=profile.pick_ref,
    )


def _drain(
    git: Git,
    state: State,
    progress: Progress,
    options: Options,
    outcome: Outcome,
    report: Reporter,
    projector: Projector,
) -> Outcome:
    """Прокрутить очередь до конца или до первого конфликта."""
    total = progress.total
    while progress.queue:
        sha = progress.queue.pop(0)
        progress.current = sha
        index = len(progress.done) + 1
        subject = git.out("show", "-s", "--format=%s", sha)
        report(f"[{index}/{total}] {sha[:12]} {subject}")
        logbook.info("шаг %d/%d: беру %s %s", index, total, sha, subject)
        status, result, head = pick(git, sha, options, projector)
        logbook.info("шаг %d/%d: %s → %s", index, total, status, head or "HEAD не сдвинулся")
        if status == CONFLICT:
            progress.expected_head = head
            state.in_progress = progress
            state.save()
            outcome.conflict = sha
            outcome.remaining = list(progress.queue)
            logbook.warn(
                "конфликт на %s; в очереди осталось %d", sha, len(progress.queue)
            )
            report("  конфликт — разрешите его и выполните: git-xfer continue")
            report(result.stdout.strip() or result.stderr.strip())
            return outcome
        if status == FAILED:
            progress.queue.insert(0, sha)
            progress.current = None
            state.in_progress = progress
            state.save()
            logbook.error("cherry-pick %s не удался: %s", sha, result.describe())
            raise XferError(f"cherry-pick {sha[:12]} не удался:\n{result.describe()}")
        _record(state, progress, outcome, sha, status, head)
        if status == EMPTY:
            untouched = projector.enabled and (
                # Ответ уже посчитан в pick() и лежит в кэше.
                projector.commit(sha, trailer=options.trailer) is None
            )
            report(
                "  подкаталог не затронут — пропущен"
                if untouched
                else "  пусто после переноса — пропущен"
            )
        elif status == SKIPPED:
            report("  merge-коммит — пропущен (нужен --allow-merges)")
        progress.expected_head = head or progress.expected_head
        state.save()
    progress.current = None
    state.in_progress = None
    state.save()
    projector.unpin()
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
            "есть незавершённый перенос. Закончите его: git-xfer continue / skip / abort"
        )
    head = head_sha(git)
    if not head:
        raise XferError("в целевой ветке нет коммитов")
    # Префиксы направления фиксируем в опциях серии здесь, а не в cli:
    # так их не забудет ни один вызывающий.
    options.source_prefix = profile.source_prefix
    options.target_prefix = profile.target_prefix
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
    logbook.info(
        "серия: %d коммит(ов), профиль %s, HEAD до начала %s",
        len(shas), profile.name, head,
    )
    return _drain(
        git,
        state,
        progress,
        options,
        Outcome(),
        report,
        projector_for(git, profile, options),
    )


def _load_progress(state: State, profile: Profile, report: Reporter = _noop) -> Progress:
    """Незавершённая серия целевого репозитория.

    Имя профиля здесь только для сведения: state привязан к целевому
    репозиторию, очередь лежит в нём же, и доделывать её можно независимо
    от того, как назвали направление в этот раз. Требовать совпадения имени
    нельзя — разовый прогон (`-b feature`) даёт другое имя, и человек
    остался бы посреди cherry-pick без единого способа его закончить.
    """
    progress = state.in_progress
    if not progress:
        raise StateError("незавершённого переноса нет")
    if progress.profile != profile.name:
        report(
            f"  серия начата как {progress.profile!r}, продолжаем её "
            f"(сейчас вызвано как {profile.name!r})"
        )
        logbook.info(
            "продолжаем серию профиля %s под именем %s", progress.profile, profile.name
        )
    return progress


def _drift_error(progress: Progress, head: str) -> StateError:
    return StateError(
        "пока перенос стоял на паузе, HEAD ушёл не туда:\n"
        f"  оставляли {progress.expected_head[:12]}, сейчас {head[:12] or '—'}\n"
        "Гадать, что из этого перенос, мы не будем. Разберитесь руками; "
        "забыть незавершённую серию: git-xfer cleanup --state"
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
            "Разберитесь руками; чтобы забыть незавершённую серию: git-xfer cleanup --state"
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
    progress = _load_progress(state, profile, report)
    options = Options.from_json(progress.opts)
    if (options.source_prefix, options.target_prefix) != (
        profile.source_prefix,
        profile.target_prefix,
    ):
        report(
            "  подкаталоги в конфиге изменились с начала серии; доигрываем "
            f"теми, с которыми начинали: {options.source_prefix or 'корень'} → "
            f"{options.target_prefix or 'корень'}"
        )
        logbook.warn(
            "префиксы серии (%r → %r) разошлись с профилем (%r → %r)",
            options.source_prefix, options.target_prefix,
            profile.source_prefix, profile.target_prefix,
        )
    projector = projector_for(git, profile, options)
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

    return _drain(git, state, progress, options, outcome, report, projector)


def abort(git: Git, profile: Profile, state: State, report: Reporter = _noop) -> Outcome:
    """Откатить только текущий коммит; уже перенесённые остаются на месте."""
    progress = _load_progress(state, profile, report)
    if git_path(git, "CHERRY_PICK_HEAD").exists():
        result = git.run("cherry-pick", "--abort", check=False, mutating=True)
        if not result.ok:
            raise XferError("git cherry-pick --abort не прошёл:\n" + result.describe())
    projector_for(git, profile, Options.from_json(progress.opts)).unpin()
    done = len(progress.done)
    report(
        f"Серия прервана. Перенесённых коммитов оставлено: {done}; "
        f"не применено: {len(progress.queue) + (1 if progress.current else 0)}"
    )
    state.in_progress = None
    state.save()
    return Outcome(results=[])
