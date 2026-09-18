"""Чтение конфига и профилей.

Конфиг лежит вне репозитория утилиты (`~/.config/git-xfer/config.toml`,
с учётом `XDG_CONFIG_HOME`) и правится руками; мы его только читаем.
"""

from __future__ import annotations

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

#: Дописывать ли в сообщение `(cherry picked from commit <sha>)`. По
#: умолчанию нет: сообщение коммита переносится один в один. Цена — самый
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
# Конфиг git-xfer. Правится руками, утилита его только читает.
#
# Профиль — это ПАРА репозиториев, а не направление. Направление выбирается
# в момент вызова: `--to b` тащит из a в b, `--to a` — обратно.
# Профилей может быть сколько угодно: разные пары, разные ветки одной пары.

[defaults]
# Сколько коммитов источника показывать. Разово — флагом --limit.
scan_limit = 30

# Как глубоко читать целевую историю в поисках трейлера
# (cherry picked from commit ...). Работает, только если трейлер включён
# (см. trailer ниже) — зато тогда это самый надёжный канал: окно определяет,
# на сколько своих коммитов назад утилита помнит, что уже переносила.
# Читается быстро, уменьшать смысла нет.
dedup_window = 5000

# Окно сравнения по patch-id — эвристика для пометки ~.
# Дороже, но результат кэшируется.
patchid_window = 1000

# Строка (cherry picked from commit <sha>) в сообщении перенесённого коммита.
#   false — НЕ дописывать: сообщение едет один в один  ← так по умолчанию
#   true  — дописывать: в конце сообщения появится эта строка
# Что стоит за false: трейлер — самый надёжный канал дедупликации, он лежит
# в самой целевой истории. Без него о том, что коммит уже переносили, утилита
# знает только из своего state (эта машина, и только в ту сторону, в которую
# переносили) и по patch-id — эвристике, которая метит коммит значком ≈.
# Разово, без правки конфига: --trailer / --no-trailer.
trailer = false

# Схлопывать ли выбранные коммиты в один.
#   false — НЕ схлопывать: сколько выбрали, столько и приедет  ← по умолчанию
#   true  — вся выбранная серия становится одним коммитом
# Сообщение схлопнутого: сообщения серии подряд, либо своё — --message "...".
# Разово: --squash / --no-squash.
squash = false

# Чьё имя останется в поле author перенесённого коммита.
#   false — ваше: автор и коммиттер — тот, кто переносит,
#           author date — момент переноса                   ← так по умолчанию
#   true  — автора и его дату оставить от исходного коммита
# Коммиттером (кто применил) в git в любом случае становится тот, кто
# переносит: это поле настройки не имеет.
# Разово: --keep-author / --reset-author.
keep_author = false

# Потолок на один вызов git, секунды. Страховка от зависания: git, севший
# ждать ввода (редактор, пейджер, запрос пароля), иначе держит утилиту вечно.
#   600 — прервать вызов, который длится дольше десяти минут  ← по умолчанию
#   0   — не ограничивать
git_timeout = 600

# Журнал прогонов: что запускалось, с каким кодом и где встало.
# Лежит рядом со state, вне рабочих репозиториев, в git не попадает.
# Посмотреть путь: git-xfer status
log = true
# log_file = "~/.local/state/git-xfer/git-xfer.log"

[agent]
# Насколько самостоятельно агент (скилл git-xfer для Claude Code) разбирает
# конфликты. Читает это скилл; сама утилита по ней ничего не делает, только
# проверяет значение и показывает его в `git-xfer status`.
#   "ask"        — не правит ничего сам, только показывает суть и предлагает
#   "mechanical" — сам чинит механические конфликты (импорты, соседние
#                  строки, форматирование, сгенерённое) и показывает, что
#                  сделал; смысловые обязательно выносит на решение человеку
#   "auto"       — разбирает всё сам, включая смысловые, и отчитывается
#                  постфактум. Ошибка тут уезжает коммитом в рабочий
#                  репозиторий, так что включайте осознанно
resolve_conflicts = "mechanical"

# [profiles.myproj]
# a = "/path/to/repo-a"
# b = "/path/to/repo-b"
# branch = "master"        # одна ветка с обеих сторон
#
# trailer, keep_author, squash, scan_limit, dedup_window и patchid_window можно
# переопределить внутри профиля.
#
# [profiles.myproj-release]  # та же пара, другие ветки
# a = "/path/to/repo-a"
# b = "/path/to/repo-b"
# a_branch = "release/1.x"   # если ветки называются по-разному
# b_branch = "stable"
# --- Проект живёт в подкаталоге одного из репозиториев -------------------
# Путь стороны — ВСЕГДА корень репозитория; подкаталог задаётся отдельным
# ключом <сторона>_prefix. При переносе пути переписываются: снимается
# префикс источника, надевается префикс цели.
#
# [profiles.myapp]
# a = "/path/to/monorepo"
# a_prefix = "apps/mobile"       # относительно корня репо, без слеша впереди
# b = "/path/to/personal-repo"   # тут проект в корне — b_prefix не нужен
# branch = "master"
#
# Коммит, не задевший подкаталог, в список не попадает вовсе. Коммит,
# задевший и подкаталог, и файлы вне него, переносится ЧАСТИЧНО (только
# внутренняя часть) и помечен в списке звёздочкой: + *
"""

def _xdg_dir(var: str, fallback: str) -> Path:
    raw = os.environ.get(var)
    base = Path(raw).expanduser() if raw else Path.home() / fallback
    return base / APP


def config_dir() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def config_path() -> Path:
    return config_dir() / "config.toml"


def normalize_prefix(raw: str | None, where: str = "префикс") -> str:
    """`"./a/b/"` → `"a/b"`. Абсолютный путь и `..` — ошибка конфига."""
    if raw is None or not str(raw).strip():
        return ""
    text = str(raw).strip().replace("\\", "/")
    if text.startswith("/"):
        raise ConfigError(
            f"{where}: нужен путь внутри репозитория, а не абсолютный ({raw!r})"
        )
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ConfigError(f"{where}: '..' в пути не допускается ({raw!r})")
        parts.append(part)
    return "/".join(parts)


def _at(prefix: str) -> str:
    return f"/{prefix}" if prefix else ""


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
    #: Дописывать ли трейлер `(cherry picked from commit ...)`.
    trailer: bool = DEFAULT_TRAILER
    #: Схлопывать ли всю серию в один коммит.
    squash: bool = DEFAULT_SQUASH

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
    trailer: bool = DEFAULT_TRAILER
    squash: bool = DEFAULT_SQUASH
    #: Для старого формата source/target направление задано самими ключами,
    #: и спрашивать о нём нечего. У пары a/b его выбирают при вызове.
    implied: str | None = None

    def side(self, key: str) -> Side:
        if key not in ("a", "b"):
            raise ConfigError(f"сторона может быть только 'a' или 'b', а не {key!r}")
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
            trailer=self.trailer,
            squash=self.squash,
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

    def pair(self, name: str | None) -> Pair:
        if not name:
            if len(self.profiles) == 1:
                return next(iter(self.profiles.values()))
            raise ConfigError(
                "не указан профиль (-p/--profile). Доступны: "
                + (", ".join(sorted(self.profiles)) or "ни одного")
            )
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles)) or "ни одного"
            raise ConfigError(f"профиль {name!r} не найден. Доступны: {known}") from None


def _require_str(table: dict, key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: нужен непустой строковый ключ {key!r}")
    return value.strip()


def _require_int(table: dict, key: str, where: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where}: {key!r} должен быть целым числом больше нуля")
    return value


def _require_bool(table: dict, key: str, where: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key!r} должен быть true или false")
    return value


def _require_timeout(table: dict, key: str, where: str, default: int) -> int:
    """Секунды; 0 — без ограничения, отрицательное — ошибка."""
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"{where}: {key!r} должен быть целым числом секунд (0 — без ограничения)"
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
    trailer: bool = DEFAULT_TRAILER,
    squash: bool = DEFAULT_SQUASH,
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
        trailer=trailer,
        squash=squash,
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
    trailer: bool,
    squash: bool,
) -> "Pair":
    """Разобрать профиль. Понимает и старый формат source/target."""
    new_style = "a" in table or "b" in table
    old_style = "source" in table or "target" in table
    if new_style and old_style:
        raise ConfigError(
            f"{where}: смешаны два формата. Либо a/b (пара репозиториев),"
            " либо source/target (старый формат с зашитым направлением)"
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
            raise ConfigError(f"{where}: 'branch' должен быть непустой строкой")
        sides = []
        for key in ("a", "b"):
            path = Path(_require_str(table, key, where)).expanduser()
            branch = table.get(f"{key}_branch") or shared
            if not isinstance(branch, str) or not branch.strip():
                raise ConfigError(
                    f"{where}: не задана ветка для стороны {key!r}."
                    " Укажите 'branch' для обеих сторон или"
                    f" '{key}_branch' отдельно"
                )
            raw_prefix = table.get(f"{key}_prefix")
            if raw_prefix is not None and not isinstance(raw_prefix, str):
                raise ConfigError(f"{where}: '{key}_prefix' должен быть строкой")
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
            f"{where}: нужны ключи 'a' и 'b' — пути к двум репозиториям пары"
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
        trailer=_require_bool(table, "trailer", where, trailer),
        squash=_require_bool(table, "squash", where, squash),
    )


def load_config(path: Path | None = None) -> Config:
    """Прочитать конфиг; ошибки описываем словами, а не трейсбеком."""
    path = Path(path).expanduser() if path else config_path()
    if not path.exists():
        raise ConfigError(
            f"конфиг не найден: {path}\nСоздайте шаблон командой: git-xfer init"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: не разбирается как TOML — {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: не читается — {exc}") from None

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path}: секция [defaults] должна быть таблицей")
    log_enabled = defaults.get("log", True)
    if not isinstance(log_enabled, bool):
        raise ConfigError("[defaults]: 'log' должен быть true или false")
    raw_log_file = defaults.get("log_file")
    if raw_log_file is not None and (not isinstance(raw_log_file, str) or not raw_log_file.strip()):
        raise ConfigError("[defaults]: 'log_file' должен быть непустой строкой")
    log_file = Path(raw_log_file).expanduser() if raw_log_file else None

    agent = data.get("agent") or {}
    if not isinstance(agent, dict):
        raise ConfigError(f"{path}: секция [agent] должна быть таблицей")
    resolve = agent.get("resolve_conflicts", DEFAULT_RESOLVE)
    if resolve not in RESOLVE_POLICIES:
        raise ConfigError(
            "[agent]: 'resolve_conflicts' должен быть одним из "
            + ", ".join(repr(x) for x in RESOLVE_POLICIES)
            + f", а не {resolve!r}"
        )

    scan_limit = _require_int(defaults, "scan_limit", "[defaults]", DEFAULT_SCAN_LIMIT)
    dedup = _require_int(defaults, "dedup_window", "[defaults]", DEFAULT_DEDUP_WINDOW)
    window = _require_int(defaults, "patchid_window", "[defaults]", DEFAULT_PATCHID_WINDOW)
    keep_author = _require_bool(defaults, "keep_author", "[defaults]", DEFAULT_KEEP_AUTHOR)
    trailer = _require_bool(defaults, "trailer", "[defaults]", DEFAULT_TRAILER)
    squash = _require_bool(defaults, "squash", "[defaults]", DEFAULT_SQUASH)
    git_timeout = _require_timeout(defaults, "git_timeout", "[defaults]", DEFAULT_GIT_TIMEOUT)

    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ConfigError(f"{path}: секция [profiles] должна быть таблицей")

    profiles: dict[str, Pair] = {}
    for name, table in raw_profiles.items():
        where = f"[profiles.{name}]"
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: должен быть таблицей")
        profiles[name] = _parse_pair(
            name,
            table,
            where,
            scan_limit=scan_limit,
            dedup=dedup,
            window=window,
            keep_author=keep_author,
            trailer=trailer,
            squash=squash,
        )
    if not profiles:
        raise ConfigError(f"{path}: не описан ни один профиль [profiles.<имя>]")
    return Config(
        path=path,
        profiles=profiles,
        log_enabled=log_enabled,
        log_file=log_file,
        resolve_conflicts=resolve,
        git_timeout=git_timeout,
    )


def write_template(path: Path | None = None, *, force: bool = False) -> tuple[Path, bool]:
    """Создать шаблон конфига. Возвращает путь и признак «создан»."""
    path = Path(path).expanduser() if path else config_path()
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path, True
