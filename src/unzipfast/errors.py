"""Exceptions that are safe to show to the end user."""

from __future__ import annotations


class UnzipfastError(Exception):
    """Base class for expected, user-facing errors."""


class ArchiveError(UnzipfastError):
    """The archive is missing, unreadable or unsupported."""


class PasswordError(UnzipfastError):
    """The archive is encrypted and no usable password was supplied."""
