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

APP = "git-xfer"

DEFAULT_SCAN_LIMIT = 30
#: Как глубоко читать целевую историю в поисках трейлеров. Канал дешёвый
#: (`git log` без диффов), а окно определяет, на сколько своих коммитов назад
#: утилита помнит, что уже переносила, — экономить тут нечего.
DEFAULT_DEDUP_WINDOW = 5000
#: Окно patch-id. Канал дорогой (считается дифф каждого коммита), зато
#: результат кэшируется в state, так что платим один раз.
DEFAULT_PATCHID_WINDOW = 1000

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
# (cherry picked from commit ...). Самый надёжный канал: окно определяет,
# на сколько своих коммитов назад утилита помнит, что уже переносила.
# Читается быстро, уменьшать смысла нет.
dedup_window = 5000

# Окно сравнения по patch-id — эвристика для пометки ~.
# Дороже, но результат кэшируется.
patchid_window = 1000

# Журнал прогонов: что запускалось, с каким кодом и где встало.
# Лежит рядом со state, вне рабочих репозиториев, в git не попадает.
# Посмотреть путь: git-xfer status
log = true
# log_file = "~/.local/state/git-xfer/git-xfer.log"

# [profiles.myproj]
# a = "/path/to/repo-a"
# b = "/path/to/repo-b"
# branch = "master"        # одна ветка с обеих сторон
#
# [profiles.myproj-release]  # та же пара, другие ветки
# a = "/path/to/repo-a"
# b = "/path/to/repo-b"
# a_branch = "release/1.x"   # если ветки называются по-разному
# b_branch = "stable"
"""

def _xdg_dir(var: str, fallback: str) -> Path:
    raw = os.environ.get(var)
    base = Path(raw).expanduser() if raw else Path.home() / fallback
    return base / APP


def config_dir() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass(frozen=True)
class Side:
    """Одна сторона пары."""

    key: str          # "a" или "b"
    path: Path
    branch: str


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
            f"{self.source} ({self.source_branch}) → "
            f"{self.target} ({self.target_branch})"
        )

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
        )


@dataclass(frozen=True)
class Config:
    path: Path
    profiles: dict[str, Pair]
    log_enabled: bool = True
    log_file: Path | None = None

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
                 _require_str(table, "source_branch", where))
        b = Side("b", Path(_require_str(table, "target", where)).expanduser(),
                 _require_str(table, "target_branch", where))
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
            sides.append(Side(key, path, branch.strip()))
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

    scan_limit = _require_int(defaults, "scan_limit", "[defaults]", DEFAULT_SCAN_LIMIT)
    dedup = _require_int(defaults, "dedup_window", "[defaults]", DEFAULT_DEDUP_WINDOW)
    window = _require_int(defaults, "patchid_window", "[defaults]", DEFAULT_PATCHID_WINDOW)

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
        )
    if not profiles:
        raise ConfigError(f"{path}: не описан ни один профиль [profiles.<имя>]")
    return Config(
        path=path,
        profiles=profiles,
        log_enabled=log_enabled,
        log_file=log_file,
    )


def write_template(path: Path | None = None, *, force: bool = False) -> tuple[Path, bool]:
    """Создать шаблон конфига. Возвращает путь и признак «создан»."""
    path = Path(path).expanduser() if path else config_path()
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path, True
