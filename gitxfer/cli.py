"""Разбор аргументов и подкоманды."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # запустили файлом, а не как часть пакета
    import sys as _sys

    _sys.exit(
        "git-xfer: этот файл — часть пакета, отдельно он не запускается.\n"
        "  из клона:       python3 -m gitxfer ...\n"
        "  или лаунчером:  ./bin/git-xfer ...\n"
        "  после установки: git-xfer ..."
    )

from . import __version__, logbook
from .config import (
    DEFAULT_DEDUP_WINDOW,
    DEFAULT_SQUASH,
    DEFAULT_GIT_TIMEOUT,
    DEFAULT_KEEP_AUTHOR,
    DEFAULT_TRAILER,
    DEFAULT_RESOLVE,
    DEFAULT_PATCHID_WINDOW,
    DEFAULT_SCAN_LIMIT,
    Profile,
    adhoc_profile,
    config_path,
    load_config,
    log_settings,
    normalize_prefix,
    write_template,
)
from .discover import (
    NEW,
    US,
    sanitize,
    SIMILAR,
    TRANSFERRED,
    Commit,
    Row,
    Survey,
    apply_order,
    has_ref,
    survey,
    sync,
)
from .errors import (
    EXIT_GIT,
    EXIT_INTERRUPT,
    ConfigError,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_USAGE,
    XferError,
)
from .gitcmd import Git
from .plan import dry_run
from .prefix import touches_outside
from .preflight import head_sha, run_preflight, stale_xfer_refs
from .picker import choose, legend, parse_selection, render_rows, require_tty
from .state import State, state_path
from .transfer import Options, abort, projector_for, resume, start


def eprint(text: str = "") -> None:
    print(text, file=sys.stderr)


# -- контекст выполнения --------------------------------------------------


def _pick(title: str, options: list[tuple[str, str]], default: int = 1) -> int:
    """Показать пронумерованный список и вернуть выбранный номер (с единицы)."""
    print(title)
    for number, (label, detail) in enumerate(options, start=1):
        print(f"  {number}. {label}")
        if detail:
            print(f"     {detail}")
    while True:
        try:
            answer = input(f"  выбор [{default}]: ").strip()
        except EOFError:
            raise KeyboardInterrupt from None
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        print(f"    нужно число от 1 до {len(options)}")


def _choose_pair(config, args: argparse.Namespace):
    """Профиль: из -p, единственный в конфиге, или спросить."""
    if args.profile:
        return config.pair(args.profile)
    names = sorted(config.profiles)
    if len(names) == 1:
        return config.profiles[names[0]]
    if not sys.stdin.isatty():
        raise ConfigError(
            "не указан профиль (-p/--profile). Доступны: " + ", ".join(names)
        )
    options = []
    for name in names:
        pair = config.profiles[name]
        branches = (
            pair.a.branch
            if pair.a.branch == pair.b.branch
            else f"{pair.a.branch} / {pair.b.branch}"
        )
        options.append((name, f"{pair.a.path} ↔ {pair.b.path}  ({branches})"))
    return config.profiles[names[_pick("Профиль:", options) - 1]]


def _choose_direction(pair, args: argparse.Namespace) -> str:
    """Сторона, в которую переносим: из --to, из старого формата, или спросить."""
    if args.to:
        return args.to
    if pair.implied:
        return pair.implied
    if not sys.stdin.isatty():
        raise XferError(
            f"профиль {pair.name!r} описывает пару репозиториев — укажите "
            "направление: --to a или --to b"
        )
    options = [
        (f"{pair.a.path} ({pair.a.branch})  →  {pair.b.path} ({pair.b.branch})", ""),
        (f"{pair.b.path} ({pair.b.branch})  →  {pair.a.path} ({pair.a.branch})", ""),
    ]
    return "b" if _pick("Куда переносим:", options) == 1 else "a"


def _current_branch(path: Path | None) -> str | None:
    """Какая ветка сейчас выгружена — годится как подсказка в опросе."""
    if not path or not Path(path).expanduser().exists():
        return None
    # Конфига на этом шаге ещё нет (его как раз собираем вопросами),
    # поэтому потолок дефолтный — для одной `symbolic-ref` этого с запасом.
    probe = Git(Path(path).expanduser())
    return probe.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False).text or None


def _ask(label: str, default: str | None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"  {label}{suffix}: ").strip()
        except EOFError:
            raise KeyboardInterrupt from None
        if answer:
            return answer
        if default:
            return default
        print("    нужно значение")


def _ask_setup(
    source: Path | None, source_branch: str | None, target: Path | None, target_branch: str | None
) -> tuple[Path, str, Path, str]:
    """Спросить направление целиком. Ветка цели по умолчанию — та же, что у источника."""
    print("Откуда и куда переносим:")
    source = Path(_ask("репозиторий-источник", str(source) if source else None)).expanduser()
    source_branch = _ask("ветка источника", source_branch or _current_branch(source))
    target = Path(_ask("целевой репозиторий", str(target) if target else None)).expanduser()
    target_branch = _ask(
        "ветка цели", target_branch or _current_branch(target) or source_branch
    )
    return source, source_branch, target, target_branch


def _load_config_if_present(path: Path | None):
    """Конфиг не обязателен, но сломанный конфиг — это ошибка, а не пустое место.

    Иначе опечатка в профилях молча уводила бы прогон в ad-hoc, и человек
    никогда бы не узнал, что его профили не читаются.
    """
    target = Path(path).expanduser() if path else config_path()
    if not target.exists():
        return None
    return load_config(target)


def resolve_profile(args: argparse.Namespace) -> tuple[Profile, object | None]:
    """Собрать направление из конфига, флагов и — если надо — вопросов.

    Профиль в конфиге описывает ПАРУ репозиториев; направление выбирается
    здесь: `--to b` тащит из a в b, `--to a` — обратно.
    """
    base: Profile | None = None
    config = None
    # Оба пути заданы явно — конфиг не нужен вовсе, и спрашивать нечего.
    adhoc = bool(getattr(args, "source", None) and getattr(args, "target", None))
    # --ask без профиля: человек сейчас сам назовёт и репозитории, и ветки.
    ask_all = args.ask and not args.profile
    if not (adhoc or ask_all):
        config = load_config(args.config)
        pair = _choose_pair(config, args)
        base = pair.direction(_choose_direction(pair, args))
    elif not args.profile:
        # Конфиг здесь необязателен, но если он есть и сломан — молчать нельзя.
        config = _load_config_if_present(args.config)

    source = args.source or (base.source if base else None)
    target = args.target or (base.target if base else None)
    # Одна ветка на обе стороны — самый частый разовый случай.
    src_branch = args.source_branch or args.branch
    dst_branch = args.target_branch or args.branch
    # Профиль знает свои ветки, и флаг про одну сторону не должен трогать
    # вторую: «-p t --target-branch stable» иначе молча увёл бы и источник.
    src_branch = src_branch or (base.source_branch if base else None)
    dst_branch = dst_branch or (base.target_branch if base else None)
    # А вот когда про вторую сторону не знает никто — она называется так же.
    src_branch = src_branch or dst_branch
    dst_branch = dst_branch or src_branch
    # Префикс «как у второй стороны» не подставляем никогда: подкаталог —
    # свойство конкретного репозитория, и угадывать его нечем.
    src_prefix = normalize_prefix(
        args.source_prefix if args.source_prefix is not None else
        (base.source_prefix if base else ""),
        "--source-prefix",
    )
    dst_prefix = normalize_prefix(
        args.target_prefix if args.target_prefix is not None else
        (base.target_prefix if base else ""),
        "--target-prefix",
    )

    if args.ask or not (source and target and src_branch and dst_branch):
        if not sys.stdin.isatty():
            raise XferError(
                "не хватает данных о направлении, а stdin не терминал. "
                "Задайте профиль (-p) или --source/--target и --branch"
            )
        source, src_branch, target, dst_branch = _ask_setup(
            source, src_branch, target, dst_branch
        )

    base_name = base.name if base else "adhoc"
    changed = base is None or (
        Path(source) != base.source
        or Path(target) != base.target
        or src_branch != base.source_branch
        or dst_branch != base.target_branch
        or src_prefix != base.source_prefix
        or dst_prefix != base.target_prefix
    )
    profile = adhoc_profile(
        name=base_name if not changed else f"{base_name}@{src_branch}",
        source=Path(source),
        source_branch=src_branch,
        target=Path(target),
        target_branch=dst_branch,
        scan_limit=base.scan_limit if base else DEFAULT_SCAN_LIMIT,
        dedup_window=base.dedup_window if base else DEFAULT_DEDUP_WINDOW,
        patchid_window=base.patchid_window if base else DEFAULT_PATCHID_WINDOW,
        source_prefix=src_prefix,
        target_prefix=dst_prefix,
        source_key=base.source_key if base else "a",
        target_key=base.target_key if base else "b",
        keep_author=base.keep_author if base else DEFAULT_KEEP_AUTHOR,
        trailer=base.trailer if base else DEFAULT_TRAILER,
        squash=base.squash if base else DEFAULT_SQUASH,
    )
    return profile, config


def _side_line(path: Path, branch: str, prefix: str) -> str:
    where = f"{path} ({branch})"
    return f"{where}, подкаталог {prefix}/" if prefix else where


def describe(profile: Profile) -> str:
    return (
        "Направление: "
        + _side_line(profile.source, profile.source_branch, profile.source_prefix)
        + "\n          →  "
        + _side_line(profile.target, profile.target_branch, profile.target_prefix)
    )


class Context:
    """Направление + пара Git-обёрток + state: всё, что нужно подкоманде."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.profile, self.config = resolve_profile(args)
        logbook.info("направление: %s", self.profile.describe_short())
        # Без конфига (разовый прогон) действует дефолт самой обёртки.
        timeout = self.config.git_timeout if self.config else DEFAULT_GIT_TIMEOUT
        self.target = Git(
            self.profile.target, dry_run=args.dry_run, verbose=args.verbose, timeout=timeout
        )
        self.source = Git(
            self.profile.source, dry_run=args.dry_run, verbose=args.verbose, timeout=timeout
        )
        self.state = State.load(self.profile.target)
        self.args = args

    def ensure_ref(self) -> None:
        """Объекты источника должны быть в целевом репо."""
        if has_ref(self.target, self.profile.ref):
            return
        if self.args.dry_run:
            # Подтянуть их молча нельзя — это запись в репозиторий.
            raise XferError(
                f"объектов источника нет ({self.profile.ref}), а --dry-run не даёт их "
                f"подтянуть. Выполните сначала: git-xfer sync -p {self.profile.name}"
            )
        eprint(f"Объекты источника ещё не перенесены, выполняю sync ({self.profile.ref})")
        sync(self.target, self.profile)
        if not has_ref(self.target, self.profile.ref):
            raise XferError(f"не удалось создать {self.profile.ref}")

    def survey(self, *, limit: int | None = None, use_patch_id: bool = True) -> Survey:
        result = survey(
            self.target,
            self.profile,
            self.state,
            limit=limit,
            use_patch_id=use_patch_id,
            allow_merges=getattr(self.args, "allow_merges", False),
        )
        self.state.trim_patchid_cache(result.hot_shas)
        self.state.save()
        return result


# -- выбор коммитов -------------------------------------------------------


def resolve_rows(context: Context, view: Survey) -> list[Row]:
    """Из аргументов или интерактива — набор строк таблицы."""
    args = context.args
    if getattr(args, "sha", None):
        return rows_by_sha(context, view, args.sha)
    if getattr(args, "commits", None):
        numbers = parse_selection(args.commits, len(view.rows))
        return [view.rows[number - 1] for number in numbers]
    if getattr(args, "interactive", False):
        require_tty()
        return choose(view.rows)
    if sys.stdin.isatty():
        return choose(view.rows)
    raise XferError(
        "нечего выбирать: stdin не терминал. Укажите --commits 1,3,5-7 или --sha <hash>..."
    )


def rows_by_sha(context: Context, view: Survey, shas: list[str]) -> list[Row]:
    known = {row.commit.sha: row for row in view.rows}
    result: list[Row] = []
    for raw in shas:
        full = context.target.run(
            "rev-parse", "--verify", "--quiet", f"{raw}^{{commit}}", check=False
        ).text
        if not full:
            raise XferError(f"{raw!r} — не коммит в целевом репозитории (сделайте sync)")
        if not context.target.ok("merge-base", "--is-ancestor", full, context.profile.ref):
            raise XferError(f"{raw!r} не принадлежит ветке источника {context.profile.ref}")
        row = known.get(full)
        if row is None:
            # Строки нет в таблице: коммит либо вне окна --limit, либо —
            # при переносе с префиксом — вне подкаталога. Поля дочитываем,
            # иначе в «порядке применения» окажется хеш без заголовка.
            # split(US, 2): заголовок идёт последним и сам может содержать
            # разделитель — распаковка «ровно в три» дала бы трейсбек.
            date, author, subject = context.target.out(
                "show", "-s", "--date=short", f"--format=%ad{US}%an{US}%s", full
            ).split(US, 2)
            partial = touches_outside(
                context.target, full, context.profile.source_prefix
            )
            outside = bool(context.profile.source_prefix) and not context.target.lines(
                "log", "--no-walk", "--format=%H", full, "--",
                context.profile.source_prefix,
            )
            commit = Commit(
                sha=full,
                short=full[:12],
                date=date,
                author=sanitize(author),
                subject=sanitize(subject),
            )
            row = Row(
                number=0,
                commit=commit,
                status=NEW,
                reason=(
                    f"вне подкаталога {context.profile.source_prefix}/"
                    if outside
                    else "вне окна --limit"
                ),
                partial=partial and not outside,
            )
        result.append(row)
    return result


def confirm(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise XferError("нужно подтверждение, но stdin не терминал. Добавьте --yes")
    try:
        answer = input(f"{question} [y/N]: ").strip().lower()
    except EOFError:
        # Ctrl-D в вопросе «делать?» — это «нет», а не трейсбек.
        print()
        return False
    return answer in ("y", "yes", "д", "да")


def print_order(context: Context, commits: list[Commit]) -> list[Commit]:
    """Показать фактический порядок применения old→new."""
    ordered_shas = apply_order(context.target, [commit.sha for commit in commits])
    index = {commit.sha: commit for commit in commits}
    ordered = [index[sha] for sha in ordered_shas]
    print()
    print(f"Порядок применения (old → new), коммитов: {len(ordered)}")
    for position, commit in enumerate(ordered, start=1):
        print(f"  {position:>3}. {commit.short} {commit.subject}")
    return ordered


# -- подкоманды -----------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path, created = write_template(args.config, force=args.force)
    if created:
        print(f"Создан шаблон конфига: {path}")
        print("Опишите профили и запустите: git-xfer doctor -p <профиль>")
    else:
        print(f"Конфиг уже существует: {path} (перезаписать — init --force)")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    context = Context(args)
    profile = context.profile
    print(f"Профиль: {profile.name}")
    print(describe(profile))
    report = run_preflight(context.target, context.source, profile)
    print(report.render())
    if context.state.in_progress:
        progress = context.state.in_progress
        print(
            f"  ! незавершённый перенос: сделано {len(progress.done)}, "
            f"осталось {len(progress.queue) + (1 if progress.current else 0)}"
        )
    print()
    if report.failures:
        print(f"Не готово: блокирующих проблем {len(report.failures)}")
        return EXIT_PREFLIGHT
    print(f"Готово к переносу. Предупреждений: {len(report.warnings)}")
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    context = Context(args)
    profile = context.profile
    before = context.target.run("rev-parse", "--verify", "--quiet", profile.ref, check=False).text
    sha = sync(context.target, profile)
    if args.dry_run:
        return EXIT_OK
    if before == sha:
        print(f"{profile.ref} без изменений: {sha[:12]}")
    else:
        print(f"{profile.ref}: {before[:12] or '—'} → {sha[:12]}")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    context = Context(args)
    context.ensure_ref()
    view = context.survey(limit=args.limit, use_patch_id=not args.no_patch_id)
    rows = view.rows
    if args.new:
        rows = [row for row in rows if row.status == NEW]
    if not rows:
        print("Коммитов не найдено")
        return EXIT_OK
    for line in render_rows(rows):
        print(line)
    print()
    print(f"  {legend(view.rows)}")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    context = Context(args)
    context.ensure_ref()
    view = context.survey(limit=args.limit, use_patch_id=not args.no_patch_id)
    rows = resolve_rows(context, view)
    if not rows:
        print("Ничего не выбрано")
        return EXIT_OK
    ordered = print_order(context, [row.commit for row in rows])
    head = head_sha(context.target)
    if not head:
        raise XferError("в целевой ветке нет коммитов")
    result = dry_run(
        context.target, head, ordered, projector_for(context.target, context.profile)
    )
    print()
    print("Сухой прогон (рабочее дерево не тронуто):")
    for position, step in enumerate(result.steps, start=1):
        if step.conflicts:
            print(f"  {position:>3}. ✗ {step.commit.short} {step.commit.subject}")
            for path in step.conflicts:
                print(f"        конфликт: {path}")
        elif step.empty:
            print(f"  {position:>3}. ∅ {step.commit.short} {step.commit.subject} — станет пустым")
        else:
            print(f"  {position:>3}. ✓ {step.commit.short} {step.commit.subject}")
    print()
    conflicted = result.conflicted
    if conflicted:
        print(
            f"Конфликтов ожидается: {len(conflicted)} из {len(result.steps)}. "
            "После первого конфликта предсказание приблизительно."
        )
    else:
        print(f"Конфликтов не ожидается, коммитов: {len(result.steps)}")
    return EXIT_OK


def _options(args: argparse.Namespace, profile: Profile) -> Options:
    """Опции серии: конфиг профиля, поверх него — флаги этого вызова."""
    keep_author = args.keep_author
    squash = profile.squash if args.squash is None else args.squash
    message = (args.message or "").strip()
    if message and not squash:
        raise XferError(
            "--message задаёт сообщение схлопнутого коммита и без --squash "
            "ничего не значит: сообщения переносимых коммитов не переписываются"
        )
    return Options(
        trailer=profile.trailer if args.trailer is None else args.trailer,
        empty=args.empty,
        allow_merges=args.allow_merges,
        hooks=args.hooks,
        gpg_sign=args.gpg_sign,
        keep_committer_date=args.keep_committer_date,
        keep_author=profile.keep_author if keep_author is None else keep_author,
        squash=squash,
        message=message,
    )


def cmd_apply(args: argparse.Namespace) -> int:
    if args.dry_run:
        # Под --dry-run cherry-pick не выполняется, HEAD не двигается,
        # и каждый коммит выглядел бы пустым. Сухой прогон — это plan.
        raise XferError("для сухого прогона есть отдельная подкоманда: git-xfer plan")
    context = Context(args)
    # Проверки раньше fetch: незачем тащить объекты в репозиторий,
    # который мы тут же признаем непригодным.
    report = run_preflight(context.target, context.source, context.profile)
    for check in report.warnings:
        eprint(check.render())
    report.raise_if_failed()
    context.ensure_ref()

    view = context.survey(limit=args.limit, use_patch_id=not args.no_patch_id)
    rows = resolve_rows(context, view)
    if not rows:
        print("Ничего не выбрано")
        return EXIT_OK
    marked = [row for row in rows if row.status != NEW]
    if marked:
        print()
        print("Внимание, среди выбранных есть уже перенесённые:")
        for row in marked:
            print(f"  {row.status} {row.commit.short} {row.commit.subject} — {row.reason}")
    ordered = print_order(context, [row.commit for row in rows])
    options = _options(args, context.profile)
    question = (
        f"Перенести {len(ordered)} коммит(ов) ОДНИМ коммитом?"
        if options.squash and len(ordered) > 1
        else f"Перенести {len(ordered)} коммит(ов)?"
    )
    if not confirm(question, assume_yes=args.yes):
        print("Отменено")
        return EXIT_OK
    print()
    outcome = start(
        context.target,
        context.profile,
        context.state,
        [commit.sha for commit in ordered],
        options,
        report=print,
    )
    return _summary(outcome)


def _summary(outcome) -> int:
    print()
    print(
        f"Перенесено: {outcome.count('ok')}; "
        f"пусто: {outcome.count('empty')}; пропущено: {outcome.count('skipped')}"
    )
    if outcome.squashed:
        print(
            f"Схлопнуто в один коммит: {outcome.squashed[:12]} "
            f"({outcome.squashed_count} коммит(ов))"
        )
    if outcome.conflict:
        print(f"Остановлено на {outcome.conflict[:12]}, в очереди ещё {len(outcome.remaining)}")
        print("Дальше: git-xfer continue | skip | abort")
    return outcome.exit_code


def cmd_continue(args: argparse.Namespace) -> int:
    context = Context(args)
    outcome = resume(context.target, context.profile, context.state, report=print)
    return _summary(outcome)


def cmd_skip(args: argparse.Namespace) -> int:
    context = Context(args)
    outcome = resume(context.target, context.profile, context.state, skip=True, report=print)
    return _summary(outcome)


def cmd_abort(args: argparse.Namespace) -> int:
    context = Context(args)
    abort(context.target, context.profile, context.state, report=print)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    context = Context(args)
    profile = context.profile
    progress = context.state.in_progress
    print(f"Профиль: {profile.name}")
    print(describe(profile))
    print(f"State:   {state_path(profile.target)}")
    print(f"Журнал:  {logbook.path() or 'выключен'}")
    policy = context.config.resolve_conflicts if context.config else DEFAULT_RESOLVE
    print(f"Конфликты: {policy} (политика для скилла, не для самой утилиты)")
    # У начатой серии режим свой: она доигрывается теми опциями, с которыми
    # начиналась, даже если конфиг тем временем поправили. Показывать здесь
    # конфиг значило бы соврать ровно там, где за ответом и приходят.
    started = Options.from_json(progress.opts) if progress else None
    trailer = started.trailer if started else profile.trailer
    keep_author = started.keep_author if started else profile.keep_author
    squash = started.squash if started else profile.squash
    whose = " (серия начата с ними)" if started else ""
    print(
        "Сообщение: "
        + (
            "+ трейлер (cherry picked from commit ...)"
            if trailer
            else "переносится один в один, трейлера нет"
        )
        + whose
    )
    print(
        "Авторство: "
        + (
            "автор исходного коммита сохраняется (keep_author)"
            if keep_author
            else "автором станет тот, кто переносит"
        )
        + whose
    )
    print(
        "Схлопывание: "
        + ("вся серия в один коммит (squash)" if squash else "коммит в коммит")
        + whose
    )
    if not progress:
        print("Незавершённого переноса нет")
        return EXIT_OK
    head = head_sha(context.target) or "—"
    print(f"Начат: {progress.started_at}, профиль {progress.profile}")
    print(f"  сделано {len(progress.done)}, в очереди {len(progress.queue)}")
    if progress.current:
        print(f"  остановлен на {progress.current[:12]}")
    print(f"  HEAD до начала: {progress.head_before[:12]}")
    print(f"  HEAD ожидаемый: {progress.expected_head[:12]}, фактический: {head[:12]}")
    if head != progress.expected_head:
        print("  ! HEAD не там, где мы его оставили — перенос трогали руками")
        print("    забыть незавершённую серию: git-xfer cleanup --state")
    return EXIT_OK


def cmd_cleanup(args: argparse.Namespace) -> int:
    context = Context(args)
    profile = context.profile
    if context.state.in_progress and not args.state:
        raise XferError(
            "есть незавершённый перенос; сначала continue/skip/abort "
            "или добавьте --state, чтобы забыть его"
        )
    refs = stale_xfer_refs(context.target) if args.all else []
    if not args.all:
        refs = [
            ref
            for ref in (profile.ref, profile.pick_ref)
            if has_ref(context.target, ref)
        ]
    for ref in refs:
        context.target.run("update-ref", "-d", ref, mutating=True)
        if not args.dry_run:
            print(f"Удалён {ref}")
    if not refs:
        print("Ссылок refs/xfer/* не найдено")
    if args.state:
        path = state_path(profile.target)
        if args.dry_run:
            print(f"[dry-run] удалить {path}")
        elif path.exists():
            path.unlink()
            print(f"Удалён state {path}")
        else:
            print("State-файла нет")
    if refs and not args.dry_run:
        print("Объекты источника станут недостижимы; освободит их git gc")
    return EXIT_OK


# -- разбор аргументов ----------------------------------------------------


def _add_repo_flags(parser: argparse.ArgumentParser) -> None:
    """Направление и разовые переопределения — без правки конфига."""
    group = parser.add_argument_group("направление")
    group.add_argument(
        "--to",
        choices=("a", "b"),
        help="в какую сторону пары переносим; без флага спросим",
    )
    group.add_argument(
        "-b", "--branch", metavar="NAME", help="ветка с обеих сторон, разово"
    )
    group.add_argument("--source-branch", metavar="NAME", help="ветка источника")
    group.add_argument(
        "--target-branch", metavar="NAME", help="ветка цели (по умолчанию та же)"
    )
    group.add_argument(
        "--source", type=Path, metavar="PATH", help="репозиторий-источник, мимо конфига"
    )
    group.add_argument(
        "--target", type=Path, metavar="PATH", help="целевой репозиторий, мимо конфига"
    )
    group.add_argument(
        "--source-prefix",
        metavar="DIR",
        help="подкаталог источника, в котором лежит проект",
    )
    group.add_argument(
        "--target-prefix",
        metavar="DIR",
        help="подкаталог цели, в который класть проект",
    )
    group.add_argument(
        "--ask", action="store_true", help="спросить репозитории и ветки интерактивно"
    )


def _add_profile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--profile", help="имя профиля из конфига")
    _add_repo_flags(parser)


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-i", "--interactive", action="store_true", help="выбрать коммиты из списка")
    parser.add_argument("--commits", help="номера из списка: 1,3,5-7 (можно !5 и all)")
    parser.add_argument("--sha", nargs="+", metavar="SHA", help="коммиты источника по хешу")
    parser.add_argument("--limit", type=int, help="сколько коммитов показывать")
    parser.add_argument("--no-patch-id", action="store_true", help="не считать patch-id (быстрее)")
    parser.add_argument(
        "--allow-merges", action="store_true", help="показывать и переносить merge-коммиты"
    )


class Parser(argparse.ArgumentParser):
    """argparse по умолчанию выходит с кодом 2, а он занят предполётной
    проверкой: скрипт должен отличать «репозиторий не готов» от «неверный вызов»."""

    def error(self, message: str):  # noqa: D102
        self.print_usage(sys.stderr)
        eprint(f"{self.prog}: ошибка: {message}")
        raise SystemExit(EXIT_USAGE)


def _add_common(parser: argparse.ArgumentParser, *, after_command: bool) -> None:
    """Флаги, работающие и до подкоманды, и после неё.

    В копии для подкоманды дефолт — SUPPRESS: без него argparse затёр бы
    значение, разобранное головным парсером, и `git-xfer --config X init`
    перестал бы работать.
    """
    hidden = argparse.SUPPRESS
    parser.add_argument(
        "--config",
        type=Path,
        default=hidden if after_command else None,
        help=f"путь к конфигу (по умолчанию {config_path()})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=hidden if after_command else False,
        help="печатать вызовы git",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        default=hidden if after_command else False,
        help="не выполнять команды, меняющие репозиторий",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        default=hidden if after_command else False,
        help="не вести журнал прогона",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=hidden if after_command else None,
        metavar="PATH",
        help="куда писать журнал (по умолчанию рядом со state)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="git-xfer",
        description="Перенос коммитов между несвязанными git-репозиториями.",
    )
    parser.add_argument("--version", action="version", version=f"git-xfer {__version__}")
    _add_common(parser, after_command=False)
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, after_command=True)
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    init = subparsers.add_parser("init", help="создать шаблон конфига", parents=[common])
    init.add_argument("--force", action="store_true", help="перезаписать существующий")
    init.set_defaults(func=cmd_init)

    doctor = subparsers.add_parser("doctor", help="предполётные проверки", parents=[common])
    _add_profile(doctor)
    doctor.set_defaults(func=cmd_doctor)

    sync_cmd = subparsers.add_parser("sync", help="перенести объекты source → target", parents=[common])
    _add_profile(sync_cmd)
    sync_cmd.set_defaults(func=cmd_sync)

    list_cmd = subparsers.add_parser("list", help="таблица коммитов источника", parents=[common])
    _add_profile(list_cmd)
    list_cmd.add_argument("--limit", type=int, help="сколько коммитов показывать")
    list_cmd.add_argument("--new", action="store_true", help="только непереносившиеся")
    list_cmd.add_argument("--no-patch-id", action="store_true", help="не считать patch-id")
    list_cmd.add_argument(
        "--allow-merges", action="store_true", help="показывать и merge-коммиты"
    )
    list_cmd.set_defaults(func=cmd_list)

    plan_cmd = subparsers.add_parser("plan", help="сухой прогон: где будут конфликты", parents=[common])
    _add_profile(plan_cmd)
    _add_selection(plan_cmd)
    plan_cmd.set_defaults(func=cmd_plan)

    apply_cmd = subparsers.add_parser("apply", help="перенести выбранные коммиты", parents=[common])
    _add_profile(apply_cmd)
    _add_selection(apply_cmd)
    apply_cmd.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    trailer = apply_cmd.add_mutually_exclusive_group()
    trailer.add_argument(
        "--trailer", dest="trailer", action="store_true", default=None,
        help="дописать (cherry picked from commit ...) — надёжная дедупликация",
    )
    trailer.add_argument(
        "--no-trailer", dest="trailer", action="store_false",
        help="не трогать сообщение коммита (по умолчанию)",
    )
    apply_cmd.add_argument(
        "--empty", choices=("drop", "keep", "stop"), default="drop",
        help="что делать с коммитом, ставшим пустым",
    )
    apply_cmd.add_argument("--hooks", action="store_true", help="не отключать хуки")
    apply_cmd.add_argument("--gpg-sign", action="store_true", help="подписывать коммиты")
    apply_cmd.add_argument(
        "--keep-committer-date", action="store_true", help="сохранить committer date источника"
    )
    squash = apply_cmd.add_mutually_exclusive_group()
    squash.add_argument(
        "--squash", dest="squash", action="store_true", default=None,
        help="схлопнуть выбранные коммиты в один",
    )
    squash.add_argument(
        "--no-squash", dest="squash", action="store_false",
        help="перенести коммит в коммит (по умолчанию)",
    )
    apply_cmd.add_argument(
        "--message", metavar="TEXT",
        help="сообщение схлопнутого коммита; без него — сообщения серии подряд",
    )
    author = apply_cmd.add_mutually_exclusive_group()
    author.add_argument(
        "--keep-author", dest="keep_author", action="store_true", default=None,
        help="оставить автором автора исходного коммита",
    )
    author.add_argument(
        "--reset-author", dest="keep_author", action="store_false",
        help="автор — тот, кто переносит (по умолчанию)",
    )
    apply_cmd.set_defaults(func=cmd_apply)

    cont = subparsers.add_parser("continue", help="продолжить после разрешения конфликта", parents=[common])
    _add_profile(cont)
    cont.set_defaults(func=cmd_continue)

    skip_cmd = subparsers.add_parser("skip", help="пропустить конфликтный коммит", parents=[common])
    _add_profile(skip_cmd)
    skip_cmd.set_defaults(func=cmd_skip)

    abort_cmd = subparsers.add_parser("abort", help="прервать серию, откатив текущий коммит", parents=[common])
    _add_profile(abort_cmd)
    abort_cmd.set_defaults(func=cmd_abort)

    status_cmd = subparsers.add_parser("status", help="состояние незавершённого переноса", parents=[common])
    _add_profile(status_cmd)
    status_cmd.set_defaults(func=cmd_status)

    cleanup = subparsers.add_parser("cleanup", help="убрать refs/xfer/* и state", parents=[common])
    _add_profile(cleanup)
    cleanup.add_argument("--all", action="store_true", help="все refs/xfer/*, не только профиля")
    cleanup.add_argument("--state", action="store_true", help="удалить и state-файл")
    cleanup.set_defaults(func=cmd_cleanup)

    return parser


def _log_hint() -> str:
    path = logbook.path()
    return f"\nПодробности прогона: {path}" if path else ""


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Настройки журнала читаем до его открытия: иначе в выключенный
    # журнал успела бы попасть шапка прогона.
    try:
        from_config, config_file = log_settings(args.config)
        logbook.setup(
            enabled=not args.no_log and (from_config or bool(args.log_file)),
            file=args.log_file or config_file,
        )
        logbook.run_header(list(argv if argv is not None else sys.argv[1:]), __version__)
    except OSError as exc:
        # Журнал — удобство. Что бы с ним ни случилось, работу это
        # останавливать не должно.
        logbook.disable()
        eprint(f"git-xfer: журнал не ведётся ({exc})")
    code = EXIT_OK
    try:
        code = args.func(args)
        return code
    except XferError as exc:
        logbook.error("%s", exc, exc_info=True)
        eprint(f"git-xfer: {exc}{_log_hint()}")
        code = exc.exit_code
        return code
    except KeyboardInterrupt:
        logbook.warn("прервано с клавиатуры")
        eprint()
        eprint("git-xfer: прервано")
        code = EXIT_INTERRUPT
        return code
    except BrokenPipeError:
        code = EXIT_OK
        return code
    except Exception:
        # Журнал заводили ровно ради таких случаев — он не должен врать,
        # что прогон закончился нулём.
        code = EXIT_GIT
        logbook.error("непредвиденная ошибка", exc_info=True)
        raise
    finally:
        logbook.info("выход с кодом %s", code)


if __name__ == "__main__":
    sys.exit(main())
