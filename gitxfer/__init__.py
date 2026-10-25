"""git-xfer — перенос коммитов между несвязанными git-репозиториями."""

import sys

__version__ = "0.2.1"

#: Ниже не запускаемся: `tomllib`, которым читается конфиг, появился в 3.11.
MIN_PYTHON = (3, 11)

# Проверка стоит здесь, а не в cli.main(): `gitxfer.config` импортирует
# tomllib на уровне модуля, поэтому к моменту запуска main() падение уже
# случилось бы — и не нашим сообщением, а ModuleNotFoundError.
# Синтаксис ниже намеренно древний: f-строки и прочее на старом
# интерпретаторе дали бы SyntaxError вместо внятного текста.
if sys.version_info < MIN_PYTHON:
    raise SystemExit(
        "git-xfer: needs Python {need} or newer, running {have}.\n"
        "  interpreter: {exe}\n"
        "  reason: the config is read with the tomllib module, added in 3.11\n"
        "\n"
        "  Install the tool so that the interpreter is pinned:\n"
        "    uv tool install git+https://github.com/StasPotapov/git-xfer\n"
        "    pipx install --python python3.11 "
        "git+https://github.com/StasPotapov/git-xfer\n"
        "  or run the right python explicitly:\n"
        "    python3.11 -m gitxfer ...".format(
            need=".".join(str(part) for part in MIN_PYTHON),
            have=".".join(str(part) for part in sys.version_info[:3]),
            exe=sys.executable,
        )
    )
