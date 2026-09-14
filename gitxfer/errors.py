"""Исключения и коды выхода."""

from __future__ import annotations

# Коды выхода утилиты.
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PREFLIGHT = 2
EXIT_CONFLICT = 3
EXIT_GIT = 4
EXIT_INTERRUPT = 130


class XferError(Exception):
    """Ошибка, которую показываем пользователю без трейсбека."""

    exit_code = EXIT_USAGE


class ConfigError(XferError):
    """Конфиг отсутствует, не читается или описан неверно."""


class PreflightError(XferError):
    """Предполётная проверка не прошла."""

    exit_code = EXIT_PREFLIGHT


class StateError(XferError):
    """State разошёлся с реальностью репозитория."""
