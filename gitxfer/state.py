"""State переноса: кэш patch-id, маппинг src→dst и незавершённая очередь.

Файл на каждый целевой репозиторий:
`~/.local/state/git-xfer/<sha1(realpath(target))>.json` (с учётом XDG_STATE_HOME).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import APP
from .errors import StateError

STATE_VERSION = 1


def state_dir() -> Path:
    raw = os.environ.get("XDG_STATE_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".local" / "state"
    return base / APP


def state_path(target: Path) -> Path:
    key = hashlib.sha1(str(Path(target).resolve()).encode("utf-8")).hexdigest()
    return state_dir() / f"{key}.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Progress:
    """Незавершённая серия переноса."""

    profile: str
    head_before: str
    expected_head: str
    queue: list[str]                 # ещё не применённые, в порядке применения
    done: list[dict[str, str]]       # {"src", "dst", "status"}
    current: str | None = None       # коммит, на котором встали в конфликт
    opts: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=now_iso)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Progress":
        return cls(
            profile=str(data.get("profile", "")),
            head_before=str(data.get("head_before", "")),
            expected_head=str(data.get("expected_head", "")),
            queue=[str(s) for s in data.get("queue", [])],
            done=[dict(item) for item in data.get("done", [])],
            current=data.get("current") or None,
            opts=dict(data.get("opts") or {}),
            started_at=str(data.get("started_at") or now_iso()),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "head_before": self.head_before,
            "expected_head": self.expected_head,
            "queue": list(self.queue),
            "done": list(self.done),
            "current": self.current,
            "opts": dict(self.opts),
            "started_at": self.started_at,
        }

    @property
    def total(self) -> int:
        return len(self.done) + (1 if self.current else 0) + len(self.queue)


@dataclass
class State:
    path: Path
    target: str
    patchid_cache: dict[str, str] = field(default_factory=dict)
    mapping: dict[str, str] = field(default_factory=dict)
    in_progress: Progress | None = None

    # -- загрузка/сохранение --------------------------------------------

    @classmethod
    def load(cls, target: Path) -> "State":
        path = state_path(target)
        resolved = str(Path(target).resolve())
        if not path.exists():
            return cls(path=path, target=resolved)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(
                f"state повреждён: {path} — {exc}\n"
                "Удалите файл или выполните: git xfer cleanup --state"
            ) from None
        if data.get("version") != STATE_VERSION:
            raise StateError(
                f"state {path} записан версией {data.get('version')!r}, "
                f"а ожидается {STATE_VERSION}. Выполните: git xfer cleanup --state"
            )
        progress = data.get("in_progress")
        return cls(
            path=path,
            target=resolved,
            patchid_cache=dict(data.get("patchid_cache") or {}),
            mapping=dict(data.get("mapping") or {}),
            in_progress=Progress.from_json(progress) if progress else None,
        )

    def save(self) -> None:
        """Атомарная запись: сначала во временный файл рядом, потом replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "target": self.target,
            "updated_at": now_iso(),
            "patchid_cache": self.patchid_cache,
            "mapping": self.mapping,
            "in_progress": self.in_progress.to_json() if self.in_progress else None,
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- операции --------------------------------------------------------

    def remember(self, src: str, dst: str) -> None:
        self.mapping[src] = dst

    def forget_progress(self) -> None:
        self.in_progress = None

    def trim_patchid_cache(self, keep: int = 20000) -> None:
        """Кэш не должен расти бесконечно; порядок вставки = порядок обхода."""
        if len(self.patchid_cache) <= keep:
            return
        extra = len(self.patchid_cache) - keep
        for key in list(self.patchid_cache)[:extra]:
            del self.patchid_cache[key]

    def reset(self) -> None:
        self.patchid_cache.clear()
        self.mapping.clear()
        self.in_progress = None
