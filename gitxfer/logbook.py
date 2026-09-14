"""Журнал прогонов.

Пишется рядом со state, вне рабочих репозиториев: `~/.local/state/git-xfer/`
(учитывается `XDG_STATE_HOME`). В git ничего не попадает по построению —
каталог лежит вне репозиториев вообще.

Нужен для разбора «что пошло не так»: каждый вызов git с кодом возврата
и временем, каждый шаг переноса, каждая ошибка с трейсбеком.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOGGER_NAME = "gitxfer"
FILENAME = "git-xfer.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 5

_log = logging.getLogger(LOGGER_NAME)
_log.addHandler(logging.NullHandler())
_path: Path | None = None


def default_path() -> Path:
    from .state import state_dir

    return state_dir() / FILENAME


def path() -> Path | None:
    """Куда пишем прямо сейчас; None — журнал выключен."""
    return _path


def setup(*, enabled: bool = True, file: Path | None = None) -> Path | None:
    """Включить журнал. Повторный вызов переоткрывает файл."""
    global _path
    disable()
    if not enabled:
        return None
    target = Path(file).expanduser() if file else default_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        # Журнал — удобство, а не условие работы: не смогли — молча без него.
        print(f"git-xfer: журнал не ведётся ({target}: {exc})", file=sys.stderr)
        return None
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s [%(process)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    _log.setLevel(logging.DEBUG)
    _log.addHandler(handler)
    _log.propagate = False
    _path = target
    return target


def disable() -> None:
    global _path
    for handler in list(_log.handlers):
        if not isinstance(handler, logging.NullHandler):
            _log.removeHandler(handler)
            handler.close()
    _path = None


def info(message: str, *args) -> None:
    _log.info(message, *args)


def debug(message: str, *args) -> None:
    _log.debug(message, *args)


def warn(message: str, *args) -> None:
    _log.warning(message, *args)


def error(message: str, *args, exc_info: bool = False) -> None:
    _log.error(message, *args, exc_info=exc_info)


def run_header(argv: list[str], version: str) -> None:
    info("=" * 60)
    info("git-xfer %s: %s", version, " ".join(argv))
    info("cwd=%s python=%s", os.getcwd(), sys.version.split()[0])
