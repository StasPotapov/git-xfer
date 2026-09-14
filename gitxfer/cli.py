"""Разбор аргументов и подкоманды."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config, Profile, config_path, load_config, write_template
from .discover import (
    NEW,
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
    EXIT_INTERRUPT,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_USAGE,
    XferError,
)
from .gitcmd import Git
from .plan import dry_run
from .preflight import head_sha, run_preflight, stale_xfer_refs
from .select import choose, legend, parse_selection, render_rows, require_tty
from .state import State, state_path
from .transfer import Options, abort, resume, start


def eprint(text: str = "") -> None:
    print(text, file=sys.stderr)


# -- контекст выполнения --------------------------------------------------


class Context:
    """Профиль + пара Git-обёрток + state: всё, что нужно подкоманде."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.config: Config = load_config(args.config)
        self.profile: Profile = self.config.profile(args.profile)
        self.target = Git(self.profile.target, dry_run=args.dry_run, verbose=args.verbose)
        self.source = Git(self.profile.source, dry_run=args.dry_run, verbose=args.verbose)
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
                f"подтянуть. Выполните сначала: git xfer sync -p {self.profile.name}"
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
            commit = Commit(sha=full, short=full[:12], date="", author="", subject="")
            row = Row(number=0, commit=commit, status=NEW, reason="вне окна --limit")
        result.append(row)
    return result


def confirm(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise XferError("нужно подтверждение, но stdin не терминал. Добавьте --yes")
    answer = input(f"{question} [y/N]: ").strip().lower()
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
        print("Опишите профили и запустите: git xfer doctor -p <профиль>")
    else:
        print(f"Конфиг уже существует: {path} (перезаписать — init --force)")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    context = Context(args)
    profile = context.profile
    print(f"Профиль {profile.name}: {profile.source} ({profile.source_branch})")
    print(f"           → {profile.target} ({profile.target_branch})")
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
    result = dry_run(context.target, head, ordered)
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


def _options(args: argparse.Namespace) -> Options:
    return Options(
        trailer=not args.no_trailer,
        empty=args.empty,
        allow_merges=args.allow_merges,
        hooks=args.hooks,
        gpg_sign=args.gpg_sign,
        keep_committer_date=args.keep_committer_date,
    )


def cmd_apply(args: argparse.Namespace) -> int:
    if args.dry_run:
        # Под --dry-run cherry-pick не выполняется, HEAD не двигается,
        # и каждый коммит выглядел бы пустым. Сухой прогон — это plan.
        raise XferError("для сухого прогона есть отдельная подкоманда: git xfer plan")
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
    if not confirm(f"Перенести {len(ordered)} коммит(ов)?", assume_yes=args.yes):
        print("Отменено")
        return EXIT_OK
    print()
    outcome = start(
        context.target,
        context.profile,
        context.state,
        [commit.sha for commit in ordered],
        _options(args),
        report=print,
    )
    return _summary(outcome)


def _summary(outcome) -> int:
    print()
    print(
        f"Перенесено: {outcome.count('ok')}; "
        f"пусто: {outcome.count('empty')}; пропущено: {outcome.count('skipped')}"
    )
    if outcome.conflict:
        print(f"Остановлено на {outcome.conflict[:12]}, в очереди ещё {len(outcome.remaining)}")
        print("Дальше: git xfer continue | skip | abort")
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
    print(f"Профиль {profile.name}: {profile.source} → {profile.target}")
    print(f"State: {state_path(profile.target)}")
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
        print("    забыть незавершённую серию: git xfer cleanup --state")
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
    if not args.all and has_ref(context.target, profile.ref):
        refs = [profile.ref]
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


def _add_profile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--profile", help="имя профиля из конфига")


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


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="git xfer",
        description="Перенос коммитов между несвязанными git-репозиториями.",
    )
    parser.add_argument("--version", action="version", version=f"git-xfer {__version__}")
    parser.add_argument("--config", type=Path, help=f"путь к конфигу (по умолчанию {config_path()})")
    parser.add_argument("-v", "--verbose", action="store_true", help="печатать вызовы git")
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="не выполнять команды, меняющие репозиторий"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="создать шаблон конфига")
    init.add_argument("--force", action="store_true", help="перезаписать существующий")
    init.set_defaults(func=cmd_init)

    doctor = subparsers.add_parser("doctor", help="предполётные проверки")
    _add_profile(doctor)
    doctor.set_defaults(func=cmd_doctor)

    sync_cmd = subparsers.add_parser("sync", help="перенести объекты source → target")
    _add_profile(sync_cmd)
    sync_cmd.set_defaults(func=cmd_sync)

    list_cmd = subparsers.add_parser("list", help="таблица коммитов источника")
    _add_profile(list_cmd)
    list_cmd.add_argument("--limit", type=int, help="сколько коммитов показывать")
    list_cmd.add_argument("--new", action="store_true", help="только непереносившиеся")
    list_cmd.add_argument("--no-patch-id", action="store_true", help="не считать patch-id")
    list_cmd.add_argument(
        "--allow-merges", action="store_true", help="показывать и merge-коммиты"
    )
    list_cmd.set_defaults(func=cmd_list)

    plan_cmd = subparsers.add_parser("plan", help="сухой прогон: где будут конфликты")
    _add_profile(plan_cmd)
    _add_selection(plan_cmd)
    plan_cmd.set_defaults(func=cmd_plan)

    apply_cmd = subparsers.add_parser("apply", help="перенести выбранные коммиты")
    _add_profile(apply_cmd)
    _add_selection(apply_cmd)
    apply_cmd.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    apply_cmd.add_argument(
        "--no-trailer", action="store_true", help="без (cherry picked from commit ...)"
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
    apply_cmd.set_defaults(func=cmd_apply)

    cont = subparsers.add_parser("continue", help="продолжить после разрешения конфликта")
    _add_profile(cont)
    cont.set_defaults(func=cmd_continue)

    skip_cmd = subparsers.add_parser("skip", help="пропустить конфликтный коммит")
    _add_profile(skip_cmd)
    skip_cmd.set_defaults(func=cmd_skip)

    abort_cmd = subparsers.add_parser("abort", help="прервать серию, откатив текущий коммит")
    _add_profile(abort_cmd)
    abort_cmd.set_defaults(func=cmd_abort)

    status_cmd = subparsers.add_parser("status", help="состояние незавершённого переноса")
    _add_profile(status_cmd)
    status_cmd.set_defaults(func=cmd_status)

    cleanup = subparsers.add_parser("cleanup", help="убрать refs/xfer/* и state")
    _add_profile(cleanup)
    cleanup.add_argument("--all", action="store_true", help="все refs/xfer/*, не только профиля")
    cleanup.add_argument("--state", action="store_true", help="удалить и state-файл")
    cleanup.set_defaults(func=cmd_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except XferError as exc:
        eprint(f"git-xfer: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        eprint()
        eprint("git-xfer: прервано")
        return EXIT_INTERRUPT
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
