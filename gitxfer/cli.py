"""Разбор аргументов и подкоманды."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # запустили файлом, а не как часть пакета
    import sys as _sys

    _sys.exit(
        "git-xfer: this file is part of a package and does not run on its own.\n"
        "  from a clone:     python3 -m gitxfer ...\n"
        "  or the launcher:  ./bin/git-xfer ...\n"
        "  after install:    git-xfer ..."
    )

from . import __version__, logbook
from .config import (
    DEFAULT_DEDUP_WINDOW,
    DEFAULT_SQUASH,
    DEFAULT_GIT_TIMEOUT,
    DEFAULT_KEEP_AUTHOR,
    DEFAULT_KEEP_COAUTHORS,
    DEFAULT_TRAILER,
    DEFAULT_RESOLVE,
    DEFAULT_PATCHID_WINDOW,
    DEFAULT_SCAN_LIMIT,
    Profile,
    SkipRules,
    adhoc_profile,
    config_path,
    load_config,
    log_settings,
    normalize_prefix,
    write_template,
)
from .compare import Comparison, compare
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
    commit_files,
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
from .picker import (
    ALL_TOKENS,
    choose,
    legend,
    parse_selection,
    render_rows,
    require_tty,
    selection_tokens,
)
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
            answer = input(f"  choice [{default}]: ").strip()
        except EOFError:
            raise KeyboardInterrupt from None
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        print(f"    need a number from 1 to {len(options)}")


def _choose_pair(config, args: argparse.Namespace):
    """Профиль: из -p, единственный в конфиге, или спросить."""
    if args.profile:
        return config.pair(args.profile)
    names = sorted(config.profiles)
    if len(names) == 1:
        return config.profiles[names[0]]
    if not sys.stdin.isatty():
        raise ConfigError(
            "no profile given (-p/--profile). Available: " + ", ".join(names)
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
    return config.profiles[names[_pick("Profile:", options) - 1]]


def _choose_direction(pair, args: argparse.Namespace) -> str:
    """Сторона, в которую переносим: из --to, из старого формата, или спросить."""
    if args.to:
        return args.to
    if pair.implied:
        return pair.implied
    if not sys.stdin.isatty():
        raise XferError(
            f"profile {pair.name!r} describes a pair of repositories; give the "
            "direction: --to a or --to b"
        )
    options = [
        (f"{pair.a.path} ({pair.a.branch})  →  {pair.b.path} ({pair.b.branch})", ""),
        (f"{pair.b.path} ({pair.b.branch})  →  {pair.a.path} ({pair.a.branch})", ""),
    ]
    return "b" if _pick("Transfer direction:", options) == 1 else "a"


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
        print("    need a value")


def _ask_setup(
    source: Path | None, source_branch: str | None, target: Path | None, target_branch: str | None
) -> tuple[Path, str, Path, str]:
    """Спросить направление целиком. Ветка цели по умолчанию — та же, что у источника."""
    print("Transfer from and to:")
    source = Path(_ask("source repository", str(source) if source else None)).expanduser()
    source_branch = _ask("source branch", source_branch or _current_branch(source))
    target = Path(_ask("target repository", str(target) if target else None)).expanduser()
    target_branch = _ask(
        "target branch", target_branch or _current_branch(target) or source_branch
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
                "not enough data about the direction, and stdin is not a terminal. "
                "Give a profile (-p) or --source/--target and --branch"
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
    # В имя идут обе ветки: с одним лишь источником `--target-branch side`
    # и `--target-branch release` дали бы одно имя, и защита «серия начата
    # под другим именем» промолчала бы, доиграв очередь в чужую ветку.
    variant = src_branch if src_branch == dst_branch else f"{src_branch}→{dst_branch}"
    profile = adhoc_profile(
        name=base_name if not changed else f"{base_name}@{variant}",
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
        keep_coauthors=base.keep_coauthors if base else DEFAULT_KEEP_COAUTHORS,
        trailer=base.trailer if base else DEFAULT_TRAILER,
        squash=base.squash if base else DEFAULT_SQUASH,
        # Разовое направление правил профиля не знает, но общие из
        # [defaults.skip] действуют и на него, если конфиг есть.
        skip=base.skip if base else (config.skip if config else SkipRules()),
    )
    return profile, config


def _side_line(path: Path, branch: str, prefix: str) -> str:
    where = f"{path} ({branch})"
    return f"{where}, subdirectory {prefix}/" if prefix else where


def _checked_out(git: Git) -> str | None:
    """Выгруженная ветка, `"detached HEAD"` или None, если git не ответил.

    Три состояния, а не два: битый путь в профиле выглядел бы как detached
    HEAD, и человек пошёл бы переключать ветку вместо того, чтобы поправить
    путь.
    """
    result = git.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.text:
        return result.text
    # Код 1 без вывода — HEAD не на ветке; всё прочее — git не смог ответить.
    return "detached HEAD" if result.returncode == 1 else None


def describe(profile: Profile) -> str:
    return (
        "Direction: "
        + _side_line(profile.source, profile.source_branch, profile.source_prefix)
        + "\n        →  "
        + _side_line(profile.target, profile.target_branch, profile.target_prefix)
    )


class Context:
    """Направление + пара Git-обёрток + state: всё, что нужно подкоманде."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.profile, self.config = resolve_profile(args)
        logbook.info("direction: %s", self.profile.describe_short())
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
                f"source objects are missing ({self.profile.ref}), and --dry-run does not let "
                f"fetch them. Run first: git-xfer sync -p {self.profile.name}"
            )
        eprint(f"Source objects are not fetched yet, running sync ({self.profile.ref})")
        sync(self.target, self.profile)
        if not has_ref(self.target, self.profile.ref):
            raise XferError(f"cannot create {self.profile.ref}")

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
    """Из аргументов или интерактива — набор строк таблицы.

    Коммиты под правилами `skip` сюда просто так не проходят (см. `_vet_skipped`).
    """
    return _vet_skipped(context, _selected_rows(context, view))


def _bulk_selection(args: argparse.Namespace) -> bool:
    """Выбор «всё подряд» (`all`, `all !5`), а не названные номера.

    Только в этом случае служебный коммит выпадает молча: «все» — это про
    обычные коммиты, а номер, названный явно, — уже намерение. `--sha`
    главнее `--commits` (см. `_selected_rows`), и с ним выбор всегда явный.
    """
    spec = getattr(args, "commits", None)
    if not spec or getattr(args, "sha", None):
        return False
    tokens = [token.lower() for token in selection_tokens(spec)]
    return any(token in ALL_TOKENS for token in tokens) and all(
        token in ALL_TOKENS or token.startswith("!") for token in tokens
    )


def _vet_skipped(context: Context, rows: list[Row]) -> list[Row]:
    """Служебные коммиты по умолчанию не переносим.

    `all` — выпадают с одной строкой пояснения. Названы явно — в терминале
    переспрашиваем, без терминала отказываем: агент должен сначала спросить
    человека и только потом прийти с `--include-skipped`.
    """
    args = context.args
    marked = [row for row in rows if row.skipped]
    if not marked or getattr(args, "include_skipped", False):
        return rows
    kept = [row for row in rows if not row.skipped]
    listing = [
        f"  {row.mark} {row.commit.short} {row.commit.subject} — {row.skipped}"
        for row in marked
    ]
    logbook.info(
        "under skip rules: %s", ", ".join(row.commit.short for row in marked)
    )
    if _bulk_selection(args):
        print("Not taken, under skip rules from the config:")
        print("\n".join(listing))
        print("  (to take them too: name the numbers explicitly and add --include-skipped)")
        return kept
    if sys.stdin.isatty():
        print("The selection has commits under skip rules from the config:")
        print("\n".join(listing))
        if confirm("Transfer them too?", assume_yes=False):
            return rows
        print("Not taking them")
        return kept
    raise XferError(
        "the selection has commits under skip rules from the config:\n"
        + "\n".join(listing)
        + "\nThey are not transferred by default. If this is intended, "
        "add --include-skipped"
    )


def _selected_rows(context: Context, view: Survey) -> list[Row]:
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
        "nothing to select from: stdin is not a terminal. Give --commits 1,3,5-7 or --sha <hash>..."
    )


def rows_by_sha(context: Context, view: Survey, shas: list[str]) -> list[Row]:
    known = {row.commit.sha: row for row in view.rows}
    result: list[Row] = []
    for raw in shas:
        full = context.target.run(
            "rev-parse", "--verify", "--quiet", f"{raw}^{{commit}}", check=False
        ).text
        if not full:
            raise XferError(f"{raw!r} is not a commit in the target repository (run sync)")
        if not context.target.ok("merge-base", "--is-ancestor", full, context.profile.ref):
            raise XferError(f"{raw!r} is not on the source branch {context.profile.ref}")
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
            skip = context.profile.skip
            files = (
                [
                    item.path
                    for item in commit_files(
                        context.target, [full], context.profile.source_prefix
                    ).get(full, [])
                ]
                if skip.paths
                else None
            )
            row = Row(
                number=0,
                commit=commit,
                status=NEW,
                reason=(
                    f"outside subdirectory {context.profile.source_prefix}/"
                    if outside
                    else "outside the --limit window"
                ),
                partial=partial and not outside,
                skipped=skip.reason(full, commit.subject, files),
            )
        result.append(row)
    return result


def confirm(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise XferError("confirmation needed, but stdin is not a terminal. Add --yes")
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
    print(f"Apply order (old → new), commits: {len(ordered)}")
    for position, commit in enumerate(ordered, start=1):
        print(f"  {position:>3}. {commit.short} {commit.subject}")
    return ordered


# -- подкоманды -----------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path, created = write_template(args.config, force=args.force)
    if created:
        print(f"Created config template: {path}")
        print("Describe the profiles and run: git-xfer doctor -p <profile>")
    else:
        print(f"Config already exists: {path} (to overwrite: init --force)")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    context = Context(args)
    profile = context.profile
    print(f"Profile: {profile.name}")
    print(describe(profile))
    report = run_preflight(context.target, context.source, profile)
    print(report.render())
    if context.state.in_progress:
        progress = context.state.in_progress
        print(
            f"  ! unfinished transfer: done {len(progress.done)}, "
            f"left {len(progress.queue) + (1 if progress.current else 0)}"
        )
    print()
    if report.failures:
        print(f"Not ready: blocking problems: {len(report.failures)}")
        return EXIT_PREFLIGHT
    print(f"Ready to transfer. Warnings: {len(report.warnings)}")
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    context = Context(args)
    profile = context.profile
    before = context.target.run("rev-parse", "--verify", "--quiet", profile.ref, check=False).text
    sha = sync(context.target, profile)
    if args.dry_run:
        return EXIT_OK
    if before == sha:
        print(f"{profile.ref} unchanged: {sha[:12]}")
    else:
        print(f"{profile.ref}: {before[:12] or '—'} → {sha[:12]}")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    context = Context(args)
    context.ensure_ref()
    view = context.survey(limit=args.limit, use_patch_id=not args.no_patch_id)
    rows = view.rows
    hidden = 0
    if args.new:
        # «Что осталось перенести»: служебные сюда не входят по определению.
        hidden = sum(1 for row in rows if row.status == NEW and row.skipped)
        rows = [row for row in rows if row.status == NEW and not row.skipped]
    if not rows:
        print("No commits found")
        if hidden:
            print(f"  hidden by skip rules: {hidden} (without --new everything is shown)")
        return EXIT_OK
    for line in render_rows(rows):
        print(line)
    print()
    print(f"  {legend(view.rows)}")
    if hidden:
        print(f"  hidden by skip rules: {hidden} (without --new everything is shown)")
    return EXIT_OK


def _print_notes(comparison: Comparison) -> None:
    """Подсказки под таблицей «нет в цели» — то, чего не видно в строке."""
    notes = []
    for row in comparison.missing:
        detail = comparison.details[row.commit.sha]
        if detail.in_sync:
            notes.append(f"{row.number}: its files already match on both sides")
        if detail.overlaps:
            notes.append(
                f"{row.number}: the same files were changed only in the target: "
                + ", ".join(detail.overlaps)
            )
        if detail.after_last_transferred is False:
            notes.append(f"{row.number}: older than the last transferred one")
    for note in notes:
        print(f"  {note}")


def cmd_compare(args: argparse.Namespace) -> int:
    context = Context(args)
    context.ensure_ref()
    result = compare(
        context.target,
        context.profile,
        context.state,
        limit=args.limit,
        use_patch_id=not args.no_patch_id,
        allow_merges=args.allow_merges,
    )
    context.state.trim_patchid_cache(result.forward.hot_shas)
    context.state.save()
    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=1))
        return EXIT_OK

    profile = context.profile
    for warning in result.warnings:
        eprint(f"  ! {warning}")
    print(describe(profile))
    print(f"Looking at the last {result.limit} commits of each side (--limit)")
    print()
    files = result.files
    if files.paths:
        print(
            f"Project files now: differ {len(files.differ)}, "
            f"only in source {len(files.source_only)}, "
            f"only in target {len(files.target_only)}"
        )
        if args.files:
            for title, paths in (
                ("differ", files.differ),
                ("only in source", files.source_only),
                ("only in target", files.target_only),
            ):
                if paths:
                    print(f"  {title}:")
                    for path in paths:
                        print(f"    {path}")
        else:
            print("  (list them: --files)")
    else:
        print("Project files now match")

    print()
    missing = result.missing
    if missing:
        print(f"Missing in target: {len(missing)}  (numbers as in list, usable in --commits)")
        for line in render_rows(missing):
            print(line)
        _print_notes(result)
    else:
        print("Missing in target: none")
    if result.skipped:
        print()
        print(f"Excluded by skip rules: {len(result.skipped)}")
        for line in render_rows(result.skipped):
            print(line)
        for row in result.skipped:
            print(f"  {row.number}: {row.skipped}")

    print()
    target_only = result.target_only
    if target_only:
        similar = sum(1 for row in target_only if row.status == SIMILAR)
        title = f"Only in target: {len(target_only) - similar}"
        if similar:
            title += f", plus {similar} ≈ that seem to be in the source too"
        print(title)
        # Номера — окна цели: те же, что у list в обратную сторону.
        print(f"  (numbers as in list --to {profile.source_key})")
        for line in render_rows(target_only):
            print(line)
    else:
        print("Only in target: none")

    print()
    print(f"Already transferred from the source window: {result.transferred}")
    print(f"  {legend(result.forward.rows)}")
    if len(result.forward.rows) >= result.limit:
        print(
            f"  commits older than {result.limit} were not checked: --limit {result.limit * 2}"
        )
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    context = Context(args)
    context.ensure_ref()
    view = context.survey(limit=args.limit, use_patch_id=not args.no_patch_id)
    rows = resolve_rows(context, view)
    if not rows:
        print("Nothing selected")
        return EXIT_OK
    ordered = print_order(context, [row.commit for row in rows])
    # Прогноз — от ветки цели, как у list и compare, а не от выгруженной:
    # иначе план на ветку, на которую ещё предстоит переключиться, считался бы
    # не про неё.
    branch = context.profile.target_branch
    head = context.target.run(
        "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False
    ).text
    if not head:
        raise XferError("the target branch has no commits")
    result = dry_run(
        context.target, head, ordered, projector_for(context.target, context.profile)
    )
    print()
    print("Dry run (work tree not touched):")
    for position, step in enumerate(result.steps, start=1):
        if step.conflicts:
            print(f"  {position:>3}. ✗ {step.commit.short} {step.commit.subject}")
            for path in step.conflicts:
                print(f"        conflict: {path}")
        elif step.empty:
            print(f"  {position:>3}. ∅ {step.commit.short} {step.commit.subject} — will become empty")
        else:
            print(f"  {position:>3}. ✓ {step.commit.short} {step.commit.subject}")
    print()
    conflicted = result.conflicted
    if conflicted:
        print(
            f"Conflicts expected: {len(conflicted)} of {len(result.steps)}. "
            "After the first conflict the prediction is approximate."
        )
    else:
        print(f"No conflicts expected, commits: {len(result.steps)}")
    return EXIT_OK


def _options(args: argparse.Namespace, profile: Profile) -> Options:
    """Опции серии: конфиг профиля, поверх него — флаги этого вызова."""
    keep_author = args.keep_author
    keep_coauthors = args.keep_coauthors
    squash = profile.squash if args.squash is None else args.squash
    message = (args.message or "").strip()
    if message and not squash:
        raise XferError(
            "--message sets the message of the squashed commit and means nothing "
            "without --squash: messages of transferred commits are not rewritten"
        )
    return Options(
        trailer=profile.trailer if args.trailer is None else args.trailer,
        empty=args.empty,
        allow_merges=args.allow_merges,
        hooks=args.hooks,
        gpg_sign=args.gpg_sign,
        keep_committer_date=args.keep_committer_date,
        keep_author=profile.keep_author if keep_author is None else keep_author,
        keep_coauthors=profile.keep_coauthors if keep_coauthors is None else keep_coauthors,
        squash=squash,
        message=message,
    )


def cmd_apply(args: argparse.Namespace) -> int:
    if args.dry_run:
        # Под --dry-run cherry-pick не выполняется, HEAD не двигается,
        # и каждый коммит выглядел бы пустым. Сухой прогон — это plan.
        raise XferError("use the separate subcommand for a dry run: git-xfer plan")
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
        print("Nothing selected")
        return EXIT_OK
    marked = [row for row in rows if row.status != NEW]
    if marked:
        print()
        print("Warning, the selection has commits that were already transferred:")
        for row in marked:
            print(f"  {row.status} {row.commit.short} {row.commit.subject} — {row.reason}")
    ordered = print_order(context, [row.commit for row in rows])
    options = _options(args, context.profile)
    question = (
        f"Transfer {len(ordered)} commit(s) as ONE commit?"
        if options.squash and len(ordered) > 1
        else f"Transfer {len(ordered)} commit(s)?"
    )
    if not confirm(question, assume_yes=args.yes):
        print("Cancelled")
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
        f"Transferred: {outcome.count('ok')}; "
        f"empty: {outcome.count('empty')}; skipped: {outcome.count('skipped')}"
    )
    if outcome.squashed:
        print(
            f"Squashed into one commit: {outcome.squashed[:12]} "
            f"({outcome.squashed_count} commit(s))"
        )
    if outcome.conflict:
        print(f"Stopped at {outcome.conflict[:12]}, {len(outcome.remaining)} more in queue")
        print("Next: git-xfer continue | skip | abort")
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
    print(f"Profile: {profile.name}")
    print(describe(profile))
    # Ветка цели — первое, что нужно решить перед переносом, и чаще всего
    # выгружена не та, что записана в профиле. Печатаем обе: из одного
    # `status` должно быть видно, куда коммиты лягут на самом деле.
    checked_out = _checked_out(context.target)
    if checked_out == profile.target_branch:
        print(f"Target branch: {profile.target_branch} (checked out)")
    elif checked_out is None:
        print(
            f"Target branch: {profile.target_branch} in the profile, "
            f"cannot read what is checked out. Details: git-xfer doctor"
        )
    else:
        # Именно «остановится», а не «перенесёт куда попало»: расхождение
        # ветки — это FAIL предполётной проверки, и apply не начнётся.
        print(
            f"Target branch: {profile.target_branch} in the profile, "
            f"{checked_out} is checked out; a transfer like this "
            f"stops (exit code 2). To transfer into the checked-out one: "
            f"--target-branch {checked_out}"
        )
    print(f"State:   {state_path(profile.target)}")
    print(f"Log:     {logbook.path() or 'off'}")
    policy = context.config.resolve_conflicts if context.config else DEFAULT_RESOLVE
    print(f"Conflicts: {policy} (policy for the skill, not for the tool itself)")
    # У начатой серии режим свой: она доигрывается теми опциями, с которыми
    # начиналась, даже если конфиг тем временем поправили. Показывать здесь
    # конфиг значило бы соврать ровно там, где за ответом и приходят.
    started = Options.from_json(progress.opts) if progress else None
    trailer = started.trailer if started else profile.trailer
    keep_author = started.keep_author if started else profile.keep_author
    keep_coauthors = started.keep_coauthors if started else profile.keep_coauthors
    squash = started.squash if started else profile.squash
    whose = " (the series started with these)" if started else ""
    print(
        "Message: "
        + (
            "+ trailer (cherry picked from commit ...)"
            if trailer
            else "no trailer"
        )
        + (
            ", Co-authored-by lines are kept (keep_coauthors)"
            if keep_coauthors
            else ", Co-authored-by lines are stripped"
        )
        + whose
    )
    print(
        "Authorship: "
        + (
            "the original commit author is kept (keep_author)"
            if keep_author
            else "the person who transfers becomes the author"
        )
        + whose
    )
    print(
        "Squash: "
        + ("the whole series into one commit (squash)" if squash else "commit by commit")
        + whose
    )
    if not progress:
        print("No unfinished transfer")
        return EXIT_OK
    head = head_sha(context.target) or "—"
    print(f"Started: {progress.started_at}, profile {progress.profile}")
    print(f"  done {len(progress.done)}, in queue {len(progress.queue)}")
    if progress.current:
        print(f"  stopped at {progress.current[:12]}")
    print(f"  HEAD before start: {progress.head_before[:12]}")
    print(f"  HEAD expected: {progress.expected_head[:12]}, actual: {head[:12]}")
    if head != progress.expected_head:
        print("  ! HEAD is not where git-xfer left it; the transfer was changed by hand")
        print("    to forget the unfinished series: git-xfer cleanup --state")
    return EXIT_OK


def cmd_cleanup(args: argparse.Namespace) -> int:
    context = Context(args)
    profile = context.profile
    if context.state.in_progress and not args.state:
        raise XferError(
            "a transfer is unfinished; first continue/skip/abort "
            "or add --state to forget it"
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
            print(f"Deleted {ref}")
    if not refs:
        print("No refs/xfer/* refs found")
    if args.state:
        path = state_path(profile.target)
        if args.dry_run:
            print(f"[dry-run] delete {path}")
        elif path.exists():
            path.unlink()
            print(f"Deleted state {path}")
        else:
            print("No state file")
    if refs and not args.dry_run:
        print("Source objects become unreachable; git gc will free them")
    return EXIT_OK


# -- разбор аргументов ----------------------------------------------------


def _add_repo_flags(parser: argparse.ArgumentParser) -> None:
    """Направление и разовые переопределения — без правки конфига."""
    group = parser.add_argument_group("direction")
    group.add_argument(
        "--to",
        choices=("a", "b"),
        help="which side of the pair to transfer to; asked if not given",
    )
    group.add_argument(
        "-b", "--branch", metavar="NAME", help="branch on both sides, one-off"
    )
    group.add_argument("--source-branch", metavar="NAME", help="source branch")
    group.add_argument(
        "--target-branch", metavar="NAME", help="target branch (same by default)"
    )
    group.add_argument(
        "--source", type=Path, metavar="PATH", help="source repository, bypassing the config"
    )
    group.add_argument(
        "--target", type=Path, metavar="PATH", help="target repository, bypassing the config"
    )
    group.add_argument(
        "--source-prefix",
        metavar="DIR",
        help="source subdirectory that holds the project",
    )
    group.add_argument(
        "--target-prefix",
        metavar="DIR",
        help="target subdirectory to put the project in",
    )
    group.add_argument(
        "--ask", action="store_true", help="ask for repositories and branches interactively"
    )


def _add_profile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--profile", help="profile name from the config")
    _add_repo_flags(parser)


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-i", "--interactive", action="store_true", help="select commits from a list")
    parser.add_argument("--commits", help="numbers from the list: 1,3,5-7 (also !5 and all)")
    parser.add_argument("--sha", nargs="+", metavar="SHA", help="source commits by hash")
    parser.add_argument("--limit", type=int, help="how many commits to show")
    parser.add_argument("--no-patch-id", action="store_true", help="do not compute patch-id (faster)")
    parser.add_argument(
        "--allow-merges", action="store_true", help="show and transfer merge commits too"
    )
    parser.add_argument(
        "--include-skipped", action="store_true",
        help="take commits under skip rules from the config too",
    )


class Parser(argparse.ArgumentParser):
    """argparse по умолчанию выходит с кодом 2, а он занят предполётной
    проверкой: скрипт должен отличать «репозиторий не готов» от «неверный вызов»."""

    def error(self, message: str):  # noqa: D102
        self.print_usage(sys.stderr)
        eprint(f"{self.prog}: error: {message}")
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
        help=f"config path (default {config_path()})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=hidden if after_command else False,
        help="print git calls",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        default=hidden if after_command else False,
        help="do not run commands that change the repository",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        default=hidden if after_command else False,
        help="do not write the run log",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=hidden if after_command else None,
        metavar="PATH",
        help="where to write the log (default: next to the state)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="git-xfer",
        description="Transfer commits between unrelated git repositories.",
    )
    parser.add_argument("--version", action="version", version=f"git-xfer {__version__}")
    _add_common(parser, after_command=False)
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, after_command=True)
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    init = subparsers.add_parser("init", help="create a config template", parents=[common])
    init.add_argument("--force", action="store_true", help="overwrite the existing one")
    init.set_defaults(func=cmd_init)

    doctor = subparsers.add_parser("doctor", help="preflight checks", parents=[common])
    _add_profile(doctor)
    doctor.set_defaults(func=cmd_doctor)

    sync_cmd = subparsers.add_parser("sync", help="fetch objects source → target", parents=[common])
    _add_profile(sync_cmd)
    sync_cmd.set_defaults(func=cmd_sync)

    list_cmd = subparsers.add_parser("list", help="table of source commits", parents=[common])
    _add_profile(list_cmd)
    list_cmd.add_argument("--limit", type=int, help="how many commits to show")
    list_cmd.add_argument("--new", action="store_true", help="only commits not transferred yet")
    list_cmd.add_argument("--no-patch-id", action="store_true", help="do not compute patch-id")
    list_cmd.add_argument(
        "--allow-merges", action="store_true", help="show merge commits too"
    )
    list_cmd.set_defaults(func=cmd_list)

    compare_cmd = subparsers.add_parser(
        "compare", help="what is missing in the target and what is only there", parents=[common]
    )
    _add_profile(compare_cmd)
    compare_cmd.add_argument("--limit", type=int, help="how many commits to check on each side")
    compare_cmd.add_argument("--json", action="store_true", help="output for agents and scripts")
    compare_cmd.add_argument("--files", action="store_true", help="list the differing files")
    compare_cmd.add_argument("--no-patch-id", action="store_true", help="do not compute patch-id")
    compare_cmd.add_argument(
        "--allow-merges", action="store_true", help="show merge commits too"
    )
    compare_cmd.set_defaults(func=cmd_compare)

    plan_cmd = subparsers.add_parser("plan", help="dry run: where conflicts will be", parents=[common])
    _add_profile(plan_cmd)
    _add_selection(plan_cmd)
    plan_cmd.set_defaults(func=cmd_plan)

    apply_cmd = subparsers.add_parser("apply", help="transfer the selected commits", parents=[common])
    _add_profile(apply_cmd)
    _add_selection(apply_cmd)
    apply_cmd.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    trailer = apply_cmd.add_mutually_exclusive_group()
    trailer.add_argument(
        "--trailer", dest="trailer", action="store_true", default=None,
        help="append (cherry picked from commit ...) for reliable dedup",
    )
    trailer.add_argument(
        "--no-trailer", dest="trailer", action="store_false",
        help="leave the commit message as is (default)",
    )
    apply_cmd.add_argument(
        "--empty", choices=("drop", "keep", "stop"), default="drop",
        help="what to do with a commit that became empty",
    )
    apply_cmd.add_argument("--hooks", action="store_true", help="do not turn off hooks")
    apply_cmd.add_argument("--gpg-sign", action="store_true", help="sign commits")
    apply_cmd.add_argument(
        "--keep-committer-date", action="store_true", help="keep the source committer date"
    )
    squash = apply_cmd.add_mutually_exclusive_group()
    squash.add_argument(
        "--squash", dest="squash", action="store_true", default=None,
        help="squash the selected commits into one",
    )
    squash.add_argument(
        "--no-squash", dest="squash", action="store_false",
        help="transfer commit by commit (default)",
    )
    apply_cmd.add_argument(
        "--message", metavar="TEXT",
        help="message of the squashed commit; without it, the series messages in order",
    )
    author = apply_cmd.add_mutually_exclusive_group()
    author.add_argument(
        "--keep-author", dest="keep_author", action="store_true", default=None,
        help="keep the original commit author as author",
    )
    author.add_argument(
        "--reset-author", dest="keep_author", action="store_false",
        help="the person who transfers is the author (default)",
    )
    coauthors = apply_cmd.add_mutually_exclusive_group()
    coauthors.add_argument(
        "--keep-coauthors", dest="keep_coauthors", action="store_true", default=None,
        help="keep Co-authored-by lines in the message",
    )
    coauthors.add_argument(
        "--strip-coauthors", dest="keep_coauthors", action="store_false",
        help="strip Co-authored-by lines (default)",
    )
    apply_cmd.set_defaults(func=cmd_apply)

    cont = subparsers.add_parser("continue", help="continue after resolving a conflict", parents=[common])
    _add_profile(cont)
    cont.set_defaults(func=cmd_continue)

    skip_cmd = subparsers.add_parser("skip", help="skip the conflicting commit", parents=[common])
    _add_profile(skip_cmd)
    skip_cmd.set_defaults(func=cmd_skip)

    abort_cmd = subparsers.add_parser("abort", help="abort the series, rolling back the current commit", parents=[common])
    _add_profile(abort_cmd)
    abort_cmd.set_defaults(func=cmd_abort)

    status_cmd = subparsers.add_parser("status", help="state of an unfinished transfer", parents=[common])
    _add_profile(status_cmd)
    status_cmd.set_defaults(func=cmd_status)

    cleanup = subparsers.add_parser("cleanup", help="remove refs/xfer/* and state", parents=[common])
    _add_profile(cleanup)
    cleanup.add_argument("--all", action="store_true", help="all refs/xfer/*, not only the profile ones")
    cleanup.add_argument("--state", action="store_true", help="delete the state file too")
    cleanup.set_defaults(func=cmd_cleanup)

    return parser


def _log_hint() -> str:
    path = logbook.path()
    return f"\nRun details: {path}" if path else ""


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
        eprint(f"git-xfer: log is off ({exc})")
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
        logbook.warn("interrupted from the keyboard")
        eprint()
        eprint("git-xfer: interrupted")
        code = EXIT_INTERRUPT
        return code
    except BrokenPipeError:
        code = EXIT_OK
        return code
    except Exception:
        # Журнал заводили ровно ради таких случаев — он не должен врать,
        # что прогон закончился нулём.
        code = EXIT_GIT
        logbook.error("unexpected error", exc_info=True)
        raise
    finally:
        logbook.info("exit code %s", code)


if __name__ == "__main__":
    sys.exit(main())
