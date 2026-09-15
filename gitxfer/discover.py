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
from .prefix import touches_outside
from .state import State

US = "\x1f"  # разделитель полей
RS = "\x1e"  # разделитель записей

NEW = "+"
SIMILAR = "≈"
TRANSFERRED = "−"
#: Коммит задел и подкаталог, и файлы вне него: приедет только часть.
PARTIAL = "*"

STATUS_HINT = {
    NEW: "новый",
    SIMILAR: "совпал patch-id",
    TRANSFERRED: "уже перенесён",
}
PARTIAL_HINT = "задевает файлы вне подкаталога — приедет только часть"

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
    #: Коммит выходит за подкаталог: перенесётся только часть внутри него.
    partial: bool = False

    @property
    def mark(self) -> str:
        return f"{self.status}{PARTIAL}" if self.partial else self.status


@dataclass
class Survey:
    """Результат разбора окна коммитов источника."""

    rows: list[Row] = field(default_factory=list)
    source_ref: str = ""
    scanned_target: int = 0
    #: Ключи кэша patch-id, задействованные в этом прогоне, — «горячая» часть.
    #: Именно ключи, а не sha: при префиксе ключ составной (см. `pid_key`).
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


def sanitize(text: str) -> str:
    """Управляющие символы из заголовка ломают вёрстку таблицы."""
    return _CONTROL_RE.sub("", text)


def read_log(
    git: Git,
    ref: str,
    limit: int,
    *,
    allow_merges: bool = False,
    prefix: str = "",
) -> list[Commit]:
    """Окно коммитов, newest-first. Парсим по \\x1e/\\x1f, никогда по \\n.

    `prefix` сужает обход до подкаталога: иначе окно из `scan_limit`
    коммитов монорепозитория могло бы не содержать ни одного нужного.
    """
    args = ["log", f"--format={LOG_FORMAT}", "--encoding=UTF-8", "--date=short", "-n", str(limit)]
    if not allow_merges:
        args.append("--no-merges")
    if prefix:
        # Без --full-history git упрощает историю по pathspec и на нелинейных
        # участках прячет коммиты, которые подкаталог всё-таки задели.
        args.append("--full-history")
    args += [ref, "--"]
    if prefix:
        args.append(prefix)
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
                author=sanitize(author),
                subject=sanitize(subject),
            )
        )
    return commits


def trailer_map(git: Git, ref: str, limit: int, prefix: str = "") -> dict[str, list[str]]:
    """Для каждого коммита окна — хеши из его `(cherry picked from commit ...)`."""
    args = ["log", f"--format=%H{US}%B{RS}", "--encoding=UTF-8", "-n", str(limit)]
    if prefix:
        # То же упрощение истории, что и в read_log: окна обязаны совпадать,
        # иначе коммит попадёт в таблицу, а его трейлер прочитан не будет —
        # и канал дедупликации молча отключится.
        args.append("--full-history")
    args += [ref, "--"]
    if prefix:
        args.append(prefix)
    raw = git.out(*args)
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


def pid_key(sha: str, prefix: str = "") -> str:
    """Ключ кэша patch-id.

    Префикс — часть ключа: с `--relative` дифф считается по другим путям,
    и один и тот же коммит при разных префиксах даёт разные patch-id.
    Без префикса ключ прежний, так что старый кэш остаётся валидным.
    """
    return f"{sha}@{prefix}" if prefix else sha


def patch_ids(
    git: Git, state: State, ref: str, window: int, *, prefix: str = ""
) -> dict[str, str]:
    """patch-id для окна коммитов `ref`, с кэшем в state.

    `git cherry` не используем: в нём `<limit>` применяется уже после
    вычисления patch-id всей целевой истории, а при несвязанных историях
    ограничить обход нечем — на большом репо это минуты.
    """
    args = ["rev-list", "--no-merges", "-n", str(window)]
    if prefix:
        args.append("--full-history")   # окно то же, что у read_log
    args += [ref, "--"]
    if prefix:
        args.append(prefix)
    shas = git.lines(*args)
    missing = [sha for sha in shas if pid_key(sha, prefix) not in state.patchid_cache]
    if missing:
        # --root: иначе корневой коммит не даст ни одной строки.
        # --no-renames: patch-id должен зависеть только от текста диффа.
        # --relative: срезает префикс, чтобы дифф сошёлся со второй стороной.
        diff = ["diff-tree", "--stdin", "-p", "--root", "--no-renames"]
        if prefix:
            diff.append(f"--relative={prefix}")
        raw = git.pipeline(
            diff,
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
            state.patchid_cache[pid_key(sha, prefix)] = computed.get(sha, "")
    return {
        sha: state.patchid_cache[pid_key(sha, prefix)]
        for sha in shas
        if state.patchid_cache.get(pid_key(sha, prefix))
    }


# -- сведение таблицы -----------------------------------------------------


def survey(
    git: Git,
    profile: Profile,
    state: State,
    *,
    limit: int | None = None,
    dedup_window: int | None = None,
    patchid_window: int | None = None,
    allow_merges: bool = False,
    use_patch_id: bool = True,
) -> Survey:
    """Собрать таблицу коммитов источника с пометками +/≈/−.

    Окна намеренно разные. `dedup_window` — надёжные каналы (трейлер,
    маппинг): читается быстро, и от него зависит, на сколько своих коммитов
    назад мы помним, что уже переносили. `patchid_window` — эвристика `≈`:
    считается дольше, поэтому окно меньше, а результат кэшируется.
    """
    limit = limit or profile.scan_limit
    dedup_window = dedup_window or profile.dedup_window
    patchid_window = patchid_window or profile.patchid_window
    src_prefix = profile.source_prefix
    dst_prefix = profile.target_prefix
    commits = read_log(
        git, profile.ref, limit, allow_merges=allow_merges, prefix=src_prefix
    )

    # Хеши целевой ветки в окне — по ним и сверяемся. Подкаталог цели тут
    # не сужаем: трейлер ищем по всей её истории, иначе коммит, приехавший
    # до появления подкаталога, перестал бы считаться перенесённым.
    target_shas = ShaSet(
        set(git.lines("rev-list", "-n", str(dedup_window), profile.target_branch, "--"))
    )
    # Канал 1: целевая история сама говорит, откуда её коммиты списаны.
    picked_here: set[str] = set()
    picked_short: list[str] = []
    for refs in trailer_map(git, profile.target_branch, dedup_window).values():
        for sha in refs:
            if len(sha) >= 40:
                picked_here.add(sha)
            else:
                picked_short.append(sha)
    # Канал 2: коммит источника сам помечен как перенесённый из коммита,
    # который уже лежит в цели, — так выглядит обратное направление.
    source_trailers = trailer_map(git, profile.ref, limit, src_prefix)
    # Канал 3: локальный маппинг. Проверяем именно достижимость из целевой
    # ветки: после reset/rebase/amend объект живёт в репозитории ещё долго,
    # и `cat-file` нашёл бы висячий коммит, которого в истории уже нет.
    mapped = {src: dst for src, dst in state.mapping.items()}

    target_pids: dict[str, str] = {}
    source_pids: dict[str, str] = {}
    if use_patch_id:
        target_pids = patch_ids(
            git, state, profile.target_branch, patchid_window, prefix=dst_prefix
        )
        # Со стороны источника patch-id нужны только для показанных строк.
        source_pids = patch_ids(git, state, profile.ref, limit, prefix=src_prefix)
    hot = {pid_key(sha, dst_prefix) for sha in target_pids} | {
        pid_key(sha, src_prefix) for sha in source_pids
    }
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
        rows.append(
            Row(
                number=number,
                commit=commit,
                status=status,
                reason=reason,
                partial=touches_outside(git, commit.sha, src_prefix),
            )
        )

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
