"""git-xfer — перенос коммитов между несвязанными git-репозиториями."""

import sys

__version__ = "0.1.0"

#: Ниже не запускаемся: `tomllib`, которым читается конфиг, появился в 3.11.
MIN_PYTHON = (3, 11)

# Проверка стоит здесь, а не в cli.main(): `gitxfer.config` импортирует
# tomllib на уровне модуля, поэтому к моменту запуска main() падение уже
# случилось бы — и не нашим сообщением, а ModuleNotFoundError.
# Синтаксис ниже намеренно древний: f-строки и прочее на старом
# интерпретаторе дали бы SyntaxError вместо внятного текста.
if sys.version_info < MIN_PYTHON:
    raise SystemExit(
        "git-xfer: нужен Python {need} или новее, а запущен {have}.\n"
        "  интерпретатор: {exe}\n"
        "  причина: конфиг читается модулем tomllib, он появился в 3.11\n"
        "\n"
        "  Поставьте утилиту так, чтобы интерпретатор фиксировался:\n"
        "    uv tool install git+https://github.com/StasPotapov/git-xfer\n"
        "    pipx install --python python3.11 "
        "git+https://github.com/StasPotapov/git-xfer\n"
        "  либо укажите нужный python явно:\n"
        "    python3.11 -m gitxfer ...".format(
            need=".".join(str(part) for part in MIN_PYTHON),
            have=".".join(str(part) for part in sys.version_info[:3]),
            exe=sys.executable,
        )
    )
