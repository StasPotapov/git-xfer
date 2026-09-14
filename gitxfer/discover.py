"""Перенос объектов, чтение истории и дедупликация.

Три канала дедупликации, по убыванию надёжности:
1. трейлер `(cherry picked from commit <sha>)` в целевой истории;
2. локальный маппинг src→dst из state;
3. patch-id — эвристика, она только помечает коммит, но не прячет его.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Profile
from .gitcmd import Git
from .state import State

US = "\x1f"  # разделитель полей
RS = "\x1e"  # разделитель записей

NEW = "+"
SIMILAR = "≈"
TRANSFERRED = "−"

STATUS_HINT = {
    NEW: "новый",
    SIMILAR: "совпал patch-id",
    TRANSFERRED: "уже перенесён",
}

LOG_FORMAT = US.join(["%H", "%h", "%ad", "%an", "%s"]) + RS

_TRAILER_RE = re.compile(r"cherry picked from commit ([0-9a-fA-F]{7,64})")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class Commit:
    sha: str
    short: str
    date: str
    author: str
    subject: str


@dataclass
class Row:
    """Строка таблицы коммитов."""

    number: int
    commit: Commit
    status: str
    reason: str = ""


@dataclass
class Survey:
    """Результат разбора окна коммитов источника."""

    rows: list[Row] = field(default_factory=list)
    source_ref: str = ""
    scanned_target: int = 0
    #: Коммиты, по которым в этом прогоне нужен patch-id — «горячая» часть кэша.
    hot_shas: set[str] = field(default_factory=set)

    def by_number(self, number: int) -> Row | None:
        if 1 <= number <= len(self.rows):
            return self.rows[number - 1]
        return None


# -- перенос объектов -----------------------------------------------------


def sync(git_target: Git, profile: Profile) -> str:
    """Притащить объекты источника в целевой репо под `refs/xfer/<alias>/head`.

    Без `git remote add`: fetch по абсолютному пути. `--no-tags` обязателен —
    иначе чужие теги осядут в `refs/tags/*` рабочего репозитория.
    """
    git_target.run(
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "--",
        str(profile.source.resolve()),
        profile.source_refspec,
        mutating=True,
    )
    return git_target.run("rev-parse", "--verify", "--quiet", profile.ref, check=False).text


def has_ref(git: Git, ref: str) -> bool:
    return bool(git.run("rev-parse", "--verify", "--quiet", ref, check=False).text)


# -- чтение истории -------------------------------------------------------


def _sanitize(text: str) -> str:
    """Управляющие символы из заголовка ломают вёрстку таблицы."""
    return _CONTROL_RE.sub("", text)


def read_log(git: Git, ref: str, limit: int, *, allow_merges: bool = False) -> list[Commit]:
    """Окно коммитов, newest-first. Парсим по \\x1e/\\x1f, никогда по \\n."""
    args = ["log", f"--format={LOG_FORMAT}", "--encoding=UTF-8", "--date=short", "-n", str(limit)]
    if not allow_merges:
        args.append("--no-merges")
    args += [ref, "--"]
    raw = git.out(*args)
    commits: list[Commit] = []
    for record in raw.split(RS):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(US)
        if len(parts) < 5:
            continue
        sha, short, date, author, subject = parts[0], parts[1], parts[2], parts[3], US.join(parts[4:])
        commits.append(
            Commit(
                sha=sha,
                short=short,
                date=date,
                author=_sanitize(author),
                subject=_sanitize(subject),
            )
        )
    return commits


def trailer_map(git: Git, ref: str, limit: int) -> dict[str, list[str]]:
    """Для каждого коммита окна — хеши из его `(cherry picked from commit ...)`."""
    raw = git.out(
        "log",
        f"--format=%H{US}%B{RS}",
        "--encoding=UTF-8",
        "-n",
        str(limit),
        ref,
        "--",
    )
    result: dict[str, list[str]] = {}
    for record in raw.split(RS):
        record = record.strip("\n")
        if not record or US not in record:
            continue
        sha, body = record.split(US, 1)
        refs = [match.group(1).lower() for match in _TRAILER_RE.finditer(body)]
        if refs:
            result[sha] = refs
    return result


class ShaSet:
    """Множество хешей с поиском по сокращённому префиксу."""

    def __init__(self, shas: set[str]) -> None:
        self._full = {sha.lower() for sha in shas}

    def __contains__(self, sha: str) -> bool:
        return sha.lower() in self._full

    def match(self, prefix: str) -> str | None:
        prefix = prefix.lower()
        if prefix in self._full:
            return prefix
        if len(prefix) >= 40:
            return None
        # Сокращённый хеш: honest scan, окно тут невелико.
        found = [sha for sha in self._full if sha.startswith(prefix)]
        return found[0] if len(found) == 1 else None


# -- patch-id -------------------------------------------------------------


def patch_ids(git: Git, state: State, ref: str, window: int) -> dict[str, str]:
    """patch-id для окна коммитов `ref`, с кэшем в state.

    `git cherry` не используем: в нём `<limit>` применяется уже после
    вычисления patch-id всей целевой истории, а при несвязанных историях
    ограничить обход нечем — на большом репо это минуты.
    """
    shas = git.lines("rev-list", "--no-merges", "-n", str(window), ref, "--")
    missing = [sha for sha in shas if sha not in state.patchid_cache]
    if missing:
        raw = git.pipeline(
            # --root: иначе корневой коммит не даст ни одной строки.
            # --no-renames: patch-id должен зависеть только от текста диффа.
            ["diff-tree", "--stdin", "-p", "--root", "--no-renames"],
            ["patch-id", "--stable"],
            stdin="\n".join(missing) + "\n",
        )
        computed: dict[str, str] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2:
                computed[parts[1]] = parts[0]
        for sha in missing:
            # Пустой дифф (коммит без изменений) кэшируем как "", чтобы
            # не пересчитывать его каждый запуск.
            state.patchid_cache[sha] = computed.get(sha, "")
    return {sha: state.patchid_cache[sha] for sha in shas if state.patchid_cache.get(sha)}


# -- сведение таблицы -----------------------------------------------------


def survey(
    git: Git,
    profile: Profile,
    state: State,
    *,
    limit: int | None = None,
    window: int | None = None,
    allow_merges: bool = False,
    use_patch_id: bool = True,
) -> Survey:
    """Собрать таблицу коммитов источника с пометками +/≈/−."""
    limit = limit or profile.scan_limit
    window = window or profile.patchid_window
    commits = read_log(git, profile.ref, limit, allow_merges=allow_merges)

    # Хеши целевой ветки в окне — по ним и сверяемся.
    target_shas = ShaSet(set(git.lines("rev-list", "-n", str(window), profile.target_branch, "--")))
    # Канал 1: целевая история сама говорит, откуда её коммиты списаны.
    picked_here: set[str] = set()
    picked_short: list[str] = []
    for refs in trailer_map(git, profile.target_branch, window).values():
        for sha in refs:
            if len(sha) >= 40:
                picked_here.add(sha)
            else:
                picked_short.append(sha)
    # Канал 2: коммит источника сам помечен как перенесённый из коммита,
    # который уже лежит в цели, — так выглядит обратное направление.
    source_trailers = trailer_map(git, profile.ref, limit)
    # Канал 3: локальный маппинг. Проверяем именно достижимость из целевой
    # ветки: после reset/rebase/amend объект живёт в репозитории ещё долго,
    # и `cat-file` нашёл бы висячий коммит, которого в истории уже нет.
    mapped = {src: dst for src, dst in state.mapping.items()}

    target_pids: dict[str, str] = {}
    source_pids: dict[str, str] = {}
    if use_patch_id:
        target_pids = patch_ids(git, state, profile.target_branch, window)
        source_pids = patch_ids(git, state, profile.ref, window)
    hot = set(target_pids) | set(source_pids)
    known_pids = {pid: sha for sha, pid in target_pids.items()}

    rows: list[Row] = []
    for number, commit in enumerate(commits, start=1):
        status, reason = NEW, ""
        sha = commit.sha.lower()
        origin = next(
            (ref for ref in source_trailers.get(commit.sha, []) if target_shas.match(ref)),
            None,
        )
        if sha in picked_here or any(sha.startswith(prefix) for prefix in picked_short):
            status, reason = TRANSFERRED, "трейлер в целевой истории"
        elif origin:
            status, reason = TRANSFERRED, f"списан с {origin[:12]}, он уже в цели"
        elif mapped.get(commit.sha) and target_shas.match(mapped[commit.sha]):
            status, reason = TRANSFERRED, f"перенесён как {mapped[commit.sha][:12]}"
        else:
            pid = source_pids.get(commit.sha)
            if pid and pid in known_pids:
                status, reason = SIMILAR, f"patch-id как у {known_pids[pid][:12]}"
        rows.append(Row(number=number, commit=commit, status=status, reason=reason))

    return Survey(
        rows=rows,
        source_ref=profile.ref,
        scanned_target=len(target_pids),
        hot_shas=hot,
    )


def apply_order(git: Git, shas: list[str]) -> list[str]:
    """Порядок применения old→new.

    `--topo-order --reverse` вместо `--no-walk=sorted`: последний сортирует
    по дате коммита, а она у переносимых коммитов ничего не гарантирует.
    """
    if not shas:
        return []
    wanted = set(shas)
    # Обходим предков и фильтруем: только так порядок топологически верен.
    # `--no-walk=sorted` здесь не годится — он сортирует по дате коммита.
    ordered = git.lines("rev-list", "--topo-order", "--reverse", *shas, "--")
    return [sha for sha in ordered if sha in wanted]
