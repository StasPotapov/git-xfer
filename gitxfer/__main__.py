"""Запуск пакета: `python3 -m gitxfer ...`.

Работает и когда этот файл запускают напрямую (`python3 gitxfer/__main__.py`):
в таком режиме пакета вокруг нет, поэтому сначала кладём в путь каталог
клона и импортируем по полному имени.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gitxfer.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    sys.exit(main())
