"""Запуск без установки: `python3 -m gitxfer ...`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
