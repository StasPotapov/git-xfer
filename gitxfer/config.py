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

DEFAULT_SCAN_LIMIT = 300
DEFAULT_PATCHID_WINDOW = 2000

_REF_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

CONFIG_TEMPLATE = """\
# Конфиг git-xfer. Правится руками, утилита его только читает.
# Профиль описывает одно направление переноса: source → target.
# Обратное направление — отдельный профиль с зеркальными путями.

[defaults]
scan_limit = 300        # сколько коммитов показывать в list/apply
patchid_window = 2000   # сколько коммитов сравнивать по patch-id

# [profiles.proj]
# source = "/Users/me/dev/project-a"
# source_branch = "master"
# target = "/Users/me/dev/project-b"
# target_branch = "master"

# [profiles.proj-back]
# source = "/Users/me/dev/project-b"
# source_branch = "master"
# target = "/Users/me/dev/project-a"
# target_branch = "master"
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
class Profile:
    """Одно направление переноса."""

    name: str
    source: Path
    source_branch: str
    target: Path
    target_branch: str
    scan_limit: int
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
        digest = hashlib.sha1(self.name.encode("utf-8")).hexdigest()[:8]
        return f"{alias or 'profile'}-{digest}"

    @property
    def ref(self) -> str:
        """Ref, под которым в целевом репо живут объекты источника."""
        return f"refs/xfer/{self.alias}/head"

    @property
    def source_refspec(self) -> str:
        return f"+refs/heads/{self.source_branch}:{self.ref}"


@dataclass(frozen=True)
class Config:
    path: Path
    profiles: dict[str, Profile]

    def profile(self, name: str | None) -> Profile:
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


def load_config(path: Path | None = None) -> Config:
    """Прочитать конфиг; ошибки описываем словами, а не трейсбеком."""
    path = Path(path).expanduser() if path else config_path()
    if not path.exists():
        raise ConfigError(
            f"конфиг не найден: {path}\nСоздайте шаблон командой: git xfer init"
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
    scan_limit = _require_int(defaults, "scan_limit", "[defaults]", DEFAULT_SCAN_LIMIT)
    window = _require_int(defaults, "patchid_window", "[defaults]", DEFAULT_PATCHID_WINDOW)

    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ConfigError(f"{path}: секция [profiles] должна быть таблицей")

    profiles: dict[str, Profile] = {}
    for name, table in raw_profiles.items():
        where = f"[profiles.{name}]"
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: должен быть таблицей")
        profiles[name] = Profile(
            name=name,
            source=Path(_require_str(table, "source", where)).expanduser(),
            source_branch=_require_str(table, "source_branch", where),
            target=Path(_require_str(table, "target", where)).expanduser(),
            target_branch=_require_str(table, "target_branch", where),
            scan_limit=_require_int(table, "scan_limit", where, scan_limit),
            patchid_window=_require_int(table, "patchid_window", where, window),
        )
    if not profiles:
        raise ConfigError(f"{path}: не описан ни один профиль [profiles.<имя>]")
    return Config(path=path, profiles=profiles)


def write_template(path: Path | None = None, *, force: bool = False) -> tuple[Path, bool]:
    """Создать шаблон конфига. Возвращает путь и признак «создан»."""
    path = Path(path).expanduser() if path else config_path()
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path, True
