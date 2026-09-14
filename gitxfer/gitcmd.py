"""Единственная точка запуска git.

Всё общение с git идёт через `Git`: фиксированный набор `-c` перед командой,
предсказуемое окружение, `shell=False` и utf-8 с заменой битых байт. Ни один
модуль не вызывает subprocess напрямую.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import logbook
from .errors import EXIT_GIT, XferError

GIT = "git"

#: Конфиг, который должен действовать на любой вызов.
BASE_CONFIG: tuple[str, ...] = (
    # fetch по локальному пути: дефолтная политика для протокола file — "user".
    "protocol.file.allow=always",
    # пути в выводе — как есть, без \NNN-эскейпов.
    "core.quotePath=false",
    "color.ui=false",
    "i18n.logOutputEncoding=UTF-8",
    "patchid.stable=true",
)

#: Окружение, гасящее всё интерактивное.
BASE_ENV: dict[str, str] = {
    # без него `cherry-pick --continue` откроет редактор и подвиснет.
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}


class GitError(XferError):
    """git вернул ненулевой код там, где мы этого не ждали."""

    exit_code = EXIT_GIT

    def __init__(self, result: "GitResult") -> None:
        self.result = result
        super().__init__(result.describe())


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.strip()

    def lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if line]

    def describe(self) -> str:
        cmd = " ".join(shlex.quote(a) for a in self.args)
        tail = (self.stderr.strip() or self.stdout.strip()).strip()
        head = f"git {cmd} → код {self.returncode}"
        return f"{head}\n{tail}" if tail else head


class Git:
    """Обёртка над git для одного репозитория."""

    def __init__(self, repo: Path | str, *, dry_run: bool = False, verbose: bool = False) -> None:
        self.repo = Path(repo)
        self.dry_run = dry_run
        self.verbose = verbose

    # -- сборка команды -------------------------------------------------

    def _argv(self, args: Sequence[str], config: Iterable[str] = ()) -> list[str]:
        argv = [GIT, "-C", str(self.repo)]
        for item in (*BASE_CONFIG, *config):
            argv += ["-c", item]
        argv += [str(a) for a in args]
        return argv

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(BASE_ENV)
        if extra:
            env.update(extra)
        return env

    def _trace(self, argv: Sequence[str], prefix: str = "+") -> None:
        if self.verbose:
            print(f"{prefix} {' '.join(shlex.quote(a) for a in argv)}", file=sys.stderr)

    # -- запуск ---------------------------------------------------------

    def run(
        self,
        *args: str,
        check: bool = True,
        config: Iterable[str] = (),
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        mutating: bool = False,
    ) -> GitResult:
        """Выполнить git. `mutating=True` — команда меняет репозиторий."""
        argv = self._argv(args, config)
        if mutating and self.dry_run:
            self._trace(argv, prefix="[dry-run]")
            if not self.verbose:
                print(f"[dry-run] {' '.join(shlex.quote(a) for a in argv)}")
            return GitResult(tuple(args), 0, "", "")
        self._trace(argv)
        started = time.monotonic()
        proc = subprocess.run(
            argv,
            shell=False,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env(env),
        )
        result = GitResult(tuple(args), proc.returncode, proc.stdout, proc.stderr)
        spent = time.monotonic() - started
        logbook.debug(
            "git %s → %d за %.2f с (%s)",
            " ".join(shlex.quote(a) for a in args),
            result.returncode,
            spent,
            self.repo,
        )
        if not result.ok:
            tail = (result.stderr.strip() or result.stdout.strip())[:2000]
            if tail:
                logbook.debug("  stderr: %s", tail)
        if check and not result.ok:
            raise GitError(result)
        return result

    def out(self, *args: str, **kwargs) -> str:
        """stdout без хвостовых переводов строки."""
        return self.run(*args, **kwargs).text

    def lines(self, *args: str, **kwargs) -> list[str]:
        return self.run(*args, **kwargs).lines()

    def ok(self, *args: str, **kwargs) -> bool:
        """Успешна ли команда; ошибку не поднимаем."""
        kwargs.setdefault("check", False)
        return self.run(*args, **kwargs).ok

    def pipeline(
        self,
        *stages: Sequence[str],
        stdin: str | None = None,
        config: Iterable[str] = (),
    ) -> str:
        """Цепочка git-команд через настоящие пайпы: stdout последней стадии.

        Промежуточные диффы бывают в десятки мегабайт, поэтому они не
        материализуются в питоне, а текут по пайпам. Вход пишем отдельным
        потоком: иначе большой stdin и большой stdout встают в дедлок.
        """
        if not stages:
            return ""
        payload = (stdin or "").encode("utf-8") if stdin is not None else None
        procs: list[subprocess.Popen] = []
        err_sinks: list = []
        writer: threading.Thread | None = None
        prev_stdout = None
        try:
            for index, stage in enumerate(stages):
                argv = self._argv(stage, config)
                self._trace(argv, prefix="|" if index else "+")
                is_last = index == len(stages) - 1
                err = subprocess.PIPE if is_last else tempfile.TemporaryFile()
                if not is_last:
                    err_sinks.append(err)
                proc = subprocess.Popen(
                    argv,
                    shell=False,
                    stdin=subprocess.PIPE if index == 0 else prev_stdout,
                    stdout=subprocess.PIPE,
                    stderr=err,
                    env=self._env(),
                )
                # Конец пайпа должен остаться только у дочернего процесса,
                # иначе следующая стадия не дождётся EOF.
                if prev_stdout is not None:
                    prev_stdout.close()
                procs.append(proc)
                prev_stdout = proc.stdout

            first_stdin = procs[0].stdin
            assert first_stdin is not None

            def _feed() -> None:
                try:
                    if payload:
                        first_stdin.write(payload)
                finally:
                    try:
                        first_stdin.close()
                    except OSError:
                        pass

            writer = threading.Thread(target=_feed, daemon=True)
            writer.start()
            out_bytes, last_err = procs[-1].communicate()
            for proc in procs[:-1]:
                proc.wait()
        finally:
            if writer is not None:
                writer.join(timeout=5)
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()

        try:
            for index, proc in enumerate(procs):
                if not proc.returncode:
                    continue
                if index == len(procs) - 1:
                    text = (last_err or b"").decode("utf-8", "replace")
                else:
                    sink = err_sinks[index]
                    sink.seek(0)
                    text = sink.read().decode("utf-8", "replace")
                raise GitError(GitResult(tuple(stages[index]), proc.returncode, "", text))
        finally:
            for sink in err_sinks:
                try:
                    sink.close()
                except OSError:
                    pass
        return (out_bytes or b"").decode("utf-8", "replace")
