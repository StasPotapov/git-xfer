"""Чтение конфига и профилей.

Конфиг лежит вне репозитория утилиты (`~/.config/git-xfer/config.toml`,
с учётом `XDG_CONFIG_HOME`) и правится руками; мы его только читаем.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError
from .gitcmd import DEFAULT_TIMEOUT

APP = "git-xfer"

DEFAULT_SCAN_LIMIT = 30
#: Как глубоко читать целевую историю в поисках трейлеров. Канал дешёвый
#: (`git log` без диффов), а окно определяет, на сколько своих коммитов назад
#: утилита помнит, что уже переносила, — экономить тут нечего.
DEFAULT_DEDUP_WINDOW = 5000
#: Окно patch-id. Канал дорогой (считается дифф каждого коммита), зато
#: результат кэшируется в state, так что платим один раз.
DEFAULT_PATCHID_WINDOW = 1000

#: Сохранять ли авторство исходного коммита. По умолчанию нет: перенос
#: обычно тащит свои же изменения между своими же репозиториями, и чужая
#: личность в истории целевой стороны там только мешает. Кому нужно
#: наоборот — `keep_author = true` в конфиге или `--keep-author` разово.
DEFAULT_KEEP_AUTHOR = False

#: Оставлять ли в сообщении строки `Co-authored-by:`. По умолчанию нет: по
#: той же причине, что и с автором, — в целевой истории коммит ваш, и чужие
#: соавторы из исходного сообщения там лишние. Кому нужно наоборот —
#: `keep_coauthors = true` в конфиге или `--keep-coauthors` разово.
DEFAULT_KEEP_COAUTHORS = False

#: Дописывать ли в сообщение `(cherry picked from commit <sha>)`. По
#: умолчанию нет: в сообщение ничего не дописывается. Цена — самый
#: надёжный канал дедупликации выключен, и «уже переносили» утилита знает
#: только из своего state (эта машина) и по patch-id (эвристика ≈).
#: Кому дедупликация важнее чистого сообщения — `trailer = true`.
DEFAULT_TRAILER = False

#: Схлопывать ли выбранные коммиты в один. По умолчанию нет: перенос
#: сохраняет историю как есть. Кому нужен один коммит на всё — `squash = true`
#: в конфиге или `--squash` разово.
DEFAULT_SQUASH = False

#: Потолок на один вызов git, секунды. Нужен не для скорости, а против
#: зависаний: git, севший ждать ввода (редактор, пейджер, запрос пароля),
#: без него держит утилиту вечно, и агент, у которого нет терминала,
#: просто перестаёт отвечать. 0 — без ограничения. Значение живёт там же,
#: где применяется, — в обёртке над git; здесь только имя для конфига.
DEFAULT_GIT_TIMEOUT = DEFAULT_TIMEOUT

#: Насколько самостоятельно агент разбирает конфликты. Читает это скилл,
#: сама утилита ничего по ней не делает — но проверяет значение и
#: показывает его в `status`, чтобы опечатка не оказалась молчаливой.
RESOLVE_POLICIES = ("ask", "mechanical", "auto")
DEFAULT_RESOLVE = "mechanical"

_REF_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

CONFIG_TEMPLATE = """\
# git-xfer config. Edit it by hand; the tool only reads it.
#
# A profile is a PAIR of repositories, not a direction. You choose the
# direction when you run the tool: `--to b` moves from a to b, `--to a` back.
# You can have any number of profiles: different pairs, different branches of one pair.

[defaults]
# How many source commits to show. One-off: the --limit flag.
scan_limit = 30

# How deep to read the target history when looking for the trailer
# (cherry picked from commit ...). Works only when the trailer is on
# (see trailer below), but then it is the most reliable channel: the window
# sets how many of its own commits back the tool remembers what it transferred.
# Reading is fast, no point in making it smaller.
dedup_window = 5000

# Window for the patch-id comparison, a heuristic for the ~ mark.
# More expensive, but the result is cached.
patchid_window = 1000

# The line (cherry picked from commit <sha>) in the transferred commit message.
#   false — do NOT add it: nothing is appended  ← default
#   true  — add it: this line appears at the end of the message
# What false costs: the trailer is the most reliable dedup channel, it lives
# in the target history itself. Without it the tool knows a commit was
# transferred only from its own state (this machine, and only in the direction
# you transferred) and by patch-id, a heuristic that marks the commit with ≈.
# One-off, without editing the config: --trailer / --no-trailer.
trailer = false

# Whether to squash the selected commits into one.
#   false — do NOT squash: as many commits arrive as you selected  ← default
#   true  — the whole selected series becomes one commit
# Message of the squashed commit: the series messages in order, or your own: --message "...".
# One-off: --squash / --no-squash.
squash = false

# Whose name stays in the author field of the transferred commit.
#   false — yours: author and committer are the person who transfers,
#           author date is the time of the transfer           ← default
#   true  — keep the author and author date of the original commit
# git always makes the person who transfers the committer (who applied it):
# that field has no setting.
# One-off: --keep-author / --reset-author.
keep_author = false

# The "Co-authored-by: …" lines in the transferred commit message.
#   false — strip them: co-authors of the original commit are not carried  ← default
#   true  — keep the message as is
# One-off: --keep-coauthors / --strip-coauthors.
keep_coauthors = false

# Limit for one git call, in seconds. Protects from hangs: git waiting
# for input (editor, pager, password prompt) would hold the tool forever.
#   600 — stop a call that runs longer than ten minutes  ← default
#   0   — no limit
git_timeout = 600

# Run log: what ran, with what exit code, and where it stopped.
# Lives next to the state, outside the working repositories, never gets into git.
# To see the path: git-xfer status
log = true
# log_file = "~/.local/state/git-xfer/git-xfer.log"

# Which commits NOT to transfer by default: service commits, drafts, other people's.
# Such a commit stays in the list with the × mark but is not included in `all`;
# naming it explicitly needs confirmation (without a terminal, the
# --include-skipped flag). Profile rules ([profiles.<name>.skip]) are added
# to these, they do not replace them. All three keys are optional.
#   subject — regexps on the commit subject (re.search); (?i) ignores case
#   commits — source commit hashes, short ones are fine
#   paths   — path masks relative to the project root; a commit is excluded
#             only if ALL its files match. * also matches /
# [defaults.skip]
# subject = ["(?i)^chore", "^wip\\\\b"]
# commits = ["a1b2c3d"]
# paths = ["*.lock", ".idea/*"]

[agent]
# How independently the agent (the git-xfer skill for Claude Code) resolves
# conflicts. The skill reads this; the tool itself does nothing with it, it only
# checks the value and shows it in `git-xfer status`.
#   "ask"        — changes nothing itself, only shows the gist and suggests
#   "mechanical" — fixes mechanical conflicts itself (imports, adjacent
#                  lines, formatting, generated code) and shows what it
#                  did; always leaves conflicts of meaning to a human
#   "auto"       — resolves everything itself, conflicts of meaning too, and
#                  reports afterwards. A mistake here lands as a commit in a
#                  working repository, so turn it on knowingly
resolve_conflicts = "mechanical"

# [profiles.myproj]
# a = "/path/to/repo-a"
# b = "/path/to/repo-b"
# branch = "master"        # one branch on both sides
#
# trailer, keep_author, keep_coauthors, squash, scan_limit, dedup_window
# and patchid_window can be overridden inside a profile.
# Profile skip rules go in [profiles.myproj.skip]: they add to the shared ones.
#
# [profiles.myproj-release]  # same pair, other branches
# a = "/path/to/repo-a"
# b = "/path/to/repo-b"
# a_branch = "release/1.x"   # if the branches have different names
# b_branch = "stable"
# --- The project lives in a subdirectory of one of the repositories ------
# A side path is ALWAYS the repository root; the subdirectory is set with
# a separate key <side>_prefix. Paths are rewritten on transfer: the source
# prefix is removed, the target prefix is added.
#
# [profiles.myapp]
# a = "/path/to/monorepo"
# a_prefix = "apps/mobile"       # relative to the repo root, no leading slash
# b = "/path/to/personal-repo"   # here the project is at the root, no b_prefix needed
# branch = "master"
#
# A commit that does not touch the subdirectory is not listed at all. A commit
# that touches both the subdirectory and files outside it is transferred
# PARTIALLY (only the inside part) and marked with an asterisk in the list: + *
"""

def _xdg_dir(var: str, fallback: str) -> Path:
    raw = os.environ.get(var)
    base = Path(raw).expanduser() if raw else Path.home() / fallback
    return base / APP


def config_dir() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def config_path() -> Path:
    return config_dir() / "config.toml"


def normalize_prefix(raw: str | None, where: str = "prefix") -> str:
    """`"./a/b/"` → `"a/b"`. Абсолютный путь и `..` — ошибка конфига."""
    if raw is None or not str(raw).strip():
        return ""
    text = str(raw).strip().replace("\\", "/")
    if text.startswith("/"):
        raise ConfigError(
            f"{where}: need a path inside the repository, not an absolute one ({raw!r})"
        )
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ConfigError(f"{where}: '..' is not allowed in the path ({raw!r})")
        parts.append(part)
    return "/".join(parts)


def _at(prefix: str) -> str:
    return f"/{prefix}" if prefix else ""


@dataclass(frozen=True)
class SkipRules:
    """Какие коммиты по умолчанию не переносить.

    Правило не прячет коммит: он остаётся в списке с пометкой ×, но в выбор
    `all` не попадает, а явный выбор требует подтверждения. Так служебный
    коммит нельзя перенести случайно, но можно — осознанно.
    """

    #: Регэкспы, `re.search` по заголовку коммита.
    subject: tuple[re.Pattern[str], ...] = ()
    #: Хеши коммитов источника, можно сокращённые; в нижнем регистре.
    commits: tuple[str, ...] = ()
    #: glob по путям относительно корня проекта (префикс стороны срезан):
    #: коммит исключён, если под них подходят ВСЕ его файлы. `*` проходит `/`.
    paths: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.subject or self.commits or self.paths)

    def merged(self, other: "SkipRules") -> "SkipRules":
        """Правила профиля добавляются к общим, а не заменяют их."""
        return SkipRules(
            subject=self.subject + other.subject,
            commits=self.commits + other.commits,
            paths=self.paths + other.paths,
        )

    def reason(self, sha: str, subject: str, files: list[str] | None = None) -> str:
        """Почему коммит исключён; пустая строка — не исключён.

        `files` — пути коммита относительно корня проекта; None — не считали
        (правил по путям нет), пустой список — коммит без файлов.
        """
        sha = sha.lower()
        for prefix in self.commits:
            if sha.startswith(prefix):
                return f"hash in skip.commits ({prefix})"
        for pattern in self.subject:
            if pattern.search(subject):
                return f"subject: {pattern.pattern}"
        if self.paths and files:
            if all(any(fnmatch.fnmatchcase(path, glob) for glob in self.paths) for path in files):
                return "only files from skip.paths"
        return ""


def _parse_skip(table: object, where: str) -> SkipRules:
    """Подтаблица `skip`. Ошибки — словами и сразу, при чтении конфига."""
    if table is None:
        return SkipRules()
    # «[defaults]» → «[defaults.skip]»: так секцию и ищут глазами в файле.
    where = f"{where[:-1]}.skip]" if where.endswith("]") else f"{where}.skip"
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: must be a table")
    unknown = sorted(set(table) - {"subject", "commits", "paths"})
    if unknown:
        # Опечатка в ключе молча выключила бы правило — ровно то, от чего
        # правило и заводили.
        raise ConfigError(
            f"{where}: unknown keys {', '.join(unknown)}; "
            "allowed: subject, commits, paths"
        )

    def strings(key: str) -> list[str]:
        """Как написано: в регэкспе пробел по краям — часть шаблона."""
        value = table.get(key, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ConfigError(f"{where}: {key!r} must be a list of non-empty strings")
        return list(value)

    subject: list[re.Pattern[str]] = []
    for raw in strings("subject"):
        try:
            subject.append(re.compile(raw))
        except re.error as exc:
            raise ConfigError(f"{where}: cannot parse regexp {raw!r}: {exc}") from None
    commits: list[str] = []
    for raw in (item.strip() for item in strings("commits")):
        if not re.fullmatch(r"[0-9a-fA-F]{4,64}", raw):
            raise ConfigError(f"{where}: {raw!r} in 'commits' is not a commit hash")
        commits.append(raw.lower())
    paths = [raw.strip().lstrip("/") for raw in strings("paths")]
    return SkipRules(subject=tuple(subject), commits=tuple(commits), paths=tuple(paths))


@dataclass(frozen=True)
class Side:
    """Одна сторона пары."""

    key: str          # "a" или "b"
    path: Path
    branch: str
    #: Подкаталог, в котором на этой стороне живёт проект. Пусто — проект
    #: лежит в корне репозитория.
    prefix: str = ""


@dataclass(frozen=True)
class Profile:
    """Одно направление переноса."""

    name: str
    source: Path
    source_branch: str
    target: Path
    target_branch: str
    scan_limit: int
    dedup_window: int
    patchid_window: int
    #: Подкаталоги сторон. Перенос срезает `source_prefix` и дописывает
    #: `target_prefix`; пустые — пути идут как есть.
    source_prefix: str = ""
    target_prefix: str = ""
    #: Какой стороной пары ("a"/"b") оказались источник и цель. Нужно там,
    #: где мы советуем человеку, какой ключ конфига править.
    source_key: str = "a"
    target_key: str = "b"
    #: Оставлять ли в перенесённом коммите автора исходного. По умолчанию
    #: нет: автором становится тот, кто переносит.
    keep_author: bool = DEFAULT_KEEP_AUTHOR
    #: Оставлять ли строки `Co-authored-by:`. По умолчанию нет: вырезаются.
    keep_coauthors: bool = DEFAULT_KEEP_COAUTHORS
    #: Дописывать ли трейлер `(cherry picked from commit ...)`.
    trailer: bool = DEFAULT_TRAILER
    #: Схлопывать ли всю серию в один коммит.
    squash: bool = DEFAULT_SQUASH
    #: Какие коммиты по умолчанию не переносить.
    skip: SkipRules = SkipRules()

    @property
    def alias(self) -> str:
        """Имя профиля, пригодное как компонент ref.

        Если чистка что-то изменила, дописываем хвост хеша исходного имени:
        иначе `proj/back` и `proj-back` схлопнутся в один ref, и forced-пуш
        второго профиля молча перезапишет объекты первого.
        """
        alias = _REF_UNSAFE.sub("-", self.name).strip("-.")
        if alias == self.name:
            return alias
        # В хеш идёт всё, что делает направление уникальным: иначе разовые
        # прогоны по разным веткам подрались бы за один и тот же ref.
        # Префикс сюда не входит намеренно: в refs/xfer/<alias>/head лежит
        # кончик ветки источника как есть, и от подкаталога он не зависит —
        # двум профилям по одной паре незачем тащить один и тот же pack дважды.
        seed = "\0".join(
            [self.name, str(self.source), self.source_branch, self.target_branch]
        )
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
        return f"{alias or 'profile'}-{digest}"

    @property
    def ref(self) -> str:
        """Ref, под которым в целевом репо живут объекты источника."""
        return f"refs/xfer/{self.alias}/head"

    def describe_short(self) -> str:
        return (
            f"{self.source}{_at(self.source_prefix)} ({self.source_branch}) → "
            f"{self.target}{_at(self.target_prefix)} ({self.target_branch})"
        )

    @property
    def has_prefix(self) -> bool:
        return bool(self.source_prefix or self.target_prefix)

    @property
    def pick_ref(self) -> str:
        """Ссылка, держащая синтетический коммит, пока идёт cherry-pick."""
        return f"refs/xfer/{self.alias}/pick"

    @property
    def source_refspec(self) -> str:
        return f"+refs/heads/{self.source_branch}:{self.ref}"


@dataclass(frozen=True)
class Pair:
    """Пара репозиториев. Направление в ней не зашито — выбирается при вызове."""

    name: str
    a: Side
    b: Side
    scan_limit: int
    dedup_window: int
    patchid_window: int
    keep_author: bool = DEFAULT_KEEP_AUTHOR
    keep_coauthors: bool = DEFAULT_KEEP_COAUTHORS
    trailer: bool = DEFAULT_TRAILER
    squash: bool = DEFAULT_SQUASH
    skip: SkipRules = SkipRules()
    #: Для старого формата source/target направление задано самими ключами,
    #: и спрашивать о нём нечего. У пары a/b его выбирают при вызове.
    implied: str | None = None

    def side(self, key: str) -> Side:
        if key not in ("a", "b"):
            raise ConfigError(f"side must be 'a' or 'b', not {key!r}")
        return self.a if key == "a" else self.b

    def direction(self, to: str) -> Profile:
        """Собрать направление «в сторону `to`»."""
        target = self.side(to)
        source = self.b if to == "a" else self.a
        return Profile(
            name=f"{self.name}-to-{to}",
            source=source.path,
            source_branch=source.branch,
            target=target.path,
            target_branch=target.branch,
            scan_limit=self.scan_limit,
            dedup_window=self.dedup_window,
            patchid_window=self.patchid_window,
            source_prefix=source.prefix,
            target_prefix=target.prefix,
            source_key=source.key,
            target_key=target.key,
            keep_author=self.keep_author,
            keep_coauthors=self.keep_coauthors,
            trailer=self.trailer,
            squash=self.squash,
            skip=self.skip,
        )


@dataclass(frozen=True)
class Config:
    path: Path
    profiles: dict[str, Pair]
    log_enabled: bool = True
    log_file: Path | None = None
    #: Политика для скилла git-xfer, не для самой утилиты.
    resolve_conflicts: str = DEFAULT_RESOLVE
    #: Потолок на один вызов git, секунды; 0 — без ограничения.
    git_timeout: int = DEFAULT_GIT_TIMEOUT
    #: Общие правила из [defaults.skip] — для разовых направлений мимо профилей.
    skip: SkipRules = SkipRules()

    def pair(self, name: str | None) -> Pair:
        if not name:
            if len(self.profiles) == 1:
                return next(iter(self.profiles.values()))
            raise ConfigError(
                "no profile given (-p/--profile). Available: "
                + (", ".join(sorted(self.profiles)) or "none")
            )
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles)) or "none"
            raise ConfigError(f"profile {name!r} not found. Available: {known}") from None


def _require_str(table: dict, key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: need a non-empty string key {key!r}")
    return value.strip()


def _require_int(table: dict, key: str, where: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where}: {key!r} must be an integer greater than zero")
    return value


def _require_bool(table: dict, key: str, where: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key!r} must be true or false")
    return value


def _require_timeout(table: dict, key: str, where: str, default: int) -> int:
    """Секунды; 0 — без ограничения, отрицательное — ошибка."""
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"{where}: {key!r} must be an integer number of seconds (0 means no limit)"
        )
    return value


def adhoc_profile(
    *,
    name: str,
    source: Path,
    source_branch: str,
    target: Path,
    target_branch: str,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    dedup_window: int = DEFAULT_DEDUP_WINDOW,
    patchid_window: int = DEFAULT_PATCHID_WINDOW,
    source_prefix: str = "",
    target_prefix: str = "",
    source_key: str = "a",
    target_key: str = "b",
    keep_author: bool = DEFAULT_KEEP_AUTHOR,
    keep_coauthors: bool = DEFAULT_KEEP_COAUTHORS,
    trailer: bool = DEFAULT_TRAILER,
    squash: bool = DEFAULT_SQUASH,
    skip: SkipRules = SkipRules(),
) -> Profile:
    """Направление, собранное из флагов или ответов, а не из конфига."""
    return Profile(
        name=name,
        source=Path(source).expanduser(),
        source_branch=source_branch,
        target=Path(target).expanduser(),
        target_branch=target_branch,
        scan_limit=scan_limit,
        dedup_window=dedup_window,
        patchid_window=patchid_window,
        source_prefix=normalize_prefix(source_prefix, "--source-prefix"),
        target_prefix=normalize_prefix(target_prefix, "--target-prefix"),
        source_key=source_key,
        target_key=target_key,
        keep_author=keep_author,
        keep_coauthors=keep_coauthors,
        trailer=trailer,
        squash=squash,
        skip=skip,
    )


def log_settings(path: Path | None = None) -> tuple[bool, Path | None]:
    """Настройки журнала, прочитанные до всего остального.

    Журнал надо открыть раньше, чем разбирать конфиг по-настоящему, иначе
    в выключенный журнал успевает попасть шапка прогона. Поэтому здесь
    отдельное, нарочито терпимое чтение: любая ошибка — журнал включён,
    а пожалуется на неё уже обычный разбор.
    """
    path = Path(path).expanduser() if path else config_path()
    try:
        defaults = tomllib.loads(path.read_text(encoding="utf-8")).get("defaults") or {}
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return True, None
    if not isinstance(defaults, dict):
        return True, None
    enabled = defaults.get("log", True)
    raw_file = defaults.get("log_file")
    return (
        enabled if isinstance(enabled, bool) else True,
        Path(raw_file).expanduser() if isinstance(raw_file, str) and raw_file.strip() else None,
    )


def _parse_pair(
    name: str,
    table: dict,
    where: str,
    *,
    scan_limit: int,
    dedup: int,
    window: int,
    keep_author: bool,
    keep_coauthors: bool,
    trailer: bool,
    squash: bool,
    skip: SkipRules,
) -> "Pair":
    """Разобрать профиль. Понимает и старый формат source/target."""
    new_style = "a" in table or "b" in table
    old_style = "source" in table or "target" in table
    if new_style and old_style:
        raise ConfigError(
            f"{where}: two formats are mixed. Use either a/b (a pair of repositories),"
            " or source/target (the old format with a fixed direction)"
        )
    if old_style:
        # Старый формат — та же пара, просто стороны назывались source и target.
        a = Side("a", Path(_require_str(table, "source", where)).expanduser(),
                 _require_str(table, "source_branch", where),
                 normalize_prefix(table.get("source_prefix"), f"{where}: 'source_prefix'"))
        b = Side("b", Path(_require_str(table, "target", where)).expanduser(),
                 _require_str(table, "target_branch", where),
                 normalize_prefix(table.get("target_prefix"), f"{where}: 'target_prefix'"))
    elif new_style:
        shared = table.get("branch")
        if shared is not None and (not isinstance(shared, str) or not shared.strip()):
            raise ConfigError(f"{where}: 'branch' must be a non-empty string")
        sides = []
        for key in ("a", "b"):
            path = Path(_require_str(table, key, where)).expanduser()
            branch = table.get(f"{key}_branch") or shared
            if not isinstance(branch, str) or not branch.strip():
                raise ConfigError(
                    f"{where}: no branch for side {key!r}."
                    " Set 'branch' for both sides or"
                    f" '{key}_branch' separately"
                )
            raw_prefix = table.get(f"{key}_prefix")
            if raw_prefix is not None and not isinstance(raw_prefix, str):
                raise ConfigError(f"{where}: '{key}_prefix' must be a string")
            sides.append(
                Side(
                    key,
                    path,
                    branch.strip(),
                    normalize_prefix(raw_prefix, f"{where}: '{key}_prefix'"),
                )
            )
        a, b = sides
    else:
        raise ConfigError(
            f"{where}: need keys 'a' and 'b', the paths to the two repositories of the pair"
        )
    return Pair(
        name=name,
        a=a,
        b=b,
        implied="b" if old_style else None,
        scan_limit=_require_int(table, "scan_limit", where, scan_limit),
        dedup_window=_require_int(table, "dedup_window", where, dedup),
        patchid_window=_require_int(table, "patchid_window", where, window),
        keep_author=_require_bool(table, "keep_author", where, keep_author),
        keep_coauthors=_require_bool(table, "keep_coauthors", where, keep_coauthors),
        trailer=_require_bool(table, "trailer", where, trailer),
        squash=_require_bool(table, "squash", where, squash),
        skip=skip.merged(_parse_skip(table.get("skip"), where)),
    )


def load_config(path: Path | None = None) -> Config:
    """Прочитать конфиг; ошибки описываем словами, а не трейсбеком."""
    path = Path(path).expanduser() if path else config_path()
    if not path.exists():
        raise ConfigError(
            f"config not found: {path}\nCreate a template with: git-xfer init"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: cannot parse as TOML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from None

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path}: section [defaults] must be a table")
    log_enabled = defaults.get("log", True)
    if not isinstance(log_enabled, bool):
        raise ConfigError("[defaults]: 'log' must be true or false")
    raw_log_file = defaults.get("log_file")
    if raw_log_file is not None and (not isinstance(raw_log_file, str) or not raw_log_file.strip()):
        raise ConfigError("[defaults]: 'log_file' must be a non-empty string")
    log_file = Path(raw_log_file).expanduser() if raw_log_file else None

    agent = data.get("agent") or {}
    if not isinstance(agent, dict):
        raise ConfigError(f"{path}: section [agent] must be a table")
    resolve = agent.get("resolve_conflicts", DEFAULT_RESOLVE)
    if resolve not in RESOLVE_POLICIES:
        raise ConfigError(
            "[agent]: 'resolve_conflicts' must be one of "
            + ", ".join(repr(x) for x in RESOLVE_POLICIES)
            + f", not {resolve!r}"
        )

    scan_limit = _require_int(defaults, "scan_limit", "[defaults]", DEFAULT_SCAN_LIMIT)
    dedup = _require_int(defaults, "dedup_window", "[defaults]", DEFAULT_DEDUP_WINDOW)
    window = _require_int(defaults, "patchid_window", "[defaults]", DEFAULT_PATCHID_WINDOW)
    keep_author = _require_bool(defaults, "keep_author", "[defaults]", DEFAULT_KEEP_AUTHOR)
    keep_coauthors = _require_bool(
        defaults, "keep_coauthors", "[defaults]", DEFAULT_KEEP_COAUTHORS
    )
    trailer = _require_bool(defaults, "trailer", "[defaults]", DEFAULT_TRAILER)
    squash = _require_bool(defaults, "squash", "[defaults]", DEFAULT_SQUASH)
    git_timeout = _require_timeout(defaults, "git_timeout", "[defaults]", DEFAULT_GIT_TIMEOUT)
    skip = _parse_skip(defaults.get("skip"), "[defaults]")

    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ConfigError(f"{path}: section [profiles] must be a table")

    profiles: dict[str, Pair] = {}
    for name, table in raw_profiles.items():
        where = f"[profiles.{name}]"
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: must be a table")
        profiles[name] = _parse_pair(
            name,
            table,
            where,
            scan_limit=scan_limit,
            dedup=dedup,
            window=window,
            keep_author=keep_author,
            keep_coauthors=keep_coauthors,
            trailer=trailer,
            squash=squash,
            skip=skip,
        )
    if not profiles:
        raise ConfigError(f"{path}: no profiles defined [profiles.<name>]")
    return Config(
        path=path,
        profiles=profiles,
        log_enabled=log_enabled,
        log_file=log_file,
        resolve_conflicts=resolve,
        git_timeout=git_timeout,
        skip=skip,
    )


def write_template(path: Path | None = None, *, force: bool = False) -> tuple[Path, bool]:
    """Создать шаблон конфига. Возвращает путь и признак «создан»."""
    path = Path(path).expanduser() if path else config_path()
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path, True
