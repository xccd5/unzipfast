"""Archive inspection, member normalization and extraction planning.

Everything in this module is pure metadata work: no file is written here.
That makes the planning step cheap and lets the CLI show an accurate summary
(and an accurate progress bar) before the first byte is decompressed.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Sequence

from .errors import ArchiveError, PasswordError

#: Compression method used by WinZip AES (handled by the optional pyzipper).
ZIP_AES = 99

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:$")
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_INVALID_WINDOWS_CHARS = '<>:"|?*'


class Task(NamedTuple):
    """One file that should be extracted.

    ``index`` points into ``ZipFile.infolist()``; passing the index (instead of
    the name) keeps duplicate member names unambiguous and pickles cheaply.
    """

    index: int
    target: str
    size: int


@dataclass(frozen=True)
class Entry:
    """A member of the archive, as displayed by ``--list``."""

    index: int
    name: str
    size: int
    compressed_size: int
    encrypted: bool
    is_dir: bool
    is_symlink: bool
    date_time: tuple[int, ...]


@dataclass(frozen=True)
class Skip:
    """An entry that will not be extracted, and why."""

    name: str
    reason: str


@dataclass
class Plan:
    """The result of planning an extraction."""

    tasks: list[Task] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    total_bytes: int = 0
    encrypted: bool = False


def _uses_aes(zf: zipfile.ZipFile) -> bool:
    return any(info.compress_type == ZIP_AES for info in zf.infolist())


def open_archive(
    path: str | os.PathLike[str], password: bytes | None = None
) -> zipfile.ZipFile:
    """Open a ZIP archive, transparently handling WinZip AES archives.

    AES archives need the optional :mod:`pyzipper` dependency.  Legacy
    ZipCrypto archives are handled by the standard library.
    """
    archive = os.fspath(path)
    if not os.path.exists(archive):
        raise ArchiveError(f"archive not found: {archive}")
    if os.path.isdir(archive):
        raise ArchiveError(f"expected a file but got a directory: {archive}")

    try:
        zf: zipfile.ZipFile = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"cannot open {archive}: {exc}") from exc

    if _uses_aes(zf):
        zf.close()
        try:
            import pyzipper  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ArchiveError(
                "this archive uses WinZip AES encryption; install the optional "
                'dependency with: pip install "unzipfast[aes]"'
            ) from exc
        zf = pyzipper.AESZipFile(archive)

    if password is not None:
        zf.setpassword(password)
    return zf


def is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


def is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def resolve_password(
    zf: zipfile.ZipFile, candidates: Sequence[str]
) -> bytes | None:
    """Return the first password that can decrypt the archive.

    Returns ``None`` when the archive is not encrypted.  Raises
    :class:`PasswordError` when the archive is encrypted and no candidate
    works.
    """
    encrypted = [info for info in zf.infolist() if is_encrypted(info) and not info.is_dir()]
    if not encrypted:
        return None
    if not candidates:
        raise PasswordError(
            "archive is encrypted; pass -p/--password or set UNZIPFAST_PASSWORD"
        )

    # The largest member gives the most reliable wrong-password signal.
    probe = max(encrypted, key=lambda info: info.file_size)
    last_error: Exception | None = None
    for candidate in candidates:
        password = candidate.encode("utf-8")
        zf.setpassword(password)
        try:
            with zf.open(probe) as handle:
                handle.read(1)
            return password
        except Exception as exc:  # wrong password or unsupported encryption
            last_error = exc
    raise PasswordError(
        f"none of the given passwords could open the archive ({last_error})"
    )


def normalize_member(name: str) -> tuple[str, ...] | None:
    """Split an archive member name into safe path components.

    Returns ``None`` for names that must not be extracted (absolute paths are
    stripped, but any ``..`` component, NUL byte or Windows drive letter makes
    the whole entry unsafe).
    """
    if not name or "\x00" in name:
        return None
    raw = name.replace("\\", "/").lstrip("/")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or _DRIVE_LETTER.match(part):
            return None
        parts.append(part)
    return tuple(parts) or None


def _windows_problem(parts: Sequence[str]) -> str | None:
    """Return a reason when a member cannot be written on Windows."""
    for part in parts:
        stem = part.split(".")[0].lower()
        if stem in _RESERVED_NAMES:
            return f"reserved name on Windows: {part}"
        if part != part.rstrip(" ."):
            return f"trailing dot or space on Windows: {part!r}"
        if any(char in part for char in _INVALID_WINDOWS_CHARS):
            return f"invalid character on Windows: {part!r}"
    return None


def _match(name: str, pattern: str) -> bool:
    pattern = pattern.replace("\\", "/")
    if fnmatch.fnmatch(name, pattern):
        return True
    # A bare pattern like "*.csv" also matches files in subdirectories.
    if "/" not in pattern:
        return fnmatch.fnmatch(name.rsplit("/", 1)[-1], pattern)
    return False


def _selected(name: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    if include and not any(_match(name, pattern) for pattern in include):
        return False
    if exclude and any(_match(name, pattern) for pattern in exclude):
        return False
    return True


def _contained(root: Path, target: Path) -> bool:
    """Verify that ``target`` stays inside ``root``.

    Existing symlinks and junctions are resolved, so a link that already sits
    in the destination cannot redirect a write outside of it.
    """
    try:
        root_str = os.path.normcase(os.path.realpath(root))
        target_str = os.path.normcase(os.path.realpath(target))
        return os.path.commonpath([root_str, target_str]) == root_str
    except ValueError:  # different drives on Windows
        return False


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _free_path(path: Path, taken: set[str]) -> Path:
    """Find a non-existing name like ``report (1).txt``."""
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    for counter in range(1, 10_000):
        candidate = path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists() and _key(candidate) not in taken:
            return candidate
    raise ArchiveError(f"could not find a free file name for {path}")


def read_entries(zf: zipfile.ZipFile) -> list[Entry]:
    """Read archive metadata for display purposes."""
    entries: list[Entry] = []
    for index, info in enumerate(zf.infolist()):
        entries.append(
            Entry(
                index=index,
                name=info.filename,
                size=info.file_size,
                compressed_size=info.compress_size,
                encrypted=is_encrypted(info),
                is_dir=info.is_dir(),
                is_symlink=is_symlink(info),
                date_time=tuple(info.date_time),
            )
        )
    return entries


def build_plan(
    archive_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    flat: bool = False,
    overwrite: str = "skip",
) -> Plan:
    """Decide which members to extract and where they go.

    ``overwrite`` is one of ``skip``, ``overwrite`` or ``rename``.
    """
    if overwrite not in ("skip", "overwrite", "rename"):
        raise ValueError(f"invalid overwrite policy: {overwrite!r}")

    dest = Path(destination)
    if dest.exists() and not dest.is_dir():
        raise ArchiveError(f"output path is not a directory: {dest}")

    plan = Plan()
    directories: list[str] = []
    seen_dirs: set[str] = set()
    taken: set[str] = set()

    with open_archive(archive_path) as zf:
        for index, info in enumerate(zf.infolist()):
            name = info.filename

            if is_symlink(info):
                plan.skipped.append(Skip(name, "symlink (not extracted for safety)"))
                continue

            parts = normalize_member(name)
            if parts is None:
                plan.skipped.append(Skip(name, "unsafe path"))
                continue

            display = "/".join(parts)

            if info.is_dir():
                if flat or (exclude and any(_match(display, pattern) for pattern in exclude)):
                    continue
                target = dest.joinpath(*parts)
                if not _contained(dest, target):
                    plan.skipped.append(Skip(name, "outside destination"))
                    continue
                key = _key(target)
                if key not in seen_dirs:
                    seen_dirs.add(key)
                    directories.append(str(target))
                continue

            if info.flag_bits & 0x40 and info.compress_type != ZIP_AES:
                plan.skipped.append(Skip(name, "unsupported strong encryption"))
                continue

            if not _selected(display, include, exclude):
                continue

            if os.name == "nt":
                problem = _windows_problem(parts)
                if problem is not None:
                    plan.skipped.append(Skip(name, problem))
                    continue

            target = dest / parts[-1] if flat else dest.joinpath(*parts)
            if not _contained(dest, target):
                plan.skipped.append(Skip(name, "outside destination"))
                continue

            if overwrite == "skip" and target.exists():
                plan.skipped.append(Skip(name, "already exists"))
                continue

            key = _key(target)
            if key in taken:
                if overwrite == "rename":
                    target = _free_path(target, taken)
                    key = _key(target)
                else:
                    plan.skipped.append(Skip(name, "duplicate target"))
                    continue
            elif overwrite == "rename" and target.exists():
                target = _free_path(target, taken)
                key = _key(target)

            taken.add(key)
            plan.tasks.append(Task(index=index, target=str(target), size=info.file_size))
            plan.total_bytes += info.file_size
            plan.encrypted = plan.encrypted or is_encrypted(info)

    plan.directories = directories
    return plan
