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

#: Служебные коммиты git-xfer идут мимо хуков всегда, даже при `--hooks`.
#: `--hooks` означает «прогнать хуки на переносимом коммите», и это делает
#: `cherry-pick`. Amend авторства и схлопывание — не отдельные изменения,
#: а доводка того же самого: `pre-commit`, который `cherry-pick` не зовёт
#: вовсе, порвал бы на них серию, а `post-commit` отработал бы дважды.
NO_HOOKS = ("core.hooksPath=/dev/null",)

#: Как вели себя опции до того, как они стали настраиваемыми. Нужно ровно
#: для одного случая: серия начата прошлой версией, застряла на конфликте,
#: а `continue` зовёт уже новая.
LEGACY_OPTIONS = {"trailer": True, "keep_author": True, "squash": False}

US = "\x1f"  # разделитель полей
RS = "\x1e"  # разделитель записей

#: Статусы шага.
OK = "ok"
EMPTY = "empty"
SKIPPED = "skipped"
CONFLICT = "conflict"
FAILED = "failed"


@dataclass
class Options:
    """Опции применения. Дефолты — как описано в README."""

    #: -x: дописать в сообщение `(cherry picked from commit <sha>)`. Дефолты
    #: тут и у `keep_author` — дефолты продукта, а не «как было»: серия,
    #: начатая версией без этих ключей и застрявшая на конфликте, доиграется
    #: новыми правилами. Обычный путь другой — значения кладёт `cli`
    #: из профиля, и они переживают паузу вместе с остальными опциями.
    trailer: bool = False
    empty: str = "drop"                   # коммит, ставший пустым, — уже перенесённый
    allow_merges: bool = False            # -m 1 схлопывает влитую ветку, почти никогда не то
    hooks: bool = False                   # падающий линтер не должен рвать серию
    gpg_sign: bool = False
    keep_committer_date: bool = False     # ломает предположение git о неубывающих таймстампах
    #: Оставить автором перенесённого коммита автора исходного. По умолчанию
    #: нет: cherry-pick тащит авторство за собой, а при переносе между своими
    #: репозиториями в целевой истории нужен тот, кто переносит. Значение
    #: берётся из профиля (`keep_author` в конфиге) или из флагов
    #: --keep-author / --reset-author; здесь — дефолт продукта, чтобы серия,
    #: начатая ещё до появления ключа, доигралась предсказуемо.
    keep_author: bool = False
    #: Схлопнуть всю серию в один коммит. Делается не отдельным механизмом,
    #: а поверх обычного: очередь проигрывается как всегда — с конфликтами,
    #: паузой и `continue` — и только в самом конце получившиеся коммиты
    #: сворачиваются в один. Поэтому squash ничего не меняет ни в разборе
    #: конфликтов, ни в проекции префикса.
    squash: bool = False
    #: Сообщение схлопнутого коммита. Пусто — склеим из сообщений серии.
    message: str = ""
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
        """Опции начатой серии из state.

        Ключа в записи нет — значит state писала версия, где его ещё не
        существовало, и серия начиналась с тогдашним поведением. Дефолт
        класса тут не годится: он описывает то, чего эта серия не знала,
        и половина её коммитов доигралась бы по другим правилам, чем
        первая.
        """
        data = data or {}
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        for field_name, legacy in LEGACY_OPTIONS.items():
            known.setdefault(field_name, legacy)
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
    #: Итоговый коммит, если серию схлопнули, и сколько коммитов в него вошло.
    squashed: str = ""
    squashed_count: int = 0

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

    return args, config, _committer_env(git, options, origin)


def _committer_env(git: Git, options: Options, sha: str) -> dict[str, str]:
    """Окружение коммита: только --keep-committer-date и только он."""
    if not options.keep_committer_date:
        return {}
    return {"GIT_COMMITTER_DATE": git.out("show", "-s", "--format=%cI", sha)}


def reset_author(git: Git, options: Options, env: dict[str, str] | None = None) -> str:
    """Переписать авторство последнего коммита на того, кто переносит.

    `cherry-pick` своего `--reset-author` не имеет: авторство он всегда берёт
    из исходного коммита. Поэтому сразу после коммита правим его `--amend`,
    пока он ещё вершина ветки и никто его не видел. `--allow-empty` нужен
    из-за `--empty=keep`, `--no-edit` — чтобы не открылся редактор.
    Возвращает новый HEAD.
    """
    args = ["commit", "--amend", "--reset-author", "--no-edit", "--allow-empty"]
    args.append("--gpg-sign" if options.gpg_sign else "--no-gpg-sign")
    result = git.run(
        *args, check=False, config=NO_HOOKS, env=env or {}, mutating=True
    )
    if not result.ok:
        raise XferError(
            "не удалось переписать авторство перенесённого коммита:\n"
            + result.describe()
        )
    return head_sha(git) or ""


def _author_env(git: Git, sha: str) -> dict[str, str]:
    """Авторство коммита `sha` как окружение для нового коммита."""
    name, email, date = git.out("show", "-s", f"--format=%an{US}%ae{US}%aI", sha).split(US)
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": date,
    }


def collected_message(git: Git, base: str) -> str:
    """Сообщения коммитов `base..HEAD` подряд, от старого к новому.

    Берём их у уже применённых коммитов, а не у оригиналов: в них есть
    и трейлер (если он включён), и правки, которые человек внёс, разрешая
    конфликт, — то есть ровно то, что и должно приехать в итоговый коммит.
    """
    raw = git.out("log", "--reverse", f"--format=%B{RS}", f"{base}..HEAD", "--")
    parts = [part.strip() for part in raw.split(RS)]
    return "\n\n".join(part for part in parts if part)


def retitle(git: Git, options: Options, message: str) -> str:
    """Переписать сообщение последнего коммита, не трогая всё остальное."""
    args = ["commit", "--amend", "--allow-empty", "-F", "-"]
    args.append("--gpg-sign" if options.gpg_sign else "--no-gpg-sign")
    result = git.run(
        *args, check=False, config=NO_HOOKS, stdin=message + "\n", mutating=True
    )
    if not result.ok:
        raise XferError(
            "не удалось записать сообщение из --message:\n" + result.describe()
        )
    return head_sha(git) or ""


def collapse(
    git: Git,
    state: State,
    progress: Progress,
    options: Options,
    outcome: Outcome,
    report: Reporter,
) -> None:
    """Свернуть уже применённую серию в один коммит.

    `reset --soft` к тому HEAD, с которого серия начиналась: дерево и индекс
    остаются с результатом всей серии, а история схлопывается. Так squash
    не нужно объяснять ни очереди, ни конфликту, ни проекции — они уже
    отработали.
    """
    picked = [step["src"] for step in progress.done if step["status"] == OK]
    base = progress.head_before
    head = head_sha(git) or ""
    if len(picked) < 2 or not base or head == base:
        # Схлопывать нечего: коммит один или серия не дала ни одного.
        # Сообщение при этом просили не для схлопывания, а для того, что
        # приедет, — молча потерять его нельзя.
        if len(picked) == 1 and options.message.strip() and head != base:
            retitle(git, options, options.message.strip())
            report(
                "  схлопывать было нечего — приехал один коммит; "
                "сообщение из --message на нём"
            )
        return
    env = _committer_env(git, options, picked[-1])
    if options.keep_author:
        # Автор — тот, с кого серия начиналась: остальные его сообщения
        # всё равно уехали в общий текст.
        env.update(_author_env(git, picked[0]))
    message = options.message.strip() or collected_message(git, base)
    args = ["commit", "--allow-empty", "-F", "-"]
    args.append("--gpg-sign" if options.gpg_sign else "--no-gpg-sign")
    git.run("reset", "--soft", base, mutating=True)
    try:
        # Между `reset` и `commit` ветка стоит отмотанной на начало серии:
        # коммитов на ней уже нет, а нового ещё нет. Любой выход отсюда —
        # ошибка, таймаут, Ctrl+C — обязан вернуть её обратно, иначе серия
        # окажется потерянной для всех, кроме reflog.
        result = git.run(
            *args, check=False, config=NO_HOOKS, env=env, stdin=message + "\n", mutating=True
        )
        if not result.ok:
            raise XferError(
                "не удалось схлопнуть серию в один коммит:\n" + result.describe()
            )
    except BaseException:
        # `reset --soft` не трогал ни дерево, ни индекс, поэтому обратный
        # `--soft` возвращает ровно то, что было.
        git.run("reset", "--soft", head, check=False, mutating=True)
        logbook.error("схлопывание не удалось, ветка возвращена на %s", head)
        raise
    squashed = head_sha(git) or ""
    # Маппинг обязан указывать на коммит, который и правда есть в истории:
    # промежуточных больше нет, и без этого дедупликация решила бы, что
    # ничего не переносилось.
    for sha in picked:
        state.remember(sha, squashed)
    progress.expected_head = squashed
    for step in progress.done:
        if step["status"] == OK:
            step["dst"] = squashed
    for step_result in outcome.results:
        if step_result.status == OK:
            step_result.dst = squashed
    outcome.squashed = squashed
    outcome.squashed_count = len(picked)
    logbook.info("схлопнуто %d коммит(ов) в %s", len(picked), squashed)
    report(f"  {len(picked)} коммит(ов) схлопнуты в один: {squashed[:12]}")


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
        if after != before:
            if not options.keep_author:
                # env тот же: --keep-committer-date должен пережить amend,
                # иначе committer date стал бы временем переписывания.
                after = reset_author(git, options, env) or after
            return OK, result, after
        return EMPTY, result, after
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
        try:
            status, result, head = pick(git, sha, options, projector)
        except XferError:
            # Упасть можно и после того, как коммит уже создан: на amend
            # авторства или на таймауте. Тогда шаг состоялся, и state обязан
            # это знать — иначе `continue` увидит уехавший HEAD и откажется
            # работать, а повторный `apply` продублирует коммит.
            _record_interrupted(git, state, progress, outcome, sha)
            raise
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
    if options.squash:
        collapse(git, state, progress, options, outcome, report)
    state.in_progress = None
    state.save()
    projector.unpin()
    return outcome


def _record_interrupted(
    git: Git, state: State, progress: Progress, outcome: Outcome, sha: str
) -> None:
    """Сохранить состояние шага, оборвавшегося исключением.

    Смотрим по HEAD, успел ли git закоммитить: если да — шаг состоялся,
    и упало то, что идёт следом; если нет — коммит возвращается в очередь.
    В обоих случаях state должен описывать репозиторий как он есть, чтобы
    `continue` продолжил, а не упёрся в «HEAD не там, где мы его оставили».
    """
    head = head_sha(git) or ""
    if head and head != progress.expected_head:
        _record(state, progress, outcome, sha, OK, head)
        progress.expected_head = head
    else:
        progress.queue.insert(0, sha)
        progress.current = None
    state.in_progress = progress
    state.save()



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
            result = git.run(
                "cherry-pick",
                "--continue",
                check=False,
                env=_committer_env(git, options, current),
                mutating=True,
            )
            if not result.ok:
                raise XferError(
                    "git cherry-pick --continue не прошёл — конфликт ещё не разрешён:\n"
                    + result.describe()
                )
            head = head_sha(git) or ""
            status = OK if head != progress.expected_head else EMPTY
            if status == OK and not options.keep_author:
                # Коммит разрешённого конфликта делает git, авторство он
                # тянет из исходного так же, как на обычном шаге.
                head = reset_author(git, options, _committer_env(git, options, current)) or head
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
                # Авторство всё равно наше дело: git, коммитя разрешённый
                # конфликт, сохраняет автора оригинала, и без этого шага
                # один коммит серии молча выбился бы из остальных.
                if not options.keep_author:
                    head = reset_author(
                        git, options, _committer_env(git, options, current)
                    ) or head
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
